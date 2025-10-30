from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime
import threading
from collections import deque
import traceback

# ===================================================
# ✅ FLASK APP
# ===================================================
app = Flask(__name__)

# ===================================================
# ✅ STATE FILE CONFIG
# ===================================================
DATA_DIR = "/data"
LOCAL_STATE_FILE = "vb_state.json"
STATE_FILE_PATH = (
    os.path.join(DATA_DIR, LOCAL_STATE_FILE)
    if os.path.isdir(DATA_DIR) and os.access(DATA_DIR, os.W_OK)
    else os.path.abspath(LOCAL_STATE_FILE)
)
if not os.path.exists(os.path.dirname(STATE_FILE_PATH)):
    os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
print(f"💾 Using state file at: {STATE_FILE_PATH}")

# ===================================================
# ✅ CONFIG (edit via Render ENV VARS)
# ===================================================
BASE_URL = "https://api.bitget.com"
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "Grrtrades")
SYMBOL = os.environ.get("SYMBOL", "TAOUSDT_UMCBL")
LEVERAGE = int(os.environ.get("LEVERAGE", 11))
RISK_PERCENTAGE = float(os.environ.get("RISK_PERCENTAGE", 30.0))
INITIAL_BALANCE = float(os.environ.get("INITIAL_BALANCE", 20.0))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", 2.0))
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", 1.3))
PHASE_1_THRESHOLD = float(os.environ.get("PHASE_1_THRESHOLD", 2000.0))
PROFIT_RESET_THRESHOLD = 2.0  # 200%
MAX_DRAWDOWN_STOP = float(os.environ.get("MAX_DRAWDOWN_STOP", 50.0))
DEBOUNCE_SEC = float(os.environ.get("DEBOUNCE_SEC", 2.0))
PRICE_CHECK_INTERVAL = 1.0
MAX_PRICE_FAILURES = 5
STATS_LOG_INTERVAL = 300  # Log stats every 5 minutes

# ===================================================
# ✅ LOGGING
# ===================================================
def log(msg, level="INFO"):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}")

# ===================================================
# ✅ VIRTUAL BALANCE CLASS
# ===================================================
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
        self.trade_history = []
        self.monitor_thread = None
        self.stop_monitoring = threading.Event()
        self.position_lock = threading.Lock()
        self.max_drawdown = 0.0
        self.peak_balance = initial_balance
        self.consecutive_losses = 0
        self.trading_paused = False
        self.reset_count = 0
        self.phase_1_resets = 0
        self.phase_2_resets = 0
        self.total_withdrawn = 0.0
        self.total_profit_generated = 0.0

    def get_current_phase(self):
        return "growth" if self.starting_balance < PHASE_1_THRESHOLD else "extraction"

    def check_auto_reset(self):
        if self.current_balance >= self.starting_balance * (1 + PROFIT_RESET_THRESHOLD):
            profit = self.current_balance - self.starting_balance
            phase = self.get_current_phase()
            reinvest_pct = 1.0 if phase == "growth" else 0.05
            withdraw_amount = profit * (1 - reinvest_pct)
            new_starting = self.starting_balance + (profit * reinvest_pct)
            self.starting_balance = self.current_balance = self.peak_balance = new_starting
            self.max_drawdown = 0.0
            self.total_withdrawn += withdraw_amount
            self.total_profit_generated += profit
            if phase == "growth":
                self.phase_1_resets += 1
            else:
                self.phase_2_resets += 1
            self.reset_count += 1
            save_state()
            log(f"🎉 Profit reset triggered, new balance: {new_starting}")

    def check_emergency_stop(self):
        if self.max_drawdown >= MAX_DRAWDOWN_STOP and not self.trading_paused:
            self.trading_paused = True
            log("🚨 Emergency stop triggered! Trading paused.", "ERROR")

    def open_position(self, side, entry_price, qty):
        with self.position_lock:
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
            self._start_monitoring()
            save_state()
            log(f"📈 Opened {side.upper()} position @ ${entry_price:.4f} | Qty: {qty:.6f} | TP: ${tp_price:.4f} | SL: ${sl_price:.4f}")
            return True

    def _start_monitoring(self):
        self.stop_monitoring.clear()
        if not self.monitor_thread or not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(
                target=self.monitor_position, daemon=True
            )
            self.monitor_thread.start()

    def monitor_position(self):
        consecutive_failures = 0
        while not self.stop_monitoring.is_set():
            with self.position_lock:
                if not self.current_position:
                    break
                price = get_current_price(SYMBOL)
                if not price:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_PRICE_FAILURES:
                        break
                    time.sleep(PRICE_CHECK_INTERVAL)
                    continue
                side = self.current_position["side"]
                if (
                    side == "long" and price >= self.current_position["tp_price"]
                ) or (side == "short" and price <= self.current_position["tp_price"]):
                    self.close_position(price, "TP")
                    break
                if (
                    side == "long" and price <= self.current_position["sl_price"]
                ) or (side == "short" and price >= self.current_position["sl_price"]):
                    self.close_position(price, "SL")
                    break
            time.sleep(PRICE_CHECK_INTERVAL)

    def close_position(self, exit_price, reason="normal"):
        with self.position_lock:
            if not self.current_position:
                return
            side = self.current_position["side"]
            entry_price = self.current_position["entry_price"]
            qty = self.current_position["qty"]
            pnl = qty * entry_price * (
                (exit_price - entry_price) / entry_price
                if side == "long"
                else (entry_price - exit_price) / entry_price
            )
            self.current_balance += pnl
            self.total_pnl += pnl
            self.total_trades += 1
            if pnl > 0:
                self.winning_trades += 1
            else:
                self.losing_trades += 1
            
            pnl_pct = (pnl / self.current_balance) * 100
            emoji = "✅" if pnl > 0 else "❌"
            
            self.current_position = None
            save_state()
            log(f"{emoji} Closed {side.upper()} @ ${exit_price:.4f} | PnL: ${pnl:.4f} ({pnl_pct:+.2f}%) | Reason: {reason} | New Balance: ${self.current_balance:.2f}")
            self.check_auto_reset()
            self.check_emergency_stop()

    def should_trade(self):
        return not self.trading_paused

    def log_stats(self):
        """Log comprehensive trading statistics"""
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        roi = ((self.current_balance - self.initial_balance) / self.initial_balance) * 100
        phase = self.get_current_phase()
        
        log("=" * 60)
        log(f"📊 TRADING STATISTICS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log("=" * 60)
        log(f"💰 Virtual Balance: ${self.current_balance:.2f} (Start: ${self.starting_balance:.2f})")
        log(f"📈 Total PnL: ${self.total_pnl:.2f} | ROI: {roi:+.2f}%")
        log(f"📊 Trades: {self.total_trades} | Wins: {self.winning_trades} | Losses: {self.losing_trades} | Win Rate: {win_rate:.1f}%")
        log(f"🎯 Phase: {phase.upper()} | Resets: {self.reset_count} (P1: {self.phase_1_resets}, P2: {self.phase_2_resets})")
        log(f"💸 Total Withdrawn: ${self.total_withdrawn:.2f} | Total Profit: ${self.total_profit_generated:.2f}")
        
        if self.current_position:
            pos = self.current_position
            current_price = get_current_price(SYMBOL)
            if current_price:
                unrealized_pnl = pos['qty'] * pos['entry_price'] * (
                    (current_price - pos['entry_price']) / pos['entry_price']
                    if pos['side'] == 'long'
                    else (pos['entry_price'] - current_price) / pos['entry_price']
                )
                log(f"🔓 ACTIVE POSITION: {pos['side'].upper()} | Entry: ${pos['entry_price']:.4f} | Current: ${current_price:.4f}")
                log(f"   Qty: {pos['qty']:.6f} | TP: ${pos['tp_price']:.4f} | SL: ${pos['sl_price']:.4f}")
                log(f"   Unrealized PnL: ${unrealized_pnl:.4f} ({(unrealized_pnl/self.current_balance)*100:+.2f}%)")
        else:
            log("🔒 No active position")
        
        if self.trading_paused:
            log("⚠️  TRADING PAUSED - Emergency stop triggered!")
        
        log("=" * 60)


# ===================================================
# ✅ STATE MANAGEMENT
# ===================================================
def save_state():
    try:
        state = virtual_balance.__dict__.copy()
        # Remove non-serializable objects
        state.pop('monitor_thread', None)
        state.pop('stop_monitoring', None)
        state.pop('position_lock', None)
        with open(STATE_FILE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"❌ Failed to save state: {e}", "ERROR")


def load_state():
    try:
        with open(STATE_FILE_PATH, "r") as f:
            st = json.load(f)
        # Restore serializable attributes
        for k, v in st.items():
            if k not in ['monitor_thread', 'stop_monitoring', 'position_lock']:
                setattr(virtual_balance, k, v)
        log(f"✅ State loaded from disk: Balance ${virtual_balance.current_balance:.2f}")
    except FileNotFoundError:
        log("📝 No previous state found — starting fresh with ${:.2f}".format(INITIAL_BALANCE))
    except Exception as e:
        log(f"⚠️  Failed to load state: {e}", "WARN")


# ===================================================
# ✅ BITGET API UTILITIES
# ===================================================
def get_current_price(symbol):
    try:
        data = requests.get(
            BASE_URL + f"/api/mix/v1/market/ticker?symbol={symbol}", timeout=5
        ).json()
        return float(data["data"]["last"]) if data.get("code") == "00000" else None
    except Exception as e:
        log(f"❌ Price fetch failed: {e}", "ERROR")
        return None


# ===================================================
# ✅ PERIODIC STATS LOGGER
# ===================================================
def stats_logger_thread():
    """Background thread that logs stats every STATS_LOG_INTERVAL seconds"""
    while True:
        time.sleep(STATS_LOG_INTERVAL)
        try:
            virtual_balance.log_stats()
        except Exception as e:
            log(f"❌ Stats logging error: {e}", "ERROR")


# ===================================================
# ✅ INIT VIRTUAL BALANCE
# ===================================================
virtual_balance = VirtualBalance(INITIAL_BALANCE)
load_state()

# Start stats logger thread
stats_thread = threading.Thread(target=stats_logger_thread, daemon=True)
stats_thread.start()
log("🚀 Stats logger started - will log every 5 minutes")

# Log initial stats on startup
virtual_balance.log_stats()

# ===================================================
# ✅ WEBHOOK ENDPOINT
# ===================================================
last_signal_time = 0

@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    global last_signal_time

    # --- Handle GET (status check) ---
    if request.method == 'GET':
        return jsonify({
            'status': 'webhook online',
            'can_trade': virtual_balance.should_trade(),
            'balance': virtual_balance.current_balance,
            'position': virtual_balance.current_position,
            'uptime': datetime.now().isoformat()
        }), 200

    # --- Handle POST (TradingView signals) ---
    try:
        data = json.loads(request.get_data(as_text=True))
    except Exception:
        return jsonify({'error': 'Invalid JSON'}), 400

    # Check secret
    if data.get('secret') != WEBHOOK_SECRET:
        log("⚠️  Unauthorized webhook attempt", "WARN")
        return jsonify({'error': 'Unauthorized'}), 401

    # Debounce signals (prevent spam)
    now = time.time()
    if now - last_signal_time < DEBOUNCE_SEC:
        log("⏱️  Signal debounced (too fast)")
        return jsonify({'success': True, 'action': 'debounced'}), 200
    last_signal_time = now

    # Stop trading if paused
    if not virtual_balance.should_trade():
        log("⛔ Signal rejected - trading paused", "WARN")
        return jsonify({'success': False, 'reason': 'paused'}), 200

    # Get action
    action = data.get('action', '').upper()
    if action not in ['BUY', 'LONG', 'SELL', 'SHORT']:
        log(f"❌ Invalid action received: {action}", "ERROR")
        return jsonify({'error': 'Invalid action'}), 400

    log(f"📡 Webhook signal received: {action}")

    # Get live price
    price = get_current_price(SYMBOL)
    if not price:
        log("❌ Failed to fetch current price", "ERROR")
        return jsonify({'error': 'Price fetch failed'}), 500

    # Open or flip position
    with virtual_balance.position_lock:
        if virtual_balance.current_position:
            log(f"🔄 Flipping position from {virtual_balance.current_position['side'].upper()} to {action}")
            virtual_balance.close_position(price, 'signal_flip')

        qty = virtual_balance.current_balance * (RISK_PERCENTAGE / 100) * LEVERAGE / price
        side = 'long' if action in ['BUY', 'LONG'] else 'short'
        virtual_balance.open_position(side, price, qty)

    return jsonify({
        'success': True,
        'action': action,
        'price': price,
        'balance': virtual_balance.current_balance,
        'position': virtual_balance.current_position
    }), 200


# ===================================================
# ✅ HEALTH CHECK ENDPOINT (for Render)
# ===================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok", 
        "message": "bot running fine",
        "balance": virtual_balance.current_balance,
        "trades": virtual_balance.total_trades,
        "active_position": virtual_balance.current_position is not None
    }), 200


# ===================================================
# ✅ MAIN ENTRY POINT
# ===================================================
if __name__ == "__main__":
    log("🤖 Bitget Trading Bot Starting...")
    log(f"📍 Symbol: {SYMBOL} | Leverage: {LEVERAGE}x | Risk: {RISK_PERCENTAGE}%")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)