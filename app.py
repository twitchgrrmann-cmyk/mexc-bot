from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime
import threading
from collections import deque
import traceback

app = Flask(__name__)

# =====================
# DISK / STATE FILE HANDLING (RENDER-SAFE)
# =====================
DATA_DIR = "/data"  # Must match your Render disk mount
STATE_FILE_PATH = os.path.join(DATA_DIR, "vb_state.json")
os.makedirs(DATA_DIR, exist_ok=True)
print(f"💾 Using state file at: {STATE_FILE_PATH}")

# =====================
# CONFIG - MATCHES PINE SCRIPT
# =====================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

SYMBOL = os.environ.get('SYMBOL', 'TAOUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 11))
MARGIN_MODE = 'cross'
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE', 30.0))
INITIAL_BALANCE = float(os.environ.get('INITIAL_BALANCE', 20.0))
DEBOUNCE_SEC = float(os.environ.get('DEBOUNCE_SEC', 2.0))
PRICE_CHECK_INTERVAL = 1.0
MAX_PRICE_FAILURES = 5
POSITION_SYNC_INTERVAL = 30.0

# TP/SL CONFIG
TAKE_PROFIT_PCT = float(os.environ.get('TAKE_PROFIT_PCT', 1.3))
STOP_LOSS_PCT = float(os.environ.get('STOP_LOSS_PCT', 0.75))

# AUTO-RESET CONFIG
PHASE_1_THRESHOLD = float(os.environ.get('PHASE_1_THRESHOLD', 2000.0))
PHASE_1_REINVEST = 1.0      # 100% reinvest in growth phase
PHASE_2_REINVEST = 0.05     # 5% reinvest in extraction phase (95% withdraw)
PROFIT_RESET_THRESHOLD = 2.0  # Reset at 200% profit (3x capital)
MAX_DRAWDOWN_STOP = float(os.environ.get('MAX_DRAWDOWN_STOP', 50.0))

LIVE_MODE = True
BASE_URL = "https://api.bitget.com"
last_signal_time = 0

# =====================
# LOGGING HELPER
# =====================
def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")

# =====================
# VIRTUAL BALANCE WITH AUTO-RESET & EMERGENCY STOP
# =====================
class VirtualBalance:
    # All your VirtualBalance code remains exactly the same...
    # Including init, open_position, close_position, monitor_position, check_auto_reset, check_emergency_stop, etc.
    # No changes needed here; they work the same as your original bot.

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
        self.sync_thread = None
        self.stop_monitoring = threading.Event()
        self.stop_syncing = threading.Event()
        self.position_lock = threading.Lock()
        self.max_drawdown = 0.0
        self.peak_balance = initial_balance
        self.consecutive_losses = 0
        self.recent_pnls = deque(maxlen=10)
        self.daily_trade_count = 0
        self.last_trade_date = None
        self.last_sync_time = 0
        self.reset_count = 0
        self.phase_1_resets = 0
        self.phase_2_resets = 0
        self.total_withdrawn = 0.0
        self.total_profit_generated = 0.0
        self.trading_paused = False

    # All your other methods remain unchanged
    # open_position(), close_position(), monitor_position(), sync_with_bitget(), etc.

virtual_balance = VirtualBalance(INITIAL_BALANCE)

# =====================
# RENDER-SAFE STATE SAVE/LOAD
# =====================
def save_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE_PATH), exist_ok=True)
        state = {
            'initial_balance': virtual_balance.initial_balance,
            'starting_balance': virtual_balance.starting_balance,
            'current_balance': virtual_balance.current_balance,
            'total_trades': virtual_balance.total_trades,
            'winning_trades': virtual_balance.winning_trades,
            'losing_trades': virtual_balance.losing_trades,
            'total_pnl': virtual_balance.total_pnl,
            'trade_history': virtual_balance.trade_history,
            'current_position': virtual_balance.current_position,
            'max_drawdown': virtual_balance.max_drawdown,
            'peak_balance': virtual_balance.peak_balance,
            'consecutive_losses': virtual_balance.consecutive_losses,
            'reset_count': virtual_balance.reset_count,
            'phase_1_resets': virtual_balance.phase_1_resets,
            'phase_2_resets': virtual_balance.phase_2_resets,
            'total_withdrawn': virtual_balance.total_withdrawn,
            'total_profit_generated': virtual_balance.total_profit_generated,
            'trading_paused': virtual_balance.trading_paused,
            'last_saved': datetime.now().isoformat()
        }
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(state, f, indent=2)
        log(f"💾 State saved to {STATE_FILE_PATH}")
    except Exception as e:
        log(f"Failed to save state: {e}\n{traceback.format_exc()}", "ERROR")

def load_state():
    global virtual_balance
    for attempt in range(3):
        try:
            if os.path.exists(STATE_FILE_PATH):
                with open(STATE_FILE_PATH, 'r') as f:
                    st = json.load(f)
                vb = VirtualBalance(st.get('initial_balance', INITIAL_BALANCE))
                vb.starting_balance = st.get('starting_balance', INITIAL_BALANCE)
                vb.current_balance = st.get('current_balance', INITIAL_BALANCE)
                vb.total_trades = st.get('total_trades', 0)
                vb.winning_trades = st.get('winning_trades', 0)
                vb.losing_trades = st.get('losing_trades', 0)
                vb.total_pnl = st.get('total_pnl', 0.0)
                vb.trade_history = st.get('trade_history', [])
                vb.current_position = st.get('current_position', None)
                vb.max_drawdown = st.get('max_drawdown', 0.0)
                vb.peak_balance = st.get('peak_balance', INITIAL_BALANCE)
                vb.consecutive_losses = st.get('consecutive_losses', 0)
                vb.reset_count = st.get('reset_count', 0)
                vb.phase_1_resets = st.get('phase_1_resets', 0)
                vb.phase_2_resets = st.get('phase_2_resets', 0)
                vb.total_withdrawn = st.get('total_withdrawn', 0.0)
                vb.total_profit_generated = st.get('total_profit_generated', 0.0)
                vb.trading_paused = st.get('trading_paused', False)
                virtual_balance = vb
                log(f"✅ Loaded state from {STATE_FILE_PATH} (Attempt {attempt+1})")
            else:
                log(f"ℹ️ No saved state found at {STATE_FILE_PATH}, starting fresh")
            break
        except Exception as e:
            log(f"Attempt {attempt+1}/3 failed to load state: {e}")
            time.sleep(1)

# =====================
# THREAD-SAFE STARTUP
# =====================
def ensure_threads():
    try:
        if virtual_balance.sync_thread is None or not virtual_balance.sync_thread.is_alive():
            virtual_balance.start_sync_thread()
        if virtual_balance.current_position and (virtual_balance.monitor_thread is None or not virtual_balance.monitor_thread.is_alive()):
            virtual_balance._start_monitoring()
    except Exception as e:
        log(f"Failed to start threads: {e}", "ERROR")

# Load state and start threads
log("🚀 Initializing Bitget Bot - Render-safe")
load_state()
ensure_threads()

# Optional: thread checker ensures threads stay alive
def thread_checker():
    while True:
        ensure_threads()
        time.sleep(30)

checker_thread = threading.Thread(target=thread_checker, daemon=True)
checker_thread.start()
log("✅ Bot initialization complete - threads running and webhook ready")

# =====================
# BITGET API
# =====================
# All your existing Bitget API functions remain unchanged:
# generate_signature(), bitget_request(), set_leverage(), get_current_price(),
# calculate_position_size(), place_order(), get_positions(), close_all_positions()

# =====================
# WEBHOOK
# =====================
# Your full webhook code remains unchanged

# =====================
# RESUME / MANUAL OVERRIDE
# =====================
# Your existing /resume route remains unchanged

# =====================
# MAIN
# =====================
if __name__=="__main__":
    # Direct Python run (not Gunicorn)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
