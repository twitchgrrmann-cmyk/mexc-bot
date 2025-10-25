from flask import Flask, request, jsonify
import os
import time
import hmac
import hashlib
import requests

app = Flask(__name__)

# === MEXC API CONFIG ===
API_KEY = os.getenv("MEXC_API_KEY")
API_SECRET = os.getenv("MEXC_API_SECRET")

BASE_URL = "https://api.mexc.com"  # change to testnet if needed: https://testnet.mexc.com

# === UTIL: CREATE SIGNATURE ===
def sign_request(params: dict):
    query_string = '&'.join([f"{key}={params[key]}" for key in sorted(params)])
    signature = hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
    return f"{query_string}&signature={signature}"

# === ROUTE: TEST CONNECTION ===
@app.route('/test', methods=['GET'])
def test_mexc_connection():
    if not API_KEY or not API_SECRET:
        return jsonify({"error": "API keys not set"}), 400
    try:
        res = requests.get(f"{BASE_URL}/api/v3/time", timeout=10)
        if res.status_code == 200:
            return jsonify({"status": "✅ Connected to MEXC API successfully!"})
        else:
            return jsonify({"error": "Failed to connect", "code": res.status_code}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# === ROUTE: PAPER TRADE ===
@app.route('/paper_trade', methods=['POST'])
def paper_trade():
    data = request.get_json()
    print("Received webhook:", data)

    if not data or 'action' not in data:
        return jsonify({"error": "Invalid payload"}), 400

    action = data['action'].upper()
    symbol = data.get("symbol", "LTCUSDT")  # default if not sent
    quantity = data.get("qty", 0.1)          # adjustable trade size

    # Validate action
    if action not in ["LONG", "SHORT"]:
        return jsonify({"error": "Invalid action"}), 400

    # Create order params
    params = {
        "symbol": symbol,
        "side": "BUY" if action == "LONG" else "SELL",
        "type": "MARKET",
        "quantity": quantity,
        "timestamp": int(time.time() * 1000)
    }

    signed_query = sign_request(params)
    headers = {"X-MEXC-APIKEY": API_KEY}

    try:
        response = requests.post(f"{BASE_URL}/api/v3/order", headers=headers, data=signed_query)
        if response.status_code == 200:
            print("✅ Order placed:", response.json())
            return jsonify({"status": "Trade executed", "details": response.json()}), 200
        else:
            print("❌ Order failed:", response.text)
            return jsonify({"error": "Order failed", "response": response.text}), 400
    except Exception as e:
        print("❌ Exception during trade:", str(e))
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
