from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime
import threading
from collections import deque
import traceback

app = Flask(__name__)

# =====================
# DISK / STATE FILE HANDLING (RENDER-SAFE)
# =====================
DATA_DIR = "/data"  # Must match your Render disk mount
STATE_FILE_PATH = os.path.join(DATA_DIR, "vb_state.json")
os.makedirs(DATA_DIR, exist_ok=True)
print(f"💾 Using state file at: {STATE_FILE_PATH}")

# =====================
# CONFIG
# =====================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

SYMBOL = os.environ.get('SYMBOL', 'TAOUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 11))
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE', 30.0))
INITIAL_BALANCE = float(os.environ.get('INITIAL_BALANCE', 20.0))
DEBOUNCE_SEC = float(os.environ.get('DEBOUNCE_SEC', 2.0))
PRICE_CHECK_INTERVAL = 1.0
MAX_PRICE_FAILURES = 5
POSITION_SYNC_INTERVAL = 30.0

TAKE_PROFIT_PCT = float(os.environ.get('TAKE_PROFIT_PCT', 1.3))
STOP_LOSS_PCT = float(os.environ.get('STOP_LOSS_PCT', 0.75))

PHASE_1_THRESHOLD = float(os.environ.get('PHASE_1_THRESHOLD', 2000.0))
PHASE_1_REINVEST = 1.0
PHASE_2_REINVEST = 0.05
PROFIT_RESET_THRESHOLD = 2.0
MAX_DRAWDOWN_STOP = float(os.environ.get('MAX_DRAWDOWN_STOP', 50.0))

BASE_URL = "https://api.bitget.com"
last_signal_time = 0

# =====================
# LOGGING
# =====================
def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")

# =====================
# VIRTUAL BALANCE
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

    # =====================
    # PHASE / AUTO-RESET LOGIC
    # =====================
    def get_current_phase(self):
        return "growth" if self.starting_balance < PHASE_1_THRESHOLD else "extraction"

    def check_auto_reset(self):
        if self.current_balance >= self.starting_balance * (1 + PROFIT_RESET_THRESHOLD):
            profit = self.current_balance - self.starting_balance
            phase = self.get_current_phase()
            if phase == "growth":
                reinvest_pct = PHASE_1_REINVEST
                withdraw_amount = profit * (1 - reinvest_pct)
                self.phase_1_resets += 1
            else:
                reinvest_pct = PHASE_2_REINVEST
                withdraw_amount = profit * (1 - reinvest_pct)
                self.phase_2_resets += 1
            new_starting = self.starting_balance + (profit * reinvest_pct)
            self.starting_balance = new_starting
            self.current_balance = new_starting
            self.peak_balance = new_starting
            self.max_drawdown = 0.0
            self.total_withdrawn += withdraw_amount
            self.total_profit_generated += profit
            self.reset_count += 1
            save_state()
            log(f"🎉 PROFIT RESET: Phase={phase}, Withdraw=${withdraw_amount:.2f}, New Starting=${new_starting:.2f}")
            return True
        return False

    def check_emergency_stop(self):
        if self.max_drawdown >= MAX_DRAWDOWN_STOP and not self.trading_paused:
            self.trading_paused = True
            log(f"🚨 EMERGENCY STOP: Drawdown {self.max_drawdown:.2f}%")
            if self.current_position:
                close_all_positions(SYMBOL)
                time.sleep(1)
                self.close_position(self.current_position['entry_price'], reason="emergency_stop")
            save_state()
            return True
        return False

    # =====================
    # THREAD MANAGEMENT
    # =====================
    def start_sync_thread(self):
        if self.sync_thread is None or not self.sync_thread.is_alive():
            self.stop_syncing.clear()
            self.sync_thread = threading.Thread(target=self.sync_with_bitget, daemon=True)
            self.sync_thread.start()
            log("✅ Started position sync thread")

    def _start_monitoring(self):
        self.stop_monitoring.clear()
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(target=self.monitor_position, daemon=True)
            self.monitor_thread.start()
            log("✅ Started position monitor thread")

    # =====================
    # POSITION MANAGEMENT
    # =====================
    def open_position(self, side, entry_price, qty):
        with self.position_lock:
            if self.current_position:
                log("⚠️ Already in position, skipping open")
                return False
            tp, sl = self.calculate_tp_sl(side, entry_price)
            self.current_position = {
                'side': side,
                'entry_price': entry_price,
                'qty': qty,
                'tp_price': tp,
                'sl_price': sl,
                'open_time': datetime.now().isoformat()
            }
            save_state()
            log(f"📝 Opened {side} {qty} @ {entry_price} | TP={tp:.2f}, SL={sl:.2f}")
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
            price_change = (exit_price - entry_price)/entry_price if side=='long' else (entry_price - exit_price)/entry_price
            pnl = qty * entry_price * price_change
            self.current_balance += pnl
            self.total_pnl += pnl
            self.total_trades += 1
            self.recent_pnls.append(pnl)
            if pnl > 0:
                self.winning_trades += 1
                self.consecutive_losses = 0
            else:
                self.losing_trades += 1
                self.consecutive_losses += 1
            if self.current_balance > self.peak_balance:
                self.peak_balance = self.current_balance
            drawdown = (self.peak_balance - self.current_balance)/self.peak_balance*100
            if drawdown > self.max_drawdown:
                self.max_drawdown = drawdown
            today = datetime.now().date()
            if self.last_trade_date != today:
                self.daily_trade_count = 0
                self.last_trade_date = today
            self.daily_trade_count += 1
            self.trade_history.append({
                'side': side,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'qty': qty,
                'pnl': pnl,
                'balance_after': self.current_balance,
                'close_time': datetime.now().isoformat(),
                'close_reason': reason
            })
            self.current_position = None
            save_state()
            log(f"💰 Closed {side} ({reason}) | P&L={pnl:+.2f} | Balance={self.current_balance:.2f} | DD={drawdown:.2f}%")
            self.check_auto_reset()
            self.check_emergency_stop()
            return pnl

    def calculate_tp_sl(self, side, entry_price):
        if side == 'long':
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
            sl_price = entry_price * (1 - STOP_LOSS_PCT / 100)
        else:
            tp_price = entry_price * (1 - TAKE_PROFIT_PCT / 100)
            sl_price = entry_price * (1 + STOP_LOSS_PCT / 100)
        return tp_price, sl_price

    # =====================
    # MONITORING & SYNC
    # =====================
    def monitor_position(self):
        consecutive_failures = 0
        while not self.stop_monitoring.is_set():
            try:
                with self.position_lock:
                    if not self.current_position:
                        break
                    price = get_current_price(SYMBOL)
                    if not price:
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_PRICE_FAILURES:
                            self.close_position(self.current_position['entry_price'], reason="emergency")
                            break
                        time.sleep(PRICE_CHECK_INTERVAL)
                        continue
                    consecutive_failures = 0
                    side = self.current_position['side']
                    tp, sl = self.current_position['tp_price'], self.current_position['sl_price']
                    if (side=='long' and (price >= tp or price <= sl)) or (side=='short' and (price <= tp or price >= sl)):
                        reason = "TP" if ((side=='long' and price >= tp) or (side=='short' and price <= tp)) else "SL"
                        close_all_positions(SYMBOL)
                        time.sleep(1)
                        self.close_position(price, reason=reason)
                        break
                time.sleep(PRICE_CHECK_INTERVAL)
            except Exception as e:
                log(f"Monitor error: {e}", "ERROR")

    def sync_with_bitget(self):
        while not self.stop_syncing.is_set():
            try:
                time.sleep(POSITION_SYNC_INTERVAL)
                bitget_pos = get_positions(SYMBOL)
                if bitget_pos.get('code') != '00000':
                    continue
                positions = bitget_pos.get('data', [])
                has_pos = False
                side, qty = None, 0
                for p in positions:
                    if float(p.get('total', 0)) > 0:
                        has_pos = True
                        side = p['holdSide']
                        qty = float(p['total'])
                        break
                with self.position_lock:
                    if self.current_position and not has_pos:
                        self.close_position(get_current_price(SYMBOL), reason="external_close")
                    elif not self.current_position and has_pos:
                        price = get_current_price(SYMBOL)
                        self.current_position = {
                            'side': side,
                            'entry_price': price,
                            'qty': qty,
                            'tp_price': self.calculate_tp_sl(side, price)[0],
                            'sl_price': self.calculate_tp_sl(side, price)[1],
                            'open_time': datetime.now().isoformat(),
                            'recovered': True
                        }
                        save_state()
                        self._start_monitoring()
                self.last_sync_time = time.time()
            except Exception as e:
                log(f"Sync error: {e}", "ERROR")

    # =====================
    # RISK MANAGEMENT
    # =====================
    def should_trade(self):
        if self.trading_paused:
            return False
        if self.consecutive_losses >= 5:
            return False
        if self.current_balance < self.initial_balance*0.3:
            return False
        return True

    def get_stats(self):
        return {
            'balance': self.current_balance,
            'position': self.current_position,
            'trading_paused': self.trading_paused,
            'sync_alive': self.sync_thread.is_alive() if self.sync_thread else False,
            'monitor_alive': self.monitor_thread.is_alive() if self.monitor_thread else False
        }

# =====================
# STATE SAVE / LOAD
# =====================
def save_state():
    try:
        state = virtual_balance.__dict__.copy()
        state['last_saved'] = datetime.now().isoformat()
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(state, f, indent=2)
        log(f"💾 State saved")
    except Exception as e:
        log(f"❌ Save failed: {e}", "ERROR")

def load_state():
    global virtual_balance
    try:
        if os.path.exists(STATE_FILE_PATH):
            with open(STATE_FILE_PATH, 'r') as f:
                st = json.load(f)
            vb = VirtualBalance(st.get('initial_balance', INITIAL_BALANCE))
            vb.__dict__.update(st)
            virtual_balance = vb
            log("✅ Loaded state")
        else:
            virtual_balance = VirtualBalance(INITIAL_BALANCE)
            log("ℹ️ No saved state found, starting fresh")
        virtual_balance.start_sync_thread()
        if virtual_balance.current_position:
            virtual_balance._start_monitoring()
    except Exception as e:
        log(f"❌ Load failed: {e}", "ERROR")

# =====================
# BITGET API HELPERS
# =====================
def generate_signature(timestamp, method, request_path, body, secret):
    body_str = json.dumps(body) if body else ""
    message = timestamp + method + request_path + body_str
    return base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()

def bitget_request(method, endpoint, params=None):
    try:
        timestamp = str(int(time.time()*1000))
        sign = generate_signature(timestamp, method, endpoint, params, BITGET_SECRET_KEY)
        headers = {
            'ACCESS-KEY': BITGET_API_KEY,
            'ACCESS-SIGN': sign,
            'ACCESS-TIMESTAMP': timestamp,
            'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
            'Content-Type': 'application/json'
        }
        url = BASE_URL + endpoint
        r = requests.post(url, json=params, headers=headers, timeout=10) if method=="POST" else requests.get(url, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        log(f"API request error: {e}", "ERROR")
        return {'error':'request_failed'}

def get_current_price(symbol):
    try:
        r = requests.get(BASE_URL + f"/api/mix/v1/market/ticker?symbol={symbol}", timeout=5).json()
        if r.get('code')=='00000':
            return float(r['data']['last'])
    except:
        return None
    return None

def calculate_position_size(balance, price, leverage, risk_pct):
    return round(balance * (risk_pct/100) * leverage / price, 3)

def place_order(symbol, side, size):
    endpoint="/api/mix/v1/order/placeOrder"
    params={'symbol':symbol,'marginCoin':'USDT','side':side,'orderType':'market','size':str(size)}
    return bitget_request("POST", endpoint, params)

def get_positions(symbol):
    return bitget_request("GET", f"/api/mix/v1/position/singlePosition?symbol={symbol}&marginCoin=USDT")

def close_all_positions(symbol):
    pos = get_positions(symbol)
    if pos.get('code')=='00000':
        for p in pos.get('data',[]):
            if float(p.get('total',0))>0:
                side = 'close_long' if p['holdSide']=='long' else 'close_short'
                place_order(symbol, side, float(p['total']))

# =====================
# WEBHOOK
# =====================
@app.route('/webhook', methods=['POST'])
def webhook():
    global last_signal_time
    data = request.json
    if not data or data.get('secret') != WEBHOOK_SECRET:
        return jsonify({'error':'Unauthorized'}), 401
    now = time.time()
    if now - last_signal_time < DEBOUNCE_SEC:
        return jsonify({'success':True,'action':'debounced'})
    last_signal_time = now
    if not virtual_balance.should_trade():
        return jsonify({'success':False,'reason':'risk_limits'})
    action = data.get('action','').upper()
    price = get_current_price(SYMBOL)
    if not price:
        return jsonify({'error':'Price fetch failed'}),500
    qty = calculate_position_size(virtual_balance.current_balance, price, LEVERAGE, RISK_PERCENTAGE)
    if action in ['BUY','LONG']:
        place_order(SYMBOL,'open_long',qty)
        virtual_balance.open_position('long', price, qty)
    elif action in ['SELL','SHORT']:
        place_order(SYMBOL,'open_short',qty)
        virtual_balance.open_position('short', price, qty)
    elif action=='CLOSE':
        if virtual_balance.current_position:
            close_all_positions(SYMBOL)
            virtual_balance.close_position(price, reason="manual_close")
    else:
        return jsonify({'error':'Invalid action'}),400
    return jsonify({'success':True,'action':action, 'virtual_balance':virtual_balance.get_stats()})

# =====================
# STARTUP
# =====================
load_state()

if __name__=="__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT",5000)), debug=False)
