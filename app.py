from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime
import threading
from collections import deque
import traceback

app = Flask(__name__)

# =====================
# DISK / STATE FILE HANDLING
# =====================
DATA_DIR = "/data"
LOCAL_STATE_FILE = "vb_state.json"
if os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK):
    STATE_FILE_PATH = os.path.join(DATA_DIR, "vb_state.json")
else:
    STATE_FILE_PATH = os.path.abspath(LOCAL_STATE_FILE)
    os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)

print(f"💾 Using state file at: {STATE_FILE_PATH}")

# =====================
# CONFIG - MATCHES PINE SCRIPT
# =====================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

SYMBOL = os.environ.get('SYMBOL', 'TAOUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 11))
MARGIN_MODE = 'cross'
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE', 30.0))
INITIAL_BALANCE = float(os.environ.get('INITIAL_BALANCE', 20.0))
DEBOUNCE_SEC = float(os.environ.get('DEBOUNCE_SEC', 2.0))
PRICE_CHECK_INTERVAL = 1.0
MAX_PRICE_FAILURES = 5
POSITION_SYNC_INTERVAL = 30.0

# TP/SL CONFIG
TAKE_PROFIT_PCT = float(os.environ.get('TAKE_PROFIT_PCT', 1.3))
STOP_LOSS_PCT = float(os.environ.get('STOP_LOSS_PCT', 0.75))

# AUTO-RESET CONFIG
PHASE_1_THRESHOLD = float(os.environ.get('PHASE_1_THRESHOLD', 2000.0))
PHASE_1_REINVEST = 1.0
PHASE_2_REINVEST = 0.05
PROFIT_RESET_THRESHOLD = 2.0
MAX_DRAWDOWN_STOP = float(os.environ.get('MAX_DRAWDOWN_STOP', 50.0))

LIVE_MODE = True
BASE_URL = "https://api.bitget.com"
last_signal_time = 0

# =====================
# LOGGING HELPER
# =====================
def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")

# =====================
# VIRTUAL BALANCE WITH AUTO-RESET & EMERGENCY STOP
# =====================
class VirtualBalance:
    def __init__(self, initial_balance):
        self.initial_balance = initial_balance
        self.starting_balance = initial_balance
        self.current_balance = initial_balance
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = 0.0
        self.current_position = None
        self.trade_history = []
        self.monitor_thread = None
        self.sync_thread = None
        self.stop_monitoring = threading.Event()
        self.stop_syncing = threading.Event()
        self.position_lock = threading.Lock()
        self.max_drawdown = 0.0
        self.peak_balance = initial_balance
        self.consecutive_losses = 0
        self.recent_pnls = deque(maxlen=10)
        self.daily_trade_count = 0
        self.last_trade_date = None
        self.last_sync_time = 0
        self.reset_count = 0
        self.phase_1_resets = 0
        self.phase_2_resets = 0
        self.total_withdrawn = 0.0
        self.total_profit_generated = 0.0
        self.trading_paused = False

    # ---- PHASE & AUTO-RESET ----
    def get_current_phase(self):
        return "growth" if self.starting_balance < PHASE_1_THRESHOLD else "extraction"

    def check_auto_reset(self):
        if self.current_balance >= self.starting_balance * (1 + PROFIT_RESET_THRESHOLD):
            profit = self.current_balance - self.starting_balance
            phase = self.get_current_phase()
            if phase == "growth":
                reinvest_pct = PHASE_1_REINVEST
                withdraw_amount = profit * (1 - reinvest_pct)
                log(f"🚀 PHASE 1 RESET: Growth Mode - Reinvesting 100%")
                self.phase_1_resets += 1
            else:
                reinvest_pct = PHASE_2_REINVEST
                withdraw_amount = profit * (1 - reinvest_pct)
                log(f"💰 PHASE 2 RESET: Extraction Mode - Withdrawing 95%!")
                self.phase_2_resets += 1

            new_starting = self.starting_balance + (profit * reinvest_pct)
            log(f"🎉 PROFIT RESET: Profit={profit:.2f}, Withdraw={withdraw_amount:.2f}, New Start={new_starting:.2f}")

            self.starting_balance = new_starting
            self.current_balance = new_starting
            self.peak_balance = new_starting
            self.max_drawdown = 0.0
            self.total_withdrawn += withdraw_amount
            self.total_profit_generated += profit
            self.reset_count += 1
            save_state()
            return True
        return False

    # ---- EMERGENCY STOP ----
    def check_emergency_stop(self):
        if self.max_drawdown >= MAX_DRAWDOWN_STOP and not self.trading_paused:
            log(f"🚨 EMERGENCY STOP! Drawdown: {self.max_drawdown:.2f}%")
            self.trading_paused = True
            if self.current_position:
                log("🚨 Closing open position due to emergency stop")
                close_all_positions(SYMBOL)
                time.sleep(1)
                current_price = get_current_price(SYMBOL)
                if current_price:
                    self.close_position(current_price, reason="emergency_stop")
            save_state()
            return True
        return False

    # ---- SYNC THREAD ----
    def start_sync_thread(self):
        if self.sync_thread is None or not self.sync_thread.is_alive():
            self.stop_syncing.clear()
            self.sync_thread = threading.Thread(target=self.sync_with_bitget, daemon=True)
            self.sync_thread.start()
            log("✅ Sync thread started")

    def sync_with_bitget(self):
        while not self.stop_syncing.is_set():
            try:
                time.sleep(POSITION_SYNC_INTERVAL)
                pos = get_positions(SYMBOL)
                if pos.get('code') != '00000':
                    continue
                positions = pos.get('data', [])
                has_pos = any(float(p.get('total', 0)) > 0 for p in positions)
                self.last_sync_time = time.time()
            except Exception as e:
                log(f"Sync error: {e}\n{traceback.format_exc()}", "ERROR")
        log("Sync thread stopped")

    # ---- POSITION MANAGEMENT ----
    def calculate_tp_sl(self, side, entry_price):
        if side=='long':
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
            sl_price = entry_price * (1 - STOP_LOSS_PCT / 100)
        else:
            tp_price = entry_price * (1 - TAKE_PROFIT_PCT / 100)
            sl_price = entry_price * (1 + STOP_LOSS_PCT / 100)
        return tp_price, sl_price

    def open_position(self, side, entry_price, qty):
        with self.position_lock:
            if self.current_position:
                log("Already in position, skipping", "WARNING")
                return False
            tp, sl = self.calculate_tp_sl(side, entry_price)
            self.current_position = {'side': side, 'entry_price': entry_price, 'qty': qty, 'tp_price': tp, 'sl_price': sl, 'open_time': datetime.now().isoformat()}
            save_state()
            log(f"📝 Opened {side} {qty} @ {entry_price:.2f} | TP {tp:.2f} | SL {sl:.2f}")
            self._start_monitoring()
            return True

    def close_position(self, exit_price, reason="normal"):
        with self.position_lock:
            if not self.current_position:
                return 0
            self.stop_monitoring.set()
            side = self.current_position['side']
            entry_price = self.current_position['entry_price']
            qty = self.current_position['qty']
            price_change = (exit_price-entry_price)/entry_price if side=='long' else (entry_price-exit_price)/entry_price
            pnl = qty * entry_price * price_change
            self.current_balance += pnl
            self.total_pnl += pnl
            self.total_trades += 1
            self.recent_pnls.append(pnl)
            if pnl>0:
                self.winning_trades +=1
                self.consecutive_losses =0
            else:
                self.losing_trades +=1
                self.consecutive_losses +=1
            if self.current_balance > self.peak_balance:
                self.peak_balance = self.current_balance
            drawdown = (self.peak_balance - self.current_balance)/self.peak_balance*100
            if drawdown>self.max_drawdown:
                self.max_drawdown = drawdown
            self.trade_history.append({'side':side,'entry_price':entry_price,'exit_price':exit_price,'qty':qty,'pnl':pnl,'balance_after':self.current_balance,'close_time':datetime.now().isoformat(),'close_reason':reason})
            self.current_position=None
            save_state()
            log(f"💰 Closed {side} ({reason}) | P&L: {pnl:+.2f} | Balance: {self.current_balance:.2f} | DD: {drawdown:.2f}%")
            self.check_auto_reset()
            self.check_emergency_stop()
            return pnl

    # ---- MONITOR THREAD ----
    def _start_monitoring(self):
        self.stop_monitoring.clear()
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(target=self.monitor_position, daemon=True)
            self.monitor_thread.start()
            log("Started position monitor thread")

    def monitor_position(self):
        log("🔍 Monitoring position")
        try:
            while not self.stop_monitoring.is_set():
                with self.position_lock:
                    if not self.current_position:
                        break
                    current_price = get_current_price(SYMBOL)
                    if not current_price:
                        time.sleep(PRICE_CHECK_INTERVAL)
                        continue
                    side = self.current_position['side']
                    tp, sl = self.current_position['tp_price'], self.current_position['sl_price']
                    hit_tp = (side=='long' and current_price>=tp) or (side=='short' and current_price<=tp)
                    hit_sl = (side=='long' and current_price<=sl) or (side=='short' and current_price>=sl)
                    if hit_tp or hit_sl:
                        reason = "TP" if hit_tp else "SL"
                        log(f"⚡ {reason} hit for {side} at {current_price}")
                        close_all_positions(SYMBOL)
                        time.sleep(1)
                        self.close_position(current_price, reason=reason)
                        break
                time.sleep(PRICE_CHECK_INTERVAL)
        except Exception as e:
            log(f"Monitor crashed: {e}\n{traceback.format_exc()}", "ERROR")
        log("Monitor stopped")

    # ---- RISK CHECK ----
    def should_trade(self):
        if self.trading_paused:
            return False
        if self.consecutive_losses>=5:
            return False
        if self.current_balance<self.initial_balance*0.3:
            return False
        return True

    # ---- STATS ----
    def get_stats(self):
        return {
            "current_balance": self.current_balance,
            "current_position": self.current_position,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "can_trade": self.should_trade(),
            "monitor_alive": self.monitor_thread.is_alive() if self.monitor_thread else False,
            "sync_alive": self.sync_thread.is_alive() if self.sync_thread else False,
        }

# Initialize VirtualBalance
virtual_balance = VirtualBalance(INITIAL_BALANCE)
virtual_balance.start_sync_thread()

# =====================
# PERSISTENCE
# =====================
def save_state():
    try:
        data = {
            "starting_balance": virtual_balance.starting_balance,
            "current_balance": virtual_balance.current_balance,
            "total_trades": virtual_balance.total_trades,
            "winning_trades": virtual_balance.winning_trades,
            "losing_trades": virtual_balance.losing_trades,
            "max_drawdown": virtual_balance.max_drawdown,
            "trade_history": virtual_balance.trade_history,
            "consecutive_losses": virtual_balance.consecutive_losses,
            "reset_count": virtual_balance.reset_count
        }
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"Failed to save state: {e}", "ERROR")

def load_state():
    try:
        if os.path.exists(STATE_FILE_PATH):
            with open(STATE_FILE_PATH,'r') as f:
                data=json.load(f)
            virtual_balance.starting_balance=data.get("starting_balance",INITIAL_BALANCE)
            virtual_balance.current_balance=data.get("current_balance",INITIAL_BALANCE)
            virtual_balance.total_trades=data.get("total_trades",0)
            virtual_balance.winning_trades=data.get("winning_trades",0)
            virtual_balance.losing_trades=data.get("losing_trades",0)
            virtual_balance.max_drawdown=data.get("max_drawdown",0)
            virtual_balance.trade_history=data.get("trade_history",[])
            virtual_balance.consecutive_losses=data.get("consecutive_losses",0)
            virtual_balance.reset_count=data.get("reset_count",0)
            log("✅ Loaded saved state")
        else:
            log("ℹ️ No saved state found, starting fresh")
    except Exception as e:
        log(f"Failed to load state: {e}", "ERROR")

load_state()

# =====================
# BITGET API HELPERS
# =====================
def get_signature(timestamp, method, request_path, body):
    message = f"{timestamp}{method.upper()}{request_path}{body}"
    hmac_key = hmac.new(BITGET_SECRET_KEY.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(hmac_key.digest()).decode()

def send_request(method, path, params=None):
    url = BASE_URL + path
    body = json.dumps(params) if params else ''
    ts = str(int(time.time()*1000))
    headers = {
        'ACCESS-KEY': BITGET_API_KEY,
        'ACCESS-SIGN': get_signature(ts, method, path, body),
        'ACCESS-TIMESTAMP': ts,
        'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
        'Content-Type': 'application/json'
    }
    try:
        if method.lower()=='post':
            r=requests.post(url,json=params,headers=headers,timeout=5)
        else:
            r=requests.get(url,params=params,headers=headers,timeout=5)
        return r.json()
    except Exception as e:
        log(f"API request failed: {e}", "ERROR")
        return {}

def get_current_price(symbol):
    try:
        resp = send_request('GET', f'/api/mix/v1/market/ticker?symbol={symbol}')
        if 'data' in resp:
            return float(resp['data']['last'])
    except:
        pass
    return None

def place_order(symbol, side, size, price=None):
    params = {
        'symbol':symbol,
        'side':side,
        'size':size,
        'type':'market' if price is None else 'limit'
    }
    if price:
        params['price']=price
    return send_request('POST','/api/mix/v1/order/place',params)

def close_all_positions(symbol):
    pos = get_positions(symbol)
    for p in pos.get('data',[]):
        if float(p.get('total',0))>0:
            side = 'sell' if p['side']=='buy' else 'buy'
            size = p['total']
            place_order(symbol, side, size)
    return True

def get_positions(symbol):
    return send_request('GET', f'/api/mix/v1/position/singlePosition?symbol={symbol}')

# =====================
# FLASK WEBHOOK
# =====================
@app.route('/webhook', methods=['POST'])
def webhook():
    global last_signal_time
    try:
        data = request.get_json()
        sig = request.headers.get('X-SIGNATURE', '')
        if sig != WEBHOOK_SECRET:
            return jsonify({"error":"unauthorized"}), 403

        now = time.time()
        if now - last_signal_time < DEBOUNCE_SEC:
            return jsonify({"status":"debounced"}), 200
        last_signal_time = now

        signal = data.get('signal', '').lower()
        price = get_current_price(SYMBOL)
        if not price:
            return jsonify({"error":"price_unavailable"}),500

        if not virtual_balance.should_trade():
            return jsonify({"status":"cannot_trade"}), 200

        qty = round(virtual_balance.current_balance * LEVERAGE / price,4)

        if signal=='long':
            place_order(SYMBOL,'buy',qty)
            virtual_balance.open_position('long', price, qty)
        elif signal=='short':
            place_order(SYMBOL,'sell',qty)
            virtual_balance.open_position('short', price, qty)
        elif signal=='close':
            close_all_positions(SYMBOL)
            virtual_balance.close_position(price, reason="manual_close")

        return jsonify({"status":"ok"}), 200
    except Exception as e:
        log(f"Webhook error: {e}\n{traceback.format_exc()}","ERROR")
        return jsonify({"error":"internal"}),500

@app.route('/stats', methods=['GET'])
def stats():
    return jsonify(virtual_balance.get_stats())

if __name__=="__main__":
    log("✅ Bot initialization complete - threads running and webhook ready")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
