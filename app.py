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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
