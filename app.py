import os
import time
import hmac
import hashlib
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# === Load environment variables from Render ===
API_KEY = os.environ.get("MEXC_API_KEY")
SECRET_KEY = os.environ.get("MEXC_SECRET_KEY")

# === SETTINGS ===
BASE_URL = "https://contract.mexc.com"  # Futures endpoint
SYMBOL = "LTC_USDT"
TRADE_SIZE_USDT = 10  # Amount per trade (in USDT)
LEVERAGE = 200        # Your chosen leverage
DEBUG_MODE = True     # Set to False for live use


# === AUTH + SIGNING ===
def sign_request(params: dict):
    sorted_params = sorted(params.items())
    query_string = '&'.join([f"{k}={v}" for k, v in sorted_params])
    signature = hmac.new(SECRET_KEY.encode(), query_string.encode(), hashlib.sha256).hexdigest()
    return signature


# === PLACE FUTURES ORDER ===
def place_futures_order(side: str):
    endpoint = "/api/v1/private/order/submit"
    url = BASE_URL + endpoint

    timestamp = int(time.time() * 1000)
    params = {
        "symbol": SYMBOL,
        "price": 0,  # 0 = market order when orderType=1
        "vol": TRADE_SIZE_USDT,  # amount in USDT
        "leverage": LEVERAGE,
        "side": 1 if side.upper() == "LONG" else 2,  # 1 = open long, 2 = open short
        "type": 1,  # 1 = market order
        "open_type": 1,  # 1 = isolated margin
        "position_id": 0,
        "external_oid": str(timestamp),
        "stop_loss_price": 0,
        "take_profit_price": 0,
        "timestamp": timestamp
    }

    signature = sign_request(params)
    headers = {
        "Content-Type": "application/json",
        "ApiKey": API_KEY,
        "Request-Time": str(timestamp),
        "Signature": signature
    }

    if DEBUG_MODE:
        print(f"[DEBUG] Sending {side.upper()} order — {TRADE_SIZE_USDT} USDT @ {LEVERAGE}x leverage")

    response = requests.post(url, headers=headers, json=params)
    print(f"[MEXC RESPONSE] {response.status_code} - {response.text}")
    return response.json()


# === CLOSE ALL POSITIONS ===
def close_all_positions():
    endpoint = "/api/v1/private/position/close-all"
    url = BASE_URL + endpoint
    timestamp = int(time.time() * 1000)
    params = {"timestamp": timestamp}
    signature = sign_request(params)

    headers = {
        "ApiKey": API_KEY,
        "Request-Time": str(timestamp),
        "Signature": signature
    }

    response = requests.post(url, headers=headers, json=params)
    print(f"[MEXC CLOSE RESPONSE] {response.status_code} - {response.text}")
    return response.json()


# === WEBHOOK ROUTE ===
@app.route('/paper_trade', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    print("Received webhook:", data)

    if not data or "action" not in data:
        return jsonify({"error": "Missing 'action'"}), 400

    action = data["action"].upper()

    if action == "LONG":
        result = place_futures_order("LONG")
    elif action == "SHORT":
        result = place_futures_order("SHORT")
    elif action == "CLOSE":
        result = close_all_positions()
    else:
        return jsonify({"error": "Invalid action"}), 400

    return jsonify({"status": "ok", "result": result}), 200


# === STATUS ROUTE ===
@app.route('/', methods=['GET'])
def home():
    return "✅ MEXC Futures Bot is running", 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
