import os
import time
import hmac
import hashlib
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Load your API keys from environment variables (set in Render dashboard)
API_KEY = os.environ.get("MEXC_API_KEY")
SECRET_KEY = os.environ.get("MEXC_SECRET_KEY")

# === SETTINGS ===
BASE_URL = "https://api.mexc.com"  # Spot API endpoint
SYMBOL = "LTCUSDT"
TRADE_AMOUNT = 10  # USD value per trade
DEBUG_MODE = True  # Set False when live

# === HELPER: SIGN REQUEST ===
def sign_request(params: dict):
    query_string = '&'.join([f"{key}={params[key]}" for key in sorted(params)])
    signature = hmac.new(SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={signature}"

# === PLACE ORDER ===
def place_order(side: str):
    endpoint = "/api/v3/order"
    url = BASE_URL + endpoint

    timestamp = int(time.time() * 1000)
    params = {
        "symbol": SYMBOL,
        "side": side.upper(),
        "type": "MARKET",
        "quoteOrderQty": TRADE_AMOUNT,  # Spend this much USDT per trade
        "timestamp": timestamp,
    }

    signed_query = sign_request(params)
    headers = {"X-MEXC-APIKEY": API_KEY}

    if DEBUG_MODE:
        print(f"[DEBUG] Sending {side} order for ${TRADE_AMOUNT} on {SYMBOL}")

    response = requests.post(url, headers=headers, data=signed_query)
    print(f"[MEXC RESPONSE] {response.status_code} - {response.text}")
    return response.json()

# === MAIN ROUTE ===
@app.route('/paper_trade', methods=['POST'])
def paper_trade():
    data = request.get_json(force=True)
    print("Received webhook:", data)

    if not data or "action" not in data:
        return jsonify({"error": "Missing 'action' field"}), 400

    action = data["action"].upper()

    if action == "LONG":
        result = place_order("BUY")
    elif action == "SHORT":
        result = place_order("SELL")
    else:
        return jsonify({"error": "Invalid action"}), 400

    return jsonify({"status": "ok", "result": result}), 200

# === TEST ROUTE ===
@app.route('/', methods=['GET'])
def home():
    return "✅ MEXC Bot Webhook Active", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
