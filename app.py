"""
Patched Bitget TradingView Webhook Bot - Virtual Balance Tracking
Changes:
- Fixed Render timeout: Flask starts immediately, API calls deferred until first order
- All other functionality preserved
"""

from flask import Flask, request, jsonify
import hmac
import hashlib
import requests
import time
import json
import base64
import os
from datetime import datetime
import threading

app = Flask(__name__)

# ===================================
# CONFIGURATION
# ===================================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

SYMBOL = os.environ.get('SYMBOL', 'LTCUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 9))
MARGIN_MODE = os.environ.get('MARGIN_MODE', 'cross')
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE', 95.0))
STARTING_BALANCE = float(os.environ.get('STARTING_BALANCE', 5.0))
LIVE_MODE = os.environ.get('LIVE_MODE', 'False').lower() in ['true', '1', 'yes']
STATE_FILE = os.environ.get('STATE_FILE', 'vb_state.json')
DEBOUNCE_SEC = float(os.environ.get('DEBOUNCE_SEC', 2.0))
last_signal_time = 0

BASE_URL = "https://api.bitget.com"

# ===================================
# VIRTUAL BALANCE TRACKING
# ===================================
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
        self.current_position = {
            'side': side,
            'entry_price': entry_price,
            'qty': qty,
            'open_time': datetime.now().isoformat()
        }
        print(f"📝 Position recorded: {side} {qty} @ ${entry_price}")
        save_state()

    def close_position(self, exit_price):
        if not self.current_position:
            print("⚠️ No position to close")
            return 0
        side = self.current_position['side']
        entry_price = self.current_position['entry_price']
        qty = self.current_position['qty']

        price_change = (exit_price - entry_price) / entry_price if side == 'long' else (entry_price - exit_price) / entry_price
        position_value = qty * entry_price
        pnl = position_value * price_change * LEVERAGE

        self.current_balance += pnl
        self.total_pnl += pnl
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1

        trade_record = {
            'side': side,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'qty': qty,
            'pnl': pnl,
            'balance_after': self.current_balance,
            'close_time': datetime.now().isoformat()
        }
        self.trade_history.append(trade_record)

        print(f"💰 Position closed: Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f} | P&L: ${pnl:+.2f} | New Balance: ${self.current_balance:.2f}")

        self.current_position = None
        save_state()
        return pnl

    def get_stats(self):
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        roi = ((self.current_balance - self.starting_balance) / self.starting_balance * 100)
        return {
            'starting_balance': self.starting_balance,
            'current_balance': self.current_balance,
            'total_pnl': self.total_pnl,
            'roi_percent': roi,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': win_rate,
            'has_open_position': self.current_position is not None
        }

virtual_balance = VirtualBalance(STARTING_BALANCE)

# ===================================
# PERSISTENCE
# ===================================
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
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"❌ Failed to save state: {e}")

def load_state():
    global virtual_balance
    try:
        with open(STATE_FILE, 'r') as f:
            st = json.load(f)
        vb = VirtualBalance(st.get('starting_balance', STARTING_BALANCE))
        vb.current_balance = st.get('current_balance', STARTING_BALANCE)
        vb.total_trades = st.get('total_trades', 0)
        vb.winning_trades = st.get('winning_trades', 0)
        vb.losing_trades = st.get('losing_trades', 0)
        vb.total_pnl = st.get('total_pnl', 0.0)
        vb.trade_history = st.get('trade_history', [])
        vb.current_position = st.get('current_position', None)
        virtual_balance = vb
        print("✅ Loaded saved virtual balance.")
    except FileNotFoundError:
        print("ℹ️ No saved state found, using fresh virtual balance.")
    except Exception as e:
        print(f"❌ Failed to load state: {e}")

# ===================================
# BITGET API FUNCTIONS
# ===================================
def generate_signature(timestamp, method, request_path, body, secret):
    body_str = json.dumps(body) if body else ""
    message = timestamp + method + request_path + body_str
    mac = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None):
    timestamp = str(int(time.time() * 1000))
    body = params if params else None
    sign = generate_signature(timestamp, method, endpoint, body, BITGET_SECRET_KEY)
    headers = {
        'ACCESS-KEY': BITGET_API_KEY,
        'ACCESS-SIGN': sign,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
        'Content-Type': 'application/json',
        'locale': 'en-US'
    }
    url = BASE_URL + endpoint
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            response = requests.post(url, json=body, headers=headers, timeout=10)
        else:
            return None
        try:
            return response.json()
        except Exception:
            return {'error': 'no-json-response', 'text': response.text}
    except Exception as e:
        print(f"API Error: {e}")
        return None

def set_leverage(symbol, leverage):
    endpoint = "/api/mix/v1/account/setLeverage"
    params = {'symbol': symbol, 'marginCoin': 'USDT', 'leverage': leverage, 'holdSide': 'long'}
    result = bitget_request("POST", endpoint, params)
    params['holdSide'] = 'short'
    result2 = bitget_request("POST", endpoint, params)
    print(f"Set leverage: long={result}, short={result2}")
    return result

def set_margin_mode(symbol, margin_mode):
    endpoint = "/api/mix/v1/account/setMarginMode"
    params = {'symbol': symbol, 'marginCoin': 'USDT', 'marginMode': margin_mode}
    result = bitget_request("POST", endpoint, params)
    print(f"Set margin mode: {result}")
    return result

def get_current_price(symbol):
    endpoint = f"/api/mix/v1/market/ticker?symbol={symbol}"
    try:
        response = requests.get(BASE_URL + endpoint, timeout=10)
        data = response.json()
        if data.get('code') == '00000' and data.get('data'):
            return float(data['data']['last'])
    except Exception as e:
        print(f"Price fetch error: {e}")
    return None

def calculate_position_size(balance, price, leverage, risk_pct=95.0):
    usable_balance = balance * (risk_pct / 100.0)
    position_value = usable_balance * leverage
    quantity = round(position_value / price, 3)
    quantity = max(quantity, 0.001)
    quantity = min(quantity, 1_000_000)
    print(f"📊 Position Calculation: Balance=${balance:.2f}, Qty={quantity}")
    return quantity

def place_order(symbol, side, size):
    if not LIVE_MODE:
        print(f"[SIM] place_order -> symbol={symbol}, side={side}, size={size}")
        return {'code': '00000', 'data': {'orderId': 'SIM123'}}
    endpoint = "/api/mix/v1/order/placeOrder"
    holdSide = 'long' if 'long' in side else 'short' if 'short' in side else None
    params = {'symbol': symbol, 'marginCoin': 'USDT', 'side': side, 'orderType': 'market', 'size': str(size)}
    if holdSide:
        params['holdSide'] = holdSide
    result = bitget_request("POST", endpoint, params)
    print(f"Order result: {result}")
    return result

def get_positions(symbol):
    endpoint = f"/api/mix/v1/position/singlePosition?symbol={symbol}&marginCoin=USDT"
    return bitget_request("GET", endpoint)

def close_all_positions(symbol):
    if not LIVE_MODE:
        print(f"[SIM] close_all_positions({symbol})")
        return {'code': '00000'}
    positions = get_positions(symbol)
    if positions and positions.get('code') == '00000':
        for pos in positions.get('data', []):
            if float(pos.get('total', 0)) > 0:
                side = 'close_long' if pos['holdSide'] == 'long' else 'close_short'
                size = abs(float(pos['total']))
                place_order(symbol, side, size)
                print(f"✅ Closed {pos['holdSide']} position: {size} contracts")

# ===================================
# WEBHOOK HANDLER
# ===================================
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    global last_signal_time
    if request.method == 'GET':
        return jsonify({"status": "Webhook endpoint live", "mode": "VIRTUAL_BALANCE_TRACKING", **virtual_balance.get_stats()}), 200

    raw_data = request.get_data(as_text=True)
    try:
        data = json.loads(raw_data)
    except:
        return jsonify({'error': 'Invalid JSON'}), 400
    if data.get('secret') != WEBHOOK_SECRET:
        return jsonify({'error': 'Invalid secret'}), 401

    action = data.get('action', '').upper()
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n[{timestamp}] 🎯 Signal received: {action}")

    price = get_current_price(SYMBOL)
    if not price:
        return jsonify({'error': 'Price fetch failed'}), 500

    # Prevent duplicate same-side orders
    if virtual_balance.current_position:
        current_side = virtual_balance.current_position['side']
        if action in ['BUY', 'LONG'] and current_side == 'long':
            return jsonify({'success': True, 'action': 'ignored', 'reason': 'already_long'})
        if action in ['SELL', 'SHORT'] and current_side == 'short':
            return jsonify({'success': True, 'action': 'ignored', 'reason': 'already_short'})
        # Close current before switching
        virtual_balance.close_position(price)
        close_all_positions(SYMBOL)
        time.sleep(0.4)

    # Setup leverage/margin if LIVE_MODE
    if LIVE_MODE:
        set_margin_mode(SYMBOL, MARGIN_MODE)
        set_leverage(SYMBOL, LEVERAGE)

    # Execute order
    if action in ['BUY', 'LONG']:
        qty = calculate_position_size(virtual_balance.current_balance, price, LEVERAGE, RISK_PERCENTAGE)
        place_order(SYMBOL, 'open_long', qty)
        virtual_balance.open_position('long', price, qty)
    elif action in ['SELL', 'SHORT']:
        qty = calculate_position_size(virtual_balance.current_balance, price, LEVERAGE, RISK_PERCENTAGE)
        place_order(SYMBOL, 'open_short', qty)
        virtual_balance.open_position('short', price, qty)
    elif action == 'CLOSE' and virtual_balance.current_position:
        pnl = virtual_balance.close_position(price)
        close_all_positions(SYMBOL)
    else:
        return jsonify({'error': f'Invalid action: {action}'}), 400

    return jsonify({'success': True, 'action': action, 'price': price, 'virtual_balance': virtual_balance.get_stats(), 'timestamp': timestamp})

# ===================================
# OTHER ENDPOINTS
# ===================================
@app.route('/health', methods=['GET'])
def health():
    stats = virtual_balance.get_stats()
    return jsonify({'status': 'running', 'symbol': SYMBOL, 'leverage': LEVERAGE, 'mode': 'VIRTUAL_BALANCE', **stats, 'timestamp': datetime.now().isoformat()})

@app.route('/stats', methods=['GET'])
def stats():
    stats = virtual_balance.get_stats()
    return jsonify({**stats, 'recent_trades': virtual_balance.trade_history[-10:], 'timestamp': datetime.now().isoformat()})

@app.route('/reset', methods=['POST'])
def reset():
    global virtual_balance
    data = request.get_json() or {}
    if data.get('secret') != WEBHOOK_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    old_stats = virtual_balance.get_stats()
    virtual_balance = VirtualBalance(STARTING_BALANCE)
    save_state()
    return jsonify({'success': True, 'message': 'Virtual balance reset', 'old_stats': old_stats, 'new_balance': STARTING_BALANCE})

# ===================================
# MAIN
# ===================================
if __name__ == '__main__':
    print("="*60)
    print("🚀 Bitget Bot - Virtual Balance Challenge Mode (Render-friendly)")
    print("="*60)
    print(f"Exchange: Bitget | Symbol: {SYMBOL} | Leverage: {LEVERAGE}x | Starting Balance: ${STARTING_BALANCE:.2f}")
    print("[INFO] Flask starting. LIVE_MODE API calls deferred until first webhook order.")
    print("="*60)
    load_state()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False)
