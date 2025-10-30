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
STATE_FILE_PATH = os.path.join(DATA_DIR, LOCAL_STATE_FILE) if os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK) else os.path.abspath(LOCAL_STATE_FILE)
if not os.path.exists(os.path.dirname(STATE_FILE_PATH)):
    os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
print(f"💾 Using state file at: {STATE_FILE_PATH}")

# =====================
# CONFIG
# =====================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY','')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY','')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE','')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET','Grrtrades')
SYMBOL = os.environ.get('SYMBOL','TAOUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE',11))
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE',30.0))
INITIAL_BALANCE = float(os.environ.get('INITIAL_BALANCE',20.0))
TAKE_PROFIT_PCT = float(os.environ.get('TAKE_PROFIT_PCT',1.3))
STOP_LOSS_PCT = float(os.environ.get('STOP_LOSS_PCT',0.75))
PHASE_1_THRESHOLD = float(os.environ.get('PHASE_1_THRESHOLD',2000.0))
PROFIT_RESET_THRESHOLD = 2.0  # 200% profit
MAX_DRAWDOWN_STOP = float(os.environ.get('MAX_DRAWDOWN_STOP',50.0))
DEBOUNCE_SEC = float(os.environ.get('DEBOUNCE_SEC',2.0))
POSITION_SYNC_INTERVAL = 30.0
PRICE_CHECK_INTERVAL = 1.0
MAX_PRICE_FAILURES = 5

# =====================
# LOGGING
# =====================
def log(msg, level='INFO'):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {msg}")

# =====================
# VIRTUAL BALANCE CLASS
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
        self.trading_paused = False
        self.reset_count = 0
        self.phase_1_resets = 0
        self.phase_2_resets = 0
        self.total_withdrawn = 0.0
        self.total_profit_generated = 0.0

    def get_current_phase(self):
        return 'growth' if self.starting_balance < PHASE_1_THRESHOLD else 'extraction'

    def check_auto_reset(self):
        if self.current_balance >= self.starting_balance * (1 + PROFIT_RESET_THRESHOLD):
            profit = self.current_balance - self.starting_balance
            phase = self.get_current_phase()
            if phase == 'growth':
                reinvest_pct = 1.0
                withdraw_amount = profit * (1 - reinvest_pct)
                self.phase_1_resets += 1
            else:
                reinvest_pct = 0.05
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
            log(f"🎉 Profit reset triggered, new balance: {new_starting}")

    def check_emergency_stop(self):
        if self.max_drawdown >= MAX_DRAWDOWN_STOP and not self.trading_paused:
            self.trading_paused = True
            log("🚨 Emergency stop triggered! Trading paused.", 'ERROR')

    def open_position(self, side, entry_price, qty):
        with self.position_lock:
            if self.current_position: return False
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT/100) if side=='long' else entry_price * (1 - TAKE_PROFIT_PCT/100)
            sl_price = entry_price * (1 - STOP_LOSS_PCT/100) if side=='long' else entry_price * (1 + STOP_LOSS_PCT/100)
            self.current_position = {'side': side, 'entry_price': entry_price, 'qty': qty, 'tp_price': tp_price, 'sl_price': sl_price, 'open_time': datetime.now().isoformat()}
            self._start_monitoring()
            save_state()
            log(f"Opened {side} position @ {entry_price} for {qty}")
            return True

    def _start_monitoring(self):
        self.stop_monitoring.clear()
        if not self.monitor_thread or not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(target=self.monitor_position, daemon=True)
            self.monitor_thread.start()

    def monitor_position(self):
        consecutive_failures = 0
        while not self.stop_monitoring.is_set():
            with self.position_lock:
                if not self.current_position: break
                price = get_current_price(SYMBOL)
                if not price:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_PRICE_FAILURES: break
                    time.sleep(PRICE_CHECK_INTERVAL)
                    continue
                side = self.current_position['side']
                if (side=='long' and price >= self.current_position['tp_price']) or (side=='short' and price <= self.current_position['tp_price']):
                    self.close_position(price,'TP')
                    break
                if (side=='long' and price <= self.current_position['sl_price']) or (side=='short' and price >= self.current_position['sl_price']):
                    self.close_position(price,'SL')
                    break
            time.sleep(PRICE_CHECK_INTERVAL)

    def close_position(self, exit_price, reason='normal'):
        with self.position_lock:
            if not self.current_position: return
            side = self.current_position['side']
            entry_price = self.current_position['entry_price']
            qty = self.current_position['qty']
            pnl = qty * entry_price * ((exit_price - entry_price)/entry_price if side=='long' else (entry_price - exit_price)/entry_price)
            self.current_balance += pnl
            self.total_pnl += pnl
            self.total_trades += 1
            if pnl>0: self.winning_trades +=1
            else: self.losing_trades +=1
            self.current_position = None
            save_state()
            log(f"Closed {side} position, PnL: {pnl}, reason: {reason}")
            self.check_auto_reset()
            self.check_emergency_stop()

    def should_trade(self):
        return not self.trading_paused

virtual_balance = VirtualBalance(INITIAL_BALANCE)

# =====================
# STATE
# =====================
def save_state():
    try:
        state = virtual_balance.__dict__.copy()
        state['current_position'] = virtual_balance.current_position
        with open(STATE_FILE_PATH,'w') as f:
            json.dump(state,f,indent=2)
    except Exception as e:
        log(f"Failed to save state: {e}", 'ERROR')

def load_state():
    try:
        with open(STATE_FILE_PATH,'r') as f:
            st=json.load(f)
        for k,v in st.items():
            setattr(virtual_balance,k,v)
    except FileNotFoundError:
        log("No saved state found, starting fresh")

load_state()

# =====================
# BITGET API
# =====================
def generate_signature(timestamp, method, request_path, body, secret):
    body_str = json.dumps(body) if body else ''
    message = timestamp+method+request_path+body_str
    return base64.b64encode(hmac.new(secret.encode(),message.encode(),hashlib.sha256).digest()).decode()

def bitget_request(method, endpoint, params=None):
    timestamp=str(int(time.time()*1000))
    body=params if params else None
    sign=generate_signature(timestamp,method,endpoint,body,BITGET_SECRET_KEY)
    headers={'ACCESS-KEY':BITGET_API_KEY,'ACCESS-SIGN':sign,'ACCESS-TIMESTAMP':timestamp,'ACCESS-PASSPHRASE':BITGET_PASSPHRASE,'Content-Type':'application/json'}
    url=BASE_URL+endpoint
    r = requests.post(url,json=body,headers=headers,timeout=10) if method=='POST' else requests.get(url,headers=headers,timeout=10)
    return r.json()

def get_current_price(symbol):
    try:
        data=requests.get(BASE_URL+f"/api/mix/v1/market/ticker?symbol={symbol}",timeout=5).json()
        return float(data['data']['last']) if data.get('code')=='00000' else None
    except:
        return None

# =====================
# WEBHOOK
# =====================
last_signal_time = 0
@app.route('/webhook',methods=['POST','GET'])
def webhook():
    global last_signal_time
    if request.method=='GET': return jsonify({'virtual_balance':virtual_balance.__dict__,'uptime':datetime.now().isoformat()})
    data=json.loads(request.get_data(as_text=True))
    if data.get('secret')!=WEBHOOK_SECRET: return jsonify({'error':'Unauthorized'}),401
    now=time.time()
    if now-last_signal_time<DEBOUNCE_SEC: return jsonify({'success':True,'action':'debounced'})
    last_signal_time=now
    if not virtual_balance.should_trade(): return jsonify({'success':False,'reason':'paused'})
    action=data.get('action','').upper()
    price=get_current_price(SYMBOL)
    if not price: return jsonify({'error':'Price fetch failed'}),500
    with virtual_balance.position_lock:
        if virtual_balance.current_position: virtual_balance.close_position(price,'signal_flip')
        qty = virtual_balance.current_balance * (RISK_PERCENTAGE/100) * LEVERAGE / price
        virtual_balance.open_position('long' if action in ['BUY','LONG'] else 'short',price,qty)
    return jsonify({'success':True,'action':action,'virtual_balance':virtual_balance.__dict__})

# =====================
# MAIN
# =====================
if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)