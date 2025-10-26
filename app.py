"""
MEXC TradingView Webhook Bot
Free alternative to PineConnector
"""

from flask import Flask, request, jsonify
import hmac
import hashlib
import requests
import time
import json
from datetime import datetime

app = Flask(__name__)

# ===================================
# CONFIGURATION - EDIT THESE
# ===================================
MEXC_API_KEY = "mx0vglHSkvmiJIWAbA"
MEXC_SECRET_KEY = "d73c9164f5ce40b192a38e804f48fe06"
WEBHOOK_SECRET = "Grrtrades"  # Set a random password

# Trading Settings
SYMBOL = "LTC_USDT"  # MEXC futures format
LEVERAGE = 9
POSITION_SIZE_USDT = 20  # How much USDT per trade

# MEXC API Endpoints
BASE_URL = "https://contract.mexc.com"

# ===================================
# MEXC API FUNCTIONS
# ===================================

def generate_signature(params, secret):
    """Generate MEXC API signature"""
    query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = hmac.new(
        secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

def mexc_request(method, endpoint, params=None):
    """Make request to MEXC API"""
    if params is None:
        params = {}
    
    # Add timestamp
    params['timestamp'] = int(time.time() * 1000)
    
    # Generate signature
    params['signature'] = generate_signature(params, MEXC_SECRET_KEY)
    
    headers = {
        'ApiKey': MEXC_API_KEY,
        'Content-Type': 'application/json'
    }
    
    url = f"{BASE_URL}{endpoint}"
    
    try:
        if method == "GET":
            response = requests.get(url, params=params, headers=headers, timeout=10)
        elif method == "POST":
            response = requests.post(url, json=params, headers=headers, timeout=10)
        else:
            return None
        
        return response.json()
    except Exception as e:
        print(f"API Error: {e}")
        return None

def set_leverage(symbol, leverage):
    """Set leverage for symbol"""
    endpoint = "/api/v1/private/position/change_leverage"
    params = {
        'symbol': symbol,
        'leverage': leverage,
        'openType': 1  # Isolated margin
    }
    result = mexc_request("POST", endpoint, params)
    print(f"Set leverage to {leverage}x: {result}")
    return result

def get_current_price(symbol):
    """Get current market price"""
    endpoint = "/api/v1/contract/ticker"
    params = {'symbol': symbol}
    
    try:
        response = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=10)
        data = response.json()
        if data.get('success') and data.get('data'):
            return float(data['data'][0]['lastPrice'])
    except Exception as e:
        print(f"Price fetch error: {e}")
    return None

def place_order(symbol, side, quantity):
    """
    Place market order on MEXC
    side: 1 = Open Long, 2 = Close Long, 3 = Open Short, 4 = Close Short
    """
    endpoint = "/api/v1/private/order/submit"
    
    params = {
        'symbol': symbol,
        'price': 0,  # Market order
        'vol': quantity,
        'side': side,
        'type': 5,  # Market order type
        'openType': 1,  # Isolated margin
        'leverage': LEVERAGE
    }
    
    result = mexc_request("POST", endpoint, params)
    print(f"Order result: {result}")
    return result

def close_all_positions(symbol):
    """Close all open positions for symbol"""
    # Get current position
    endpoint = "/api/v1/private/position/open_positions"
    params = {'symbol': symbol}
    positions = mexc_request("GET", endpoint, params)
    
    if positions and positions.get('success'):
        for pos in positions.get('data', []):
            if float(pos.get('holdVol', 0)) > 0:
                side = 2 if pos['positionType'] == 1 else 4  # 2=Close Long, 4=Close Short
                quantity = abs(float(pos['holdVol']))
                place_order(symbol, side, quantity)
                print(f"Closed position: {pos['positionType']} {quantity}")

def calculate_position_size(price, position_value_usdt):
    """Calculate position size in contracts"""
    # MEXC uses contracts, need to calculate based on contract value
    # For LTC, 1 contract typically = 0.01 LTC
    contracts = int((position_value_usdt / price) * 100)  # Convert to contracts
    return max(contracts, 1)  # Minimum 1 contract

# ===================================
# WEBHOOK ENDPOINT
# ===================================

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive TradingView webhook with position size"""
    try:
        data = request.json
        
        # Verify secret (security)
        if data.get('secret') != WEBHOOK_SECRET:
            return jsonify({'error': 'Invalid secret'}), 401
        
        action = data.get('action', '').upper()
        tv_qty = float(data.get('qty', 0))  # Qty from TradingView
        leverage_from_tv = data.get('leverage', LEVERAGE)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        print(f"\n[{timestamp}] ═══════════════════════════════")
        print(f"Received signal: {action}")
        print(f"TradingView Qty: {tv_qty} LTC")
        print(f"Leverage: {leverage_from_tv}x")
        print(f"Full data: {json.dumps(data, indent=2)}")
        
        # Validate qty
        if tv_qty <= 0:
            return jsonify({'error': 'Invalid qty from TradingView'}), 400
        
        # Apply 95% safety buffer (for fees/slippage)
        safe_qty = tv_qty * 0.95
        print(f"Safe Qty (95%): {safe_qty} LTC")
        
        # Get current price
        price = get_current_price(SYMBOL)
        if not price:
            return jsonify({'error': 'Could not fetch price'}), 500
        
        print(f"Current {SYMBOL} price: ${price}")
        
        # Convert to MEXC contracts (1 contract = 0.01 LTC typically)
        contracts = int(safe_qty * 100)
        contracts = max(contracts, 1)  # Minimum 1 contract
        
        print(f"MEXC Contracts: {contracts} (~{contracts/100} LTC)")
        print(f"Position Value: ${(contracts/100) * price:.2f}")
        
        # Execute trade based on action
        if action == 'BUY' or action == 'LONG':
            # Close any short positions first
            close_all_positions(SYMBOL)
            # Open long position
            result = place_order(SYMBOL, 1, contracts)  # 1 = Open Long
            print(f"✅ LONG order placed: {contracts} contracts")
            
        elif action == 'SELL' or action == 'SHORT':
            # Close any long positions first
            close_all_positions(SYMBOL)
            # Open short position
            result = place_order(SYMBOL, 3, contracts)  # 3 = Open Short
            print(f"✅ SHORT order placed: {contracts} contracts")
            
        elif action == 'CLOSE':
            # Close all positions
            close_all_positions(SYMBOL)
            result = {'success': True, 'message': 'Positions closed'}
            print(f"✅ All positions closed")
            
        else:
            return jsonify({'error': 'Invalid action'}), 400
        
        print(f"═══════════════════════════════\n")
        
        return jsonify({
            'success': True,
            'action': action,
            'symbol': SYMBOL,
            'tradingview_qty': tv_qty,
            'safe_qty': safe_qty,
            'contracts': contracts,
            'price': price,
            'position_value': (contracts/100) * price,
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
        'symbol': SYMBOL,
        'leverage': LEVERAGE,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/status', methods=['GET'])
def status():
    """Check current positions"""
    try:
        endpoint = "/api/v1/private/position/open_positions"
        params = {'symbol': SYMBOL}
        positions = mexc_request("GET", endpoint, params)
        
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
    print("MEXC TradingView Webhook Bot Started")
    print("="*50)
    print(f"Symbol: {SYMBOL}")
    print(f"Leverage: {LEVERAGE}x")
    print(f"Position Size: ${POSITION_SIZE_USDT} USDT")
    print(f"Webhook URL: http://YOUR_IP:5000/webhook")
    print("="*50)
    
    # Set leverage on startup
    set_leverage(SYMBOL, LEVERAGE)
    
    # Run Flask server
    # For production, use: gunicorn -w 1 -b 0.0.0.0:5000 bot:app
    app.run(host='0.0.0.0', port=5000, debug=False)