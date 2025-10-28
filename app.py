from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime

app = Flask(__name__)

# =====================
# CONFIG
# =====================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

SYMBOL = os.environ.get('SYMBOL', 'LTCUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 9))
MARGIN_MODE = 'cross'
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE', 95.0))
STARTING_BALANCE = float(os.environ.get('STARTING_BALANCE', 5.0))
STATE_FILE = os.environ.get('STATE_FILE', 'vb_state.json')
DEBOUNCE_SEC = float(os.environ.get('DEBOUNCE_SEC', 2.0))
last_signal_time = 0

BASE_URL = "https://api.bitget.com"

LIVE_MODE = True  # Force live mode

# =====================
# VIRTUAL BALANCE
# =====================
class VirtualBalance:
    def __init__(self, starting_balance):
        self.starting_balance = starting_balance
        self.current_balance = starting_balance
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = 0.0
        self.current_position = None
        self.trade_history = []

    def open_position(self, side, entry_price, qty):
        self.current_position = {'side': side, 'entry_price': entry_price, 'qty': qty, 'open_time': datetime.now().isoformat()}
        save_state()
        print(f"📝 Opened {side} {qty} @ {entry_price}")

    def close_position(self, exit_price):
        if not self.current_position:
            return 0
        side = self.current_position['side']
        entry_price = self.current_position['entry_price']
        qty = self.current_position['qty']
        price_change = (exit_price - entry_price) / entry_price if side == 'long' else (entry_price - exit_price) / entry_price
        pnl = qty * entry_price * price_change * LEVERAGE
        self.current_balance += pnl
        self.total_pnl += pnl
        self.total_trades += 1
        if pnl > 0: self.winning_trades += 1
        else: self.losing_trades += 1
        self.trade_history.append({'side': side, 'entry_price': entry_price, 'exit_price': exit_price, 'qty': qty, 'pnl': pnl, 'balance_after': self.current_balance, 'close_time': datetime.now().isoformat()})
        self.current_position = None
        save_state()
        print(f"💰 Closed {side} P&L: {pnl:+.2f} | Balance: {self.current_balance:.2f}")
        return pnl

    def get_stats(self):
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades else 0
        roi = ((self.current_balance - self.starting_balance)/self.starting_balance*100)
        return {'starting_balance': self.starting_balance, 'current_balance': self.current_balance, 'total_pnl': self.total_pnl, 'roi_percent': roi, 'total_trades': self.total_trades, 'winning_trades': self.winning_trades, 'losing_trades': self.losing_trades, 'win_rate': win_rate, 'has_open_position': self.current_position is not None}

virtual_balance = VirtualBalance(STARTING_BALANCE)

# =====================
# STATE PERSISTENCE
# =====================
def save_state():
    try:
        state = {
            'starting_balance': virtual_balance.starting_balance,
            'current_balance': virtual_balance.current_balance,
            'total_trades': virtual_balance.total_trades,
            'winning_trades': virtual_balance.winning_trades,
            'losing_trades': virtual_balance.losing_trades,
            'total_pnl': virtual_balance.total_pnl,
            'trade_history': virtual_balance.trade_history,
            'current_position': virtual_balance.current_position
        }
        with open(STATE_FILE, 'w') as f: json.dump(state, f)
    except Exception as e: print(f"❌ Failed to save state: {e}")

def load_state():
    global virtual_balance
    try:
        with open(STATE_FILE, 'r') as f:
            st = json.load(f)
        vb = VirtualBalance(st.get('starting_balance', STARTING_BALANCE))
        vb.current_balance = st.get('current_balance', STARTING_BALANCE)
        vb.total_trades = st.get('total_trades',0)
        vb.winning_trades = st.get('winning_trades',0)
        vb.losing_trades = st.get('losing_trades',0)
        vb.total_pnl = st.get('total_pnl',0.0)
        vb.trade_history = st.get('trade_history',[])
        vb.current_position = st.get('current_position', None)
        virtual_balance = vb
        print("✅ Loaded saved virtual balance")
    except FileNotFoundError: print("ℹ️ No saved state found, starting fresh")
    except Exception as e: print(f"❌ Failed to load state: {e}")

# =====================
# BITGET API
# =====================
def generate_signature(timestamp, method, request_path, body, secret):
    body_str = json.dumps(body) if body else ""
    message = timestamp + method + request_path + body_str
    return base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()

def bitget_request(method, endpoint, params=None):
    timestamp = str(int(time.time()*1000))
    body = params if params else None
    sign = generate_signature(timestamp, method, endpoint, body, BITGET_SECRET_KEY)
    headers = {'ACCESS-KEY': BITGET_API_KEY, 'ACCESS-SIGN': sign, 'ACCESS-TIMESTAMP': timestamp, 'ACCESS-PASSPHRASE': BITGET_PASSPHRASE, 'Content-Type': 'application/json'}
    url = BASE_URL + endpoint
    try:
        r = requests.post(url,json=body,headers=headers,timeout=10) if method=="POST" else requests.get(url,headers=headers,timeout=10)
        return r.json()
    except: return {'error':'request_failed'}

def set_leverage(symbol, leverage):
    for side in ['long','short']:
        bitget_request("POST","/api/mix/v1/account/setLeverage",{'symbol':symbol,'marginCoin':'USDT','leverage':leverage,'holdSide':side})

def set_margin_mode(symbol, margin_mode):
    bitget_request("POST","/api/mix/v1/account/setMarginMode",{'symbol':symbol,'marginCoin':'USDT','marginMode':margin_mode})

def get_current_price(symbol):
    try:
        data = requests.get(BASE_URL+f"/api/mix/v1/market/ticker?symbol={symbol}",timeout=10).json()
        if data.get('code')=='00000' and data.get('data'): return float(data['data']['last'])
    except: pass
    return None

def calculate_position_size(balance, price, leverage, risk_pct):
    qty = round(balance * (risk_pct/100) * leverage / price,3)
    return max(qty,0.001)

def place_order(symbol, side, size):
    endpoint="/api/mix/v1/order/placeOrder"
    params={'symbol':symbol,'marginCoin':'USDT','side':side,'orderType':'market','size':str(size)}
    result = bitget_request("POST",endpoint,params)
    print(f"Order: {side} {size} -> {result}")
    return result

def get_positions(symbol):
    return bitget_request("GET",f"/api/mix/v1/position/singlePosition?symbol={symbol}&marginCoin=USDT")

def close_all_positions(symbol):
    pos = get_positions(symbol)
    if pos.get('code')=='00000':
        for p in pos.get('data',[]):
            if float(p.get('total',0))>0:
                side='close_long' if p['holdSide']=='long' else 'close_short'
                place_order(symbol,side,float(p['total']))
                print(f"✅ Closed {p['holdSide']} {p['total']}")

# =====================
# WEBHOOK HANDLER
# =====================
@app.route('/webhook',methods=['POST','GET'])
def webhook():
    global last_signal_time
    if request.method=='GET':
        return jsonify({"status":"live","virtual_balance":virtual_balance.get_stats()}),200
    raw_data=request.get_data(as_text=True)
    try: data=json.loads(raw_data)
    except: return jsonify({'error':'Invalid JSON'}),400
    if data.get('secret')!=WEBHOOK_SECRET: return jsonify({'error':'Unauthorized'}),401

    # Debounce
    now=time.time()
    if now-last_signal_time<DEBOUNCE_SEC: return jsonify({'success':True,'action':'debounced'})
    last_signal_time=now

    action=data.get('action','').upper()
    price=get_current_price(SYMBOL)
    if not price: return jsonify({'error':'Price fetch failed'}),500

    # Close opposite if exists
    if virtual_balance.current_position:
        if (action in ['BUY','LONG'] and virtual_balance.current_position['side']=='long') or \
           (action in ['SELL','SHORT'] and virtual_balance.current_position['side']=='short'):
            return jsonify({'success':True,'action':'ignored','reason':'already_in_position'})
        virtual_balance.close_position(price)
        close_all_positions(SYMBOL)
        time.sleep(0.3)

    # Ensure margin/leverage
    set_margin_mode(SYMBOL,MARGIN_MODE)
    set_leverage(SYMBOL,LEVERAGE)

    # Execute
    if action in ['BUY','LONG']:
        qty=calculate_position_size(virtual_balance.current_balance,price,LEVERAGE,RISK_PERCENTAGE)
        place_order(SYMBOL,'open_long',qty)
        virtual_balance.open_position('long',price,qty)
    elif action in ['SELL','SHORT']:
        qty=calculate_position_size(virtual_balance.current_balance,price,LEVERAGE,RISK_PERCENTAGE)
        place_order(SYMBOL,'open_short',qty)
        virtual_balance.open_position('short',price,qty)
    elif action=='CLOSE' and virtual_balance.current_position:
        virtual_balance.close_position(price)
        close_all_positions(SYMBOL)
    else: return jsonify({'error':f'Invalid action: {action}'}),400

    return jsonify({'success':True,'action':action,'price':price,'virtual_balance':virtual_balance.get_stats(),'timestamp':datetime.now().isoformat()})

# =====================
# MAIN
# =====================
if __name__=="__main__":
    print("🚀 Bitget Micro-Scalper Live Mode")
    load_state()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT",5000)),debug=False)
