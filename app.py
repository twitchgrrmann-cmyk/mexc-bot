from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime
import threading

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
SYMBOL = os.environ.get("SYMBOL", "ASTERUSDT_UMCBL")
PRODUCT_TYPE = "umcbl"  # USDT-M futures
MARGIN_COIN = "USDT"
LEVERAGE = int(os.environ.get("LEVERAGE", 12))
RISK_PERCENTAGE = float(os.environ.get("RISK_PERCENTAGE", 20.0))
INITIAL_BALANCE = float(os.environ.get("INITIAL_BALANCE", 25.0))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", 1.55))
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", 1.0))
PHASE_1_THRESHOLD = float(os.environ.get("PHASE_1_THRESHOLD", 1500.0))
PROFIT_RESET_THRESHOLD = 1.5  # 150%
MAX_DRAWDOWN_STOP = float(os.environ.get("MAX_DRAWDOWN_STOP", 50.0))
DEBOUNCE_SEC = float(os.environ.get("DEBOUNCE_SEC", 2.0))
PRICE_CHECK_INTERVAL = 1.0
MAX_PRICE_FAILURES = 10
STATS_LOG_INTERVAL = 300  # Log stats every 5 minutes

# ===================================================
# ✅ LOGGING
# ===================================================
def log(msg, level="INFO"):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}")

# ===================================================
# ✅ BITGET API SIGNATURE
# ===================================================
def generate_signature(timestamp, method, request_path, body=""):
    """Generate Bitget API signature"""
    message = str(timestamp) + method.upper() + request_path + (body if body else "")
    mac = hmac.new(
        bytes(BITGET_SECRET_KEY, encoding="utf8"),
        bytes(message, encoding="utf-8"),
        digestmod=hashlib.sha256,
    )
    return base64.b64encode(mac.digest()).decode()

def get_headers(method, request_path, body=""):
    """Generate request headers with signature"""
    timestamp = str(int(time.time() * 1000))
    sign = generate_signature(timestamp, method, request_path, body)
    return {
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

# ===================================================
# ✅ BITGET API FUNCTIONS
# ===================================================
def get_current_price(symbol):
    """Fetch current market price"""
    try:
        response = requests.get(
            BASE_URL + f"/api/mix/v1/market/ticker?symbol={symbol}", 
            timeout=5
        )
        data = response.json()
        if data.get("code") == "00000":
            return float(data["data"]["last"])
        log(f"❌ Price fetch error: {data}", "ERROR")
        return None
    except Exception as e:
        log(f"❌ Price fetch failed: {e}", "ERROR")
        return None

def set_leverage(symbol, leverage, margin_coin="USDT"):
    """Set leverage for symbol"""
    try:
        request_path = "/api/mix/v1/account/setLeverage"
        body = json.dumps({
            "symbol": symbol,
            "marginCoin": margin_coin,
            "leverage": str(leverage)
        })
        headers = get_headers("POST", request_path, body)
        response = requests.post(BASE_URL + request_path, headers=headers, data=body, timeout=10)
        data = response.json()
        if data.get("code") == "00000":
            log(f"✅ Leverage set to {leverage}x")
            return True
        log(f"⚠️  Leverage setting response: {data}", "WARN")
        return False
    except Exception as e:
        log(f"❌ Set leverage failed: {e}", "ERROR")
        return False

def place_market_order(symbol, side, size, margin_coin="USDT"):
    """
    Place market order on Bitget
    side: 'open_long', 'open_short', 'close_long', 'close_short'
    size: USDT value of position
    """
    try:
        request_path = "/api/mix/v1/order/placeOrder"
        body = json.dumps({
            "symbol": symbol,
            "marginCoin": margin_coin,
            "size": str(size),
            "side": side,
            "orderType": "market",
            "timeInForceValue": "normal"
        })
        headers = get_headers("POST", request_path, body)
        response = requests.post(BASE_URL + request_path, headers=headers, data=body, timeout=10)
        data = response.json()
        
        if data.get("code") == "00000":
            order_id = data["data"]["orderId"]
            log(f"✅ Order placed: {side} | Size: ${size:.2f} | Order ID: {order_id}")
            return order_id
        else:
            log(f"❌ Order failed: {data}", "ERROR")
            return None
    except Exception as e:
        log(f"❌ Place order exception: {e}", "ERROR")
        return None

def get_position(symbol, margin_coin="USDT"):
    """Get current position for symbol"""
    try:
        request_path = f"/api/mix/v1/position/singlePosition?symbol={symbol}&marginCoin={margin_coin}"
        headers = get_headers("GET", request_path)
        response = requests.get(BASE_URL + request_path, headers=headers, timeout=10)
        data = response.json()
        
        if data.get("code") == "00000" and data.get("data"):
            positions = data["data"]
            for pos in positions:
                if float(pos.get("total", 0)) > 0:
                    return pos
        return None
    except Exception as e:
        log(f"❌ Get position failed: {e}", "ERROR")
        return None

def close_all_positions(symbol, margin_coin="USDT"):
    """Close any open positions"""
    try:
        pos = get_position(symbol, margin_coin)
        if not pos:
            return True
        
        hold_side = pos.get("holdSide", "")
        total = float(pos.get("total", 0))
        
        if total > 0:
            side = "close_long" if hold_side == "long" else "close_short"
            log(f"🔄 Closing existing {hold_side} position: {total}")
            return place_market_order(symbol, side, total, margin_coin) is not None
        return True
    except Exception as e:
        log(f"❌ Close positions failed: {e}", "ERROR")
        return False

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
            log(f"🎉 Profit reset triggered! Phase: {phase.upper()} | New balance: ${new_starting:.2f} | Withdrawn: ${withdraw_amount:.2f}")

    def check_emergency_stop(self):
        if self.max_drawdown >= MAX_DRAWDOWN_STOP and not self.trading_paused:
            self.trading_paused = True
            log("🚨 EMERGENCY STOP TRIGGERED! Max drawdown exceeded. Trading paused.", "ERROR")
            # Close any open positions
            close_all_positions(SYMBOL, MARGIN_COIN)

    def open_position(self, side, entry_price, position_size_usdt):
        """
        Open position on Bitget exchange
        side: 'long' or 'short'
        position_size_usdt: USDT value of position
        """
        with self.position_lock:
            if self.current_position:
                log("⚠️  Position already open, closing first", "WARN")
                self.close_position(entry_price, "flip")
            
            # Place market order on Bitget
            bitget_side = "open_long" if side == "long" else "open_short"
            order_id = place_market_order(SYMBOL, bitget_side, position_size_usdt, MARGIN_COIN)
            
            if not order_id:
                log("❌ Failed to open position on exchange", "ERROR")
                return False
            
            # Calculate TP/SL prices
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
            
            # Calculate quantity in coins
            qty = position_size_usdt / entry_price
            
            self.current_position = {
                "side": side,
                "entry_price": entry_price,
                "qty": qty,
                "size_usdt": position_size_usdt,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "open_time": datetime.now().isoformat(),
                "order_id": order_id
            }
            
            self._start_monitoring()
            save_state()
            
            log(f"📈 OPENED {side.upper()} position")
            log(f"   Entry: ${entry_price:.4f} | Size: ${position_size_usdt:.2f} ({qty:.4f} coins)")
            log(f"   TP: ${tp_price:.4f} (+{TAKE_PROFIT_PCT}%) | SL: ${sl_price:.4f} (-{STOP_LOSS_PCT}%)")
            log(f"   Order ID: {order_id}")
            
            return True

    def _start_monitoring(self):
        """Start background thread to monitor TP/SL"""
        self.stop_monitoring.clear()
        if not self.monitor_thread or not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(
                target=self.monitor_position, daemon=True
            )
            self.monitor_thread.start()

    def monitor_position(self):
        """Monitor position and close when TP/SL is hit"""
        consecutive_failures = 0
        while not self.stop_monitoring.is_set():
            with self.position_lock:
                if not self.current_position:
                    break
                
                price = get_current_price(SYMBOL)
                if not price:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_PRICE_FAILURES:
                        log("❌ Too many price fetch failures, stopping monitor", "ERROR")
                        break
                    time.sleep(PRICE_CHECK_INTERVAL)
                    continue
                
                consecutive_failures = 0
                side = self.current_position["side"]
                
                # Check TP
                if (side == "long" and price >= self.current_position["tp_price"]) or \
                   (side == "short" and price <= self.current_position["tp_price"]):
                    self.close_position(price, "TP")
                    break
                
                # Check SL
                if (side == "long" and price <= self.current_position["sl_price"]) or \
                   (side == "short" and price >= self.current_position["sl_price"]):
                    self.close_position(price, "SL")
                    break
            
            time.sleep(PRICE_CHECK_INTERVAL)

    def close_position(self, exit_price, reason="manual"):
        """Close position on Bitget exchange and update virtual balance"""
        with self.position_lock:
            if not self.current_position:
                log("⚠️  No position to close", "WARN")
                return
            
            side = self.current_position["side"]
            entry_price = self.current_position["entry_price"]
            size_usdt = self.current_position["size_usdt"]
            qty = self.current_position["qty"]
            
            # Close position on Bitget
            bitget_side = "close_long" if side == "long" else "close_short"
            order_id = place_market_order(SYMBOL, bitget_side, size_usdt, MARGIN_COIN)
            
            if not order_id:
                log("❌ Failed to close position on exchange!", "ERROR")
                # Continue anyway to update virtual balance
            
            # Calculate PnL based on virtual balance
            price_change_pct = (
                (exit_price - entry_price) / entry_price
                if side == "long"
                else (entry_price - exit_price) / entry_price
            )
            pnl = size_usdt * price_change_pct
            
            # Update virtual balance
            self.current_balance += pnl
            self.total_pnl += pnl
            self.total_trades += 1
            
            if pnl > 0:
                self.winning_trades += 1
                self.consecutive_losses = 0
            else:
                self.losing_trades += 1
                self.consecutive_losses += 1
            
            # Update drawdown tracking
            if self.current_balance > self.peak_balance:
                self.peak_balance = self.current_balance
            drawdown = ((self.peak_balance - self.current_balance) / self.peak_balance) * 100
            if drawdown > self.max_drawdown:
                self.max_drawdown = drawdown
            
            pnl_pct = (pnl / size_usdt) * 100
            emoji = "✅" if pnl > 0 else "❌"
            
            log(f"{emoji} CLOSED {side.upper()} position")
            log(f"   Entry: ${entry_price:.4f} → Exit: ${exit_price:.4f}")
            log(f"   PnL: ${pnl:.4f} ({pnl_pct:+.2f}%) | Reason: {reason}")
            log(f"   New Balance: ${self.current_balance:.2f} | Drawdown: {drawdown:.1f}%")
            
            self.current_position = None
            self.stop_monitoring.set()
            save_state()
            
            # Check for auto-reset or emergency stop
            self.check_auto_reset()
            self.check_emergency_stop()

    def should_trade(self):
        return not self.trading_paused

    def log_stats(self):
        """Log comprehensive trading statistics"""
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        roi = ((self.current_balance - self.initial_balance) / self.initial_balance) * 100
        phase = self.get_current_phase()
        
        log("=" * 70)
        log(f"📊 TRADING STATISTICS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log("=" * 70)
        log(f"💰 Virtual Balance: ${self.current_balance:.2f} (Start: ${self.starting_balance:.2f})")
        log(f"📈 Total PnL: ${self.total_pnl:.2f} | ROI: {roi:+.2f}%")
        log(f"📊 Trades: {self.total_trades} | Wins: {self.winning_trades} | Losses: {self.losing_trades} | Win Rate: {win_rate:.1f}%")
        log(f"📉 Max Drawdown: {self.max_drawdown:.2f}% | Consecutive Losses: {self.consecutive_losses}")
        log(f"🎯 Phase: {phase.upper()} | Resets: {self.reset_count} (P1: {self.phase_1_resets}, P2: {self.phase_2_resets})")
        log(f"💸 Total Withdrawn: ${self.total_withdrawn:.2f} | Total Profit Generated: ${self.total_profit_generated:.2f}")
        
        if self.current_position:
            pos = self.current_position
            current_price = get_current_price(SYMBOL)
            if current_price:
                price_change_pct = (
                    (current_price - pos['entry_price']) / pos['entry_price']
                    if pos['side'] == 'long'
                    else (pos['entry_price'] - current_price) / pos['entry_price']
                )
                unrealized_pnl = pos['size_usdt'] * price_change_pct
                
                log(f"🔓 ACTIVE POSITION: {pos['side'].upper()}")
                log(f"   Entry: ${pos['entry_price']:.4f} | Current: ${current_price:.4f}")
                log(f"   Size: ${pos['size_usdt']:.2f} ({pos['qty']:.4f} coins)")
                log(f"   TP: ${pos['tp_price']:.4f} | SL: ${pos['sl_price']:.4f}")
                log(f"   Unrealized PnL: ${unrealized_pnl:.4f} ({(unrealized_pnl/pos['size_usdt'])*100:+.2f}%)")
        else:
            log("🔒 No active position")
        
        if self.trading_paused:
            log("⚠️  ⚠️  TRADING PAUSED - Emergency stop triggered! ⚠️  ⚠️")
        
        log("=" * 70)


# ===================================================
# ✅ STATE MANAGEMENT
# ===================================================
def save_state():
    try:
        state = {
            'initial_balance': virtual_balance.initial_balance,
            'starting_balance': virtual_balance.starting_balance,
            'current_balance': virtual_balance.current_balance,
            'total_trades': virtual_balance.total_trades,
            'winning_trades': virtual_balance.winning_trades,
            'losing_trades': virtual_balance.losing_trades,
            'total_pnl': virtual_balance.total_pnl,
            'current_position': virtual_balance.current_position,
            'trade_history': virtual_balance.trade_history,
            'max_drawdown': virtual_balance.max_drawdown,
            'peak_balance': virtual_balance.peak_balance,
            'consecutive_losses': virtual_balance.consecutive_losses,
            'trading_paused': virtual_balance.trading_paused,
            'reset_count': virtual_balance.reset_count,
            'phase_1_resets': virtual_balance.phase_1_resets,
            'phase_2_resets': virtual_balance.phase_2_resets,
            'total_withdrawn': virtual_balance.total_withdrawn,
            'total_profit_generated': virtual_balance.total_profit_generated,
        }
        with open(STATE_FILE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"❌ Failed to save state: {e}", "ERROR")

def load_state():
    try:
        with open(STATE_FILE_PATH, "r") as f:
            state = json.load(f)
        
        for key, value in state.items():
            if hasattr(virtual_balance, key):
                setattr(virtual_balance, key, value)
        
        log(f"✅ State loaded: Balance ${virtual_balance.current_balance:.2f}, {virtual_balance.total_trades} trades")
        
        # Resume monitoring if there's an active position
        if virtual_balance.current_position:
            log(f"♻️  Resuming monitoring of {virtual_balance.current_position['side'].upper()} position")
            virtual_balance._start_monitoring()
            
    except FileNotFoundError:
        log(f"📝 No previous state found — starting fresh with ${INITIAL_BALANCE:.2f}")
    except Exception as e:
        log(f"⚠️  Failed to load state: {e}", "WARN")

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
# ✅ INIT VIRTUAL BALANCE & SETUP
# ===================================================
virtual_balance = VirtualBalance(INITIAL_BALANCE)
load_state()

# Set leverage on startup
log(f"⚙️  Setting leverage to {LEVERAGE}x...")
set_leverage(SYMBOL, LEVERAGE, MARGIN_COIN)

# Start stats logger thread
stats_thread = threading.Thread(target=stats_logger_thread, daemon=True)
stats_thread.start()
log(f"🚀 Stats logger started - logging every {STATS_LOG_INTERVAL}s")

# Log initial stats
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

    # Debounce signals
    now = time.time()
    if now - last_signal_time < DEBOUNCE_SEC:
        log("⏱️  Signal debounced (too fast)")
        return jsonify({'success': True, 'action': 'debounced'}), 200
    last_signal_time = now

    # Check if trading is paused
    if not virtual_balance.should_trade():
        log("⛔ Signal rejected - trading paused due to emergency stop", "WARN")
        return jsonify({'success': False, 'reason': 'paused'}), 200

    # Get action
    action = data.get('action', '').upper()
    
    # Handle CLOSE signal from TradingView
    if action == 'CLOSE':
        log("📥 TradingView CLOSE signal received")
        if virtual_balance.current_position:
            price = get_current_price(SYMBOL)
            if not price:
                log("❌ Failed to fetch price for close", "ERROR")
                return jsonify({'error': 'Price fetch failed'}), 500
            
            virtual_balance.close_position(price, "TV_CLOSE")
            return jsonify({
                'success': True,
                'action': 'closed',
                'price': price
            }), 200
        else:
            log("ℹ️  CLOSE signal but no position open (already closed by bot)")
            return jsonify({'success': True, 'action': 'no_position'}), 200
    
    if action not in ['BUY', 'LONG', 'SELL', 'SHORT']:
        log(f"❌ Invalid action received: {action}", "ERROR")
        return jsonify({'error': 'Invalid action'}), 400

    log(f"📡 Webhook signal received: {action}")

    # Get current market price
    price = get_current_price(SYMBOL)
    if not price:
        log("❌ Failed to fetch current price", "ERROR")
        return jsonify({'error': 'Price fetch failed'}), 500

    # Calculate position size based on virtual balance
    position_size_usdt = virtual_balance.current_balance * (RISK_PERCENTAGE / 100) * LEVERAGE
    
    # Determine side
    side = 'long' if action in ['BUY', 'LONG'] else 'short'
    
    # Open position (will close existing if any)
    success = virtual_balance.open_position(side, price, position_size_usdt)
    
    if success:
        return jsonify({
            'success': True,
            'action': action,
            'side': side,
            'price': price,
            'size_usdt': position_size_usdt,
            'balance': virtual_balance.current_balance
        }), 200
    else:
        return jsonify({
            'success': False,
            'error': 'Failed to open position'
        }), 500

# ===================================================
# ✅ HEALTH CHECK ENDPOINT
# ===================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "message": "bot running",
        "balance": virtual_balance.current_balance,
        "trades": virtual_balance.total_trades,
        "active_position": virtual_balance.current_position is not None,
        "trading_paused": virtual_balance.trading_paused
    }), 200

# ===================================================
# ✅ MAIN ENTRY POINT
# ===================================================
if __name__ == "__main__":
    log("🤖 Bitget Live Trading Bot Starting...")
    log(f"📍 Symbol: {SYMBOL} | Leverage: {LEVERAGE}x | Risk: {RISK_PERCENTAGE}%")
    log(f"🎯 TP: {TAKE_PROFIT_PCT}% | SL: {STOP_LOSS_PCT}%")
    log(f"💰 Initial Balance: ${INITIAL_BALANCE} | Phase 1 Threshold: ${PHASE_1_THRESHOLD}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)