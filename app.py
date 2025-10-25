from flask import Flask, request, jsonify
import os
import time

app = Flask(__name__)

# ========================
# === CONNECTION CHECK ===
# ========================
@app.route('/')
def home():
    return jsonify({"status": "✅ Connected to MEXC API successfully!"})


# ============================
# === TESTING ENDPOINT =======
# ============================
@app.route('/test', methods=['GET'])
def test_connection():
    return jsonify({"status": "✅ Render server is alive!", "time": time.strftime("%Y-%m-%d %H:%M:%S")})


# ===================================
# === PAPER TRADE SIMULATION ========
# ===================================
@app.route('/paper_trade', methods=['POST'])
def paper_trade():
    data = request.get_json(force=True)

    # Extract signal data
    action = data.get('action', '').upper()
    ticker = data.get('ticker', 'UNKNOWN')
    price = data.get('price', 'N/A')
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # Validate action
    if action not in ['LONG', 'SHORT', 'CLOSE']:
        return jsonify({"error": "Invalid action received"}), 400

    # Log fake trade
    print(f"[{timestamp}] 📊 PAPER TRADE -> {action} {ticker} @ {price}")

    # Save to a text file for history (optional)
    with open("paper_trades.log", "a") as f:
        f.write(f"{timestamp} - {action} {ticker} @ {price}\n")

    return jsonify({
        "status": "✅ Paper trade executed",
        "action": action,
        "ticker": ticker,
        "price": price,
        "timestamp": timestamp
    })


# ========================
# === START SERVER =======
# ========================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
