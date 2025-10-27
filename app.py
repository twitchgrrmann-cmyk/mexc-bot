"""
Bitget TradingView Webhook Bot - Virtual Balance Tracking
Starts with specified amount and compounds internally
Tracks P&L and calculates position sizes from virtual balance
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

app = Flask(__name__)

# ===================================
# CONFIGURATION
# ===================================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

# Trading Settings
SYMBOL = os.environ.get('SYMBOL', 'LTCUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 7))
MARGIN_MODE = os.environ.get('MARGIN_MODE', 'isolated')
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE', 95.0))

# Virtual Balance Settings
STARTING_BALANCE = float(os.environ.get('STARTING_BALANCE', 5.0))  # Start with $5 (or whatever you set)

# Bitget API Endpoints
BASE_URL = "https://api.bitget.com"

# ===================================
# VIRTUAL BALANCE TRACKING
# ===================================
class VirtualBalance:
    """Tracks bot's virtual balance and P&L"""
    
    def __init__(self, starting_balance):
        self.starting_balance = starting_balance
        self.current_balance = starting_balance
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = 0.0
        self.current_position = None  # {'side': 'long/short', 'entry_price': X, 'qty': Y}
        self.trade_history = []
    
    def open_position(self, side, entry_price, qty):
        """Record position opening"""
        self.current_position = {
            'side': side,
            'entry_price': entry_price,
            'qty': qty,
            'open_time': datetime.now().isoformat()
        }
        print(f"📝 Position recorded: {side} {qty} @ ${entry_price}")
    
    def close_position(self, exit_price):
        """Calculate P&L and update balance"""
        if not self.current_position:
            print("⚠️ No position to close")
            return 0
        
        side = self.current_position['side']
        entry_price = self.current_position['entry_price']
        qty = self.current_position['qty']
        
        # Calculate P&L
        if side == 'long':
            price_change = (exit_price - entry_price) / entry_price
        else:  # short
            price_change = (entry_price - exit_price) / entry_price
        
        position_value = qty * entry_price
        pnl = position_value * price_change * LEVERAGE
        
        # Update balance
        self.current_balance += pnl
        self.total_pnl += pnl
        self.total_trades += 1
        
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        
        # Record trade
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
        
        print(f"💰 Position closed:")
        print(f"   Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f}")
        print(f"   P&L: ${pnl:+.2f}")
        print(f"   New Balance: ${self.current_balance:.2f}")
        
        self.current_position = None
        return pnl
    
    def get_stats(self):
        """Get current statistics"""
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

# Initialize virtual balance
virtual_balance = VirtualBalance(STARTING_BALANCE)

# ===================================
# BITGET API FUNCTIONS
# ===================================

def generate_signature(timestamp, method, request_path, body, secret):
    """Generate Bitget API signature"""
    if body:
        body_str = json.dumps(body)
    else:
        body_str = ""
    
    message = timestamp + method + request_path + body_str
    mac = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None):
    """Make authenticated request to Bitget API"""
    timestamp = str(int(time.time() * 1000))
    request_path = endpoint
    
    if params:
        body = params
    else:
        body = None
    
    sign = generate_signature(timestamp, method, request_path, body, BITGET_SECRET_KEY)
    
    headers = {
        'ACCESS-KEY': BITGET_API_KEY,
        'ACCESS-SIGN': sign,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': BITGET_PASSPHRASE,
        'Content-Type': 'application/json',
        'locale': 'en-US'
    }
    
    url = BASE_URL + request_path
    
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            response = requests.post(url, json=body, headers=headers, timeout=10)
        else:
            return None
        
        return response.json()
    except Exception as e:
        print(f"API Error: {e}")
        return None

def set_leverage(symbol, leverage):
    """Set leverage for symbol"""
    endpoint = "/api/mix/v1/account/setLeverage"
    params = {
        'symbol': symbol,
        'marginCoin': 'USDT',
        'leverage': leverage,
        'holdSide': 'long'
    }
    result = bitget_request("POST", endpoint, params)
    print(f"Set leverage (long) to {leverage}x: {result}")
    
    params['holdSide'] = 'short'
    result2 = bitget_request("POST", endpoint, params)
    print(f"Set leverage (short) to {leverage}x: {result2}")
    return result

def set_margin_mode(symbol, margin_mode):
    """Set margin mode (isolated or crossed)"""
    endpoint = "/api/mix/v1/account/setMarginMode"
    params = {
        'symbol': symbol,
        'marginCoin': 'USDT',
        'marginMode': margin_mode
    }
    result = bitget_request("POST", endpoint, params)
    print(f"Set margin mode to {margin_mode}: {result}")
    return result

def get_current_price(symbol):
    """Get current market price"""
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
    """
    Calculate position size based on virtual balance
    
    Args:
        balance: Virtual balance
        price: Current price of asset
        leverage: Leverage multiplier
        risk_pct: Percentage of balance to use
    
    Returns:
        Position size in coins
    """
    # Use risk percentage of balance
    usable_balance = balance * (risk_pct / 100.0)
    
    # Calculate position value with leverage
    position_value = usable_balance * leverage
    
    # Calculate quantity in coins
    quantity = position_value / price
    
    # Round to 1 decimal
    quantity = round(quantity, 1)
    
    # Ensure minimum
    quantity = max(quantity, 0.1)
    
    print(f"📊 Position Calculation:")
    print(f"   Virtual Balance: ${balance:.2f}")
    print(f"   Usable ({risk_pct}%): ${usable_balance:.2f}")
    print(f"   Leverage: {leverage}x")
    print(f"   Position Value: ${position_value:.2f}")
    print(f"   Quantity: {quantity} coins")
    
    return quantity

def place_order(symbol, side, size):
    """Place market order on Bitget"""
    endpoint = "/api/mix/v1/order/placeOrder"
    
    params = {
        'symbol': symbol,
        'marginCoin': 'USDT',
        'side': side,
        'orderType': 'market',
        'size': str(size),
        'timeInForceValue': 'normal'
    }
    
    result = bitget_request("POST", endpoint, params)
    print(f"Order result: {result}")
    return result

def get_positions(symbol):
    """Get current positions"""
    endpoint = f"/api/mix/v1/position/singlePosition?symbol={symbol}&marginCoin=USDT"
    result = bitget_request("GET", endpoint)
    return result

def close_all_positions(symbol):
    """Close all open positions for symbol"""
    positions = get_positions(symbol)
    
    if positions and positions.get('code') == '00000':
        data = positions.get('data', [])
        for pos in data:
            if float(pos.get('total', 0)) > 0:
                side = 'close_long' if pos['holdSide'] == 'long' else 'close_short'
                size = abs(float(pos['total']))
                place_order(symbol, side, size)
                print(f"✅ Closed {pos['holdSide']} position: {size} contracts")

# ===================================
# WEBHOOK ENDPOINT
# ===================================

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        stats = virtual_balance.get_stats()
        return jsonify({
            "status": "Webhook endpoint live",
            "mode": "VIRTUAL_BALANCE_TRACKING",
            **stats
        }), 200
    
    """Receive TradingView webhook"""
    try:
        # Get raw data
        raw_data = request.get_data(as_text=True)
        print(f"\n[RAW] Received: {raw_data}")
        
        # Parse JSON
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            return jsonify({'error': 'Invalid JSON'}), 400
        
        # Verify secret
        if data.get('secret') != WEBHOOK_SECRET:
            return jsonify({'error': 'Invalid secret'}), 401
        
        action = data.get('action', '').upper()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"\n[{timestamp}] ═══════════════════════════════")
        print(f"🎯 Signal: {action}")
        
        # Get current stats
        stats = virtual_balance.get_stats()
        print(f"💰 Virtual Balance: ${stats['current_balance']:.2f}")
        print(f"📈 Total P&L: ${stats['total_pnl']:+.2f} ({stats['roi_percent']:+.1f}%)")
        print(f"📊 Trades: {stats['total_trades']} (W:{stats['winning_trades']} L:{stats['losing_trades']})")
        
        # Get current price
        price = get_current_price(SYMBOL)
        if not price:
            return jsonify({'error': 'Could not fetch price'}), 500
        
        print(f"💵 Current {SYMBOL} price: ${price:.2f}")
        
        # Execute trade
        if action == 'BUY' or action == 'LONG':
            # Close any existing position first (record P&L)
            if virtual_balance.current_position:
                virtual_balance.close_position(price)
                close_all_positions(SYMBOL)
            
            # Calculate new position size from virtual balance
            quantity = calculate_position_size(
                virtual_balance.current_balance,
                price,
                LEVERAGE,
                RISK_PERCENTAGE
            )
            
            # Execute on Bitget
            result = place_order(SYMBOL, 'open_long', quantity)
            
            # Record in virtual balance
            virtual_balance.open_position('long', price, quantity)
            
            print(f"✅ LONG opened: {quantity} @ ${price:.2f}")
            
        elif action == 'SELL' or action == 'SHORT':
            # Close any existing position first (record P&L)
            if virtual_balance.current_position:
                virtual_balance.close_position(price)
                close_all_positions(SYMBOL)
            
            # Calculate new position size from virtual balance
            quantity = calculate_position_size(
                virtual_balance.current_balance,
                price,
                LEVERAGE,
                RISK_PERCENTAGE
            )
            
            # Execute on Bitget
            result = place_order(SYMBOL, 'open_short', quantity)
            
            # Record in virtual balance
            virtual_balance.open_position('short', price, quantity)
            
            print(f"✅ SHORT opened: {quantity} @ ${price:.2f}")
            
        elif action == 'CLOSE':
            # Close position and record P&L
            if virtual_balance.current_position:
                pnl = virtual_balance.close_position(price)
                close_all_positions(SYMBOL)
                result = {'code': '00000', 'msg': 'Position closed', 'pnl': pnl}
                quantity = 0
                print(f"✅ Position closed with P&L: ${pnl:+.2f}")
            else:
                result = {'code': '00000', 'msg': 'No position to close'}
                quantity = 0
                print(f"ℹ️ No open position to close")
        else:
            return jsonify({'error': f'Invalid action: {action}'}), 400
        
        # Get updated stats
        final_stats = virtual_balance.get_stats()
        
        print(f"💰 New Balance: ${final_stats['current_balance']:.2f}")
        print(f"═══════════════════════════════\n")
        
        return jsonify({
            'success': True,
            'action': action,
            'symbol': SYMBOL,
            'price': price,
            'quantity': quantity if action != 'CLOSE' else 0,
            'result': result,
            'timestamp': timestamp,
            'virtual_balance': final_stats
        })
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check with stats"""
    stats = virtual_balance.get_stats()
    return jsonify({
        'status': 'running',
        'exchange': 'Bitget',
        'symbol': SYMBOL,
        'leverage': LEVERAGE,
        'mode': 'VIRTUAL_BALANCE',
        **stats,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/stats', methods=['GET'])
def stats():
    """Detailed statistics"""
    stats = virtual_balance.get_stats()
    return jsonify({
        **stats,
        'recent_trades': virtual_balance.trade_history[-10:],  # Last 10 trades
        'timestamp': datetime.now().isoformat()
    })

@app.route('/reset', methods=['POST'])
def reset():
    """Reset virtual balance (admin only)"""
    global virtual_balance
    
    # Get secret from request
    data = request.get_json() or {}
    if data.get('secret') != WEBHOOK_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    
    old_stats = virtual_balance.get_stats()
    virtual_balance = VirtualBalance(STARTING_BALANCE)
    
    return jsonify({
        'success': True,
        'message': 'Virtual balance reset',
        'old_stats': old_stats,
        'new_balance': STARTING_BALANCE
    })

# ===================================
# MAIN
# ===================================

if __name__ == '__main__':
    print("="*60)
    print("🚀 Bitget Bot - Virtual Balance Challenge Mode")
    print("="*60)
    print(f"Exchange: Bitget")
    print(f"Symbol: {SYMBOL}")
    print(f"Leverage: {LEVERAGE}x")
    print(f"💰 Starting Balance: ${STARTING_BALANCE:.2f}")
    print(f"📈 Goal: See how much you can grow it!")
    print(f"\n💡 Bot tracks its own P&L internally")
    print(f"💡 Compounds based on virtual balance")
    print(f"💡 Independent of actual Bitget balance")
    print("="*60)
    
    # Set leverage and margin mode
    set_leverage(SYMBOL, LEVERAGE)
    set_margin_mode(SYMBOL, MARGIN_MODE)
    
    print(f"\n✅ Bot ready - Starting balance: ${STARTING_BALANCE:.2f}")
    print(f"📊 Track stats at: /health or /stats endpoints\n")
    
    # Run Flask
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False)