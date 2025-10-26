"""
Bitget TradingView Webhook Bot
Receives signals from TradingView and executes on Bitget Futures
"""

from flask import Flask, request, jsonify
import hmac
import hashlib
import requests
import time
import json
import base64
import os
import threading
from datetime import datetime

app = Flask(__name__)

# ===================================
# CONFIGURATION - EDIT THESE
# ===================================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', 'bg_645ac59fdc8a6eb132299a049d8d1236')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', 'be21f86fb8e4c0b4a64d0ebbfb7ca1936d8e55099d288a8ebbb17cbc929451fd')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', 'Grrtrades')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

# Trading Settings
SYMBOL = os.environ.get('SYMBOL', 'LTCUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 9))
MARGIN_MODE = os.environ.get('MARGIN_MODE', 'isolated')

# Bitget API Endpoints
BASE_URL = "https://api.bitget.com"

# Keep-alive settings
RENDER_URL = os.environ.get('RENDER_URL', '')
KEEP_ALIVE = os.environ.get('KEEP_ALIVE', 'true').lower() == 'true'

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

def set_leverage(symbol, leverage, margin_mode):
    """Set leverage for symbol"""
    endpoint = "/api/mix/v1/account/setLeverage"
    params = {
        'symbol': symbol,
        'marginCoin': 'USDT',
        'leverage': leverage,
        'holdSide': 'long'
    }
    result = bitget_request("POST", endpoint, params)
    print(f"Set leverage to {leverage}x: {result}")
    
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

def place_order(symbol, side, size):
    """
    Place market order on Bitget
    side: 'open_long', 'close_long', 'open_short', 'close_short'
    size: quantity in contracts (LTC)
    """
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
                print(f"Closed {pos['holdSide']} position: {size} contracts")

# ===================================
# WEBHOOK ENDPOINT
# ===================================

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        return jsonify({"status": "Webhook endpoint live"}), 200
    
    """Receive TradingView webhook with position size"""
    try:
        # TradingView sends data as plain text, not JSON with proper headers
        # We need to parse it manually
        
        # Get raw data
        raw_data = request.get_data(as_text=True)
        print(f"\n[RAW] Received webhook data: {raw_data}")
        
        # Try to parse as JSON
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            print(f"❌ Failed to parse JSON from: {raw_data}")
            return jsonify({'error': 'Invalid JSON format'}), 400
        
        print(f"[PARSED] Data: {json.dumps(data, indent=2)}")
        
        # Verify secret (security)
        if data.get('secret') != WEBHOOK_SECRET:
            print(f"❌ Invalid secret: got '{data.get('secret')}', expected '{WEBHOOK_SECRET}'")
            return jsonify({'error': 'Invalid secret'}), 401
        
        action = data.get('action', '').upper()
        tv_qty = float(data.get('qty', 0))
        leverage_from_tv = data.get('leverage', LEVERAGE)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"\n[{timestamp}] ═══════════════════════════════")
        print(f"Received signal: {action}")
        print(f"TradingView Qty: {tv_qty} LTC")
        print(f"Leverage: {leverage_from_tv}x")
        
        # Validate qty for BUY/SELL (CLOSE can have qty=0)
        if action in ['BUY', 'SELL', 'LONG', 'SHORT'] and tv_qty <= 0:
            return jsonify({'error': 'Invalid qty from TradingView'}), 400
        
        # Get current price
        price = get_current_price(SYMBOL)
        if not price:
            return jsonify({'error': 'Could not fetch price'}), 500
        
        print(f"Current {SYMBOL} price: ${price}")
        
        # Execute trade based on action
        if action == 'BUY' or action == 'LONG':
            # Apply 95% safety buffer (for fees/slippage)
            safe_qty = tv_qty * 0.95
            quantity = round(safe_qty, 1)
            quantity = max(quantity, 0.1)
            
            print(f"Safe Qty (95%): {safe_qty} LTC")
            print(f"Bitget Quantity: {quantity} LTC")
            print(f"Position Value: ${quantity * price:.2f}")
            
            # Close any short positions first
            close_all_positions(SYMBOL)
            # Open long position
            result = place_order(SYMBOL, 'open_long', quantity)
            print(f"✅ LONG order placed: {quantity} LTC")
            
        elif action == 'SELL' or action == 'SHORT':
            # Apply 95% safety buffer
            safe_qty = tv_qty * 0.95
            quantity = round(safe_qty, 1)
            quantity = max(quantity, 0.1)
            
            print(f"Safe Qty (95%): {safe_qty} LTC")
            print(f"Bitget Quantity: {quantity} LTC")
            print(f"Position Value: ${quantity * price:.2f}")
            
            # Close any long positions first
            close_all_positions(SYMBOL)
            # Open short position
            result = place_order(SYMBOL, 'open_short', quantity)
            print(f"✅ SHORT order placed: {quantity} LTC")
            
        elif action == 'CLOSE':
            # Close all positions
            close_all_positions(SYMBOL)
            result = {'code': '00000', 'msg': 'Positions closed'}
            print(f"✅ All positions closed")
            quantity = 0
            
        else:
            return jsonify({'error': f'Invalid action: {action}'}), 400
        
        print(f"═══════════════════════════════\n")
        
        return jsonify({
            'success': True,
            'action': action,
            'symbol': SYMBOL,
            'tradingview_qty': tv_qty,
            'executed_qty': quantity if action != 'CLOSE' else 0,
            'price': price,
            'result': result,
            'timestamp': timestamp
        })
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'running',
        'exchange': 'Bitget',
        'symbol': SYMBOL,
        'leverage': LEVERAGE,
        'margin_mode': MARGIN_MODE,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/status', methods=['GET'])
def status():
    """Check current positions"""
    try:
        positions = get_positions(SYMBOL)
        price = get_current_price(SYMBOL)
        
        return jsonify({
            'price': price,
            'positions': positions,
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ===================================
# MAIN
# ===================================

if __name__ == '__main__':
    print("="*50)
    print("Bitget TradingView Webhook Bot Started")
    print("="*50)
    print(f"Exchange: Bitget")
    print(f"Symbol: {SYMBOL}")
    print(f"Leverage: {LEVERAGE}x")
    print(f"Margin Mode: {MARGIN_MODE}")
    print(f"Webhook Secret: {WEBHOOK_SECRET}")
    print("="*50)
    
    # Set leverage and margin mode on startup
    set_leverage(SYMBOL, LEVERAGE, MARGIN_MODE)
    set_margin_mode(SYMBOL, MARGIN_MODE)
    
    # Run Flask server
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False)