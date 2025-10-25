from flask import Flask, request, jsonify
import os

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ MEXC Trading Bot is Running on Render!"

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print("Webhook received:", data)
    # For now, just print the incoming alert
    return jsonify({"status": "ok", "data": data}), 200

if __name__ == "__main__":
@app.route('/test', methods=['GET'])
def test_mexc_connection():
    api_key = os.getenv('MEXC_API_KEY')
    api_secret = os.getenv('MEXC_API_SECRET')
    
    if not api_key or not api_secret:
        return jsonify({"error": "API keys not set"}), 400

    url = "https://api.mexc.com/api/v3/time"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            return jsonify({"status": "✅ Connected to MEXC API successfully!"})
        else:
            return jsonify({"error": "Failed to connect", "code": res.status_code}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
