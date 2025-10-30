from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime
import threading
import traceback

app = Flask(__name__)

# =====================
# DISK / STATE FILE HANDLING
# =====================
DATA_DIR = "/data"
LOCAL_STATE_FILE = "vb_state.json"
STATE_FILE_PATH = (
    os.path.join(DATA_DIR, LOCAL_STATE_FILE)
    if os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK)
    else os.path.abspath(LOCAL_STATE_FILE)
)
os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
print(f"💾 Using state file at: {STATE_FILE_PATH}")

# =====================
# CONFIG
# =====================
BASE_URL = "https://api.bitget.com"
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "Grrtrades")
SYMBOL = os.environ.get("SYMBOL", "TAOUSDT_UMCBL")
LEVERAGE = int(os.environ.get("LEVERAGE", 11))
RISK_PERCENTAGE = float(os.environ.get("RISK_PERCENTAGE", 30.0))
INITIAL_BALANCE = float(os.environ.get("INITIAL_BALANCE", 20.0))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", 1.3))
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", 0.75))
PHASE_1_THRESHOLD = float(os.environ.get("PHASE_1_THRESHOLD", 2000.0))
PROFIT_RESET_THRESHOLD = 2.0
MAX_DRAWDOWN_STOP = float(os.environ.get("MAX_DRAWDOWN_STOP", 50.0))
DEBOUNCE_SEC = float(os.environ.get("DEBOUNCE_SEC", 2.0))
PRICE_CHECK_INTERVAL = 1.0
MAX_PRICE_FAILURES = 5

# =====================
# LOGGING
# =====================
def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    print(line, flush=True)
    with open("/data/bot_log.txt", "a") as f:
        f.write(line + "\n")


# =====================
# VIRTUAL BALANCE
# =====================
class VirtualBalance:
    def __init__(self, initial_balance):
        self.initial_balance = initial_balance
        self.starting_balance = initial_balance
        self.current_balance = initial_balance
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = 0.0
        self.current_position = None
        self.max_drawdown = 0.0
        self.peak_balance = initial_balance
        self.trading_paused = False

    def open_position(self, side, entry_price, qty):
        if self.current_position:
            return False
        tp_price = (
            entry_price * (1 + TAKE_PROFIT_PCT / 100)
            if side == "long"
            else entry_price * (1 - TAKE_PROFIT_PCT / 100)
        )
        sl_price = (
            entry_price * (1 - STOP_LOSS_PCT / 100)
            if side == "long"
            else entry_price * (1 + STOP_LOSS_PCT / 100)
        )
        self.current_position = {
            "side": side,
            "entry_price": entry_price,
            "qty": qty,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "open_time": datetime.now().isoformat(),
        }
        save_state()
        log(f"📈 Opened {side} @ {entry_price:.4f} (qty={qty:.4f})")
        threading.Thread(target=self.monitor_position, daemon=True).start()
        return True

    def monitor_position(self):
        fails = 0
        while self.current_position:
            price = get_current_price(SYMBOL)
            if not price:
                fails += 1
                if fails >= MAX_PRICE_FAILURES:
                    break
                time.sleep(PRICE_CHECK_INTERVAL)
                continue
            side = self.current_position["side"]
            if side == "long":
                if price >= self.current_position["tp_price"]:
                    self.close_position(price, "TP")
                    break
                elif price <= self.current_position["sl_price"]:
                    self.close_position(price, "SL")
                    break
            else:
                if price <= self.current_position["tp_price"]:
                    self.close_position(price, "TP")
                    break
                elif price >= self.current_position["sl_price"]:
                    self.close_position(price, "SL")
                    break
            time.sleep(PRICE_CHECK_INTERVAL)

    def close_position(self, exit_price, reason="manual"):
        if not self.current_position:
            return
        side = self.current_position["side"]
        entry = self.current_position["entry_price"]
        qty = self.current_position["qty"]
        pnl = qty * entry * (
            (exit_price - entry) / entry if side == "long" else (entry - exit_price) / entry
        )
        self.current_balance += pnl
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        self.total_pnl += pnl
        log(f"❌ Closed {side} @ {exit_price:.4f} | PnL={pnl:.4f} ({reason})")
        self.current_position = None
        save_state()


# =====================
# STATE MANAGEMENT
# =====================
def save_state():
    try:
        with open(STATE_FILE_PATH, "w") as f:
            json.dump(virtual_balance.__dict__, f, indent=2)
    except Exception as e:
        log(f"Save error: {e}", "ERROR")


def load_state():
    try:
        with open(STATE_FILE_PATH, "r") as f:
            state = json.load(f)
            for k, v in state.items():
                setattr(virtual_balance, k, v)
        log("✅ State loaded")
    except FileNotFoundError:
        log("No saved state — starting new session")


virtual_balance = VirtualBalance(INITIAL_BALANCE)
load_state()

# =====================
# BITGET API
# =====================
def get_current_price(symbol):
    try:
        r = requests.get(
            f"{BASE_URL}/api/mix/v1/market/ticker?symbol={symbol}", timeout=5
        )
        data = r.json()
        return float(data["data"]["last"]) if data.get("code") == "00000" else None
    except Exception as e:
        log(f"Price fetch error: {e}", "ERROR")
        return None


# =====================
# WEBHOOK (Render Safe)
# =====================
last_signal_time = 0

@app.route("/")
def home():
    return jsonify({"status": "alive", "balance": virtual_balance.current_balance})


@app.route("/webhook", methods=["POST"])
def webhook():
    """Instant response to avoid Render timeout."""
    try:
        data = request.get_json(force=True)
        if not data or data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        threading.Thread(target=process_signal, args=(data,), daemon=True).start()
        return jsonify({"status": "received"}), 200
    except Exception as e:
        log(f"Webhook error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 500


def process_signal(data):
    global last_signal_time
    try:
        now = time.time()
        if now - last_signal_time < DEBOUNCE_SEC:
            log("⚠️ Debounced signal")
            return
        last_signal_time = now

        action = data.get("action", "").upper()
        price = get_current_price(SYMBOL)
        if not price:
            log("⚠️ No price received, skipping signal")
            return

        # Close opposite position if open
        if virtual_balance.current_position:
            virtual_balance.close_position(price, "signal_flip")

        qty = virtual_balance.current_balance * (RISK_PERCENTAGE / 100) * LEVERAGE / price
        side = "long" if action in ["BUY", "LONG"] else "short"
        virtual_balance.open_position(side, price, qty)

    except Exception as e:
        log(f"Process signal error: {traceback.format_exc()}", "ERROR")


# =====================
# MAIN
# =====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
