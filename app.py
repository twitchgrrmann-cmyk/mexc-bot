#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Production-ready Bitget webhook bot (Flask)
- Immediate webhook response (avoids Render timeouts)
- Real order placement via Bitget Mix API (market open/close)
- Virtual balance tracking + persistence to /data/vb_state.json
- Non-blocking monitoring (threading.Timer)
- Auto-reset & emergency stop logic
- TP = 2.0%, SL = 1.3% (per user's request)
"""

from flask import Flask, request, jsonify
import os, time, json, hmac, hashlib, base64, requests, threading, traceback
from datetime import datetime

# -------------------------
# Config / file paths
# -------------------------
DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "vb_state.json")
LOG_FILE = os.path.join(DATA_DIR, "bot_log.txt")

# Bitget / bot config (override via env)
BASE_URL = "https://api.bitget.com"
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.getenv("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "Grrtrades")

SYMBOL = os.getenv("SYMBOL", "TAOUSDT_UMCBL")
LEVERAGE = int(os.getenv("LEVERAGE", 25))
RISK_PERCENT = float(os.getenv("RISK_PERCENTAGE", 15.0))  # % of virtual balance to use
INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", 20.0))

# TAKE/STOP - user requested values
TAKE_PROFIT_PCT = 2.0    # 2%
STOP_LOSS_PCT = 1.3      # 1.3%

# Safety / other
DEBOUNCE_SEC = float(os.getenv("DEBOUNCE_SEC", 2.0))
POSITION_SYNC_INTERVAL = float(os.getenv("POSITION_SYNC_INTERVAL", 30.0))
PRICE_CHECK_INTERVAL = float(os.getenv("PRICE_CHECK_INTERVAL", 1.0))
MAX_PRICE_FAILURES = int(os.getenv("MAX_PRICE_FAILURES", 5))

PHASE_1_THRESHOLD = float(os.getenv("PHASE_1_THRESHOLD", 2000.0))
PROFIT_RESET_THRESHOLD = 2.0  # triggers at +200% profit
MAX_DRAWDOWN_STOP = float(os.getenv("MAX_DRAWDOWN_STOP", 50.0))  # emergency stop %
PHASE_2_REINVEST = 0.05  # 5% reinvest in extraction

# -------------------------
# Logging
# -------------------------
def log(msg, level="INFO"):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        # best-effort, but don't crash on logging failure
        pass

# -------------------------
# Bitget helpers
# -------------------------
def generate_signature(timestamp, method, request_path, body, secret):
    """
    Bitget signature: base64(HMAC_SHA256(secret, timestamp + method + requestPath + bodyStr))
    body must be JSON string if present else "".
    """
    body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body else ""
    message = f"{timestamp}{method}{request_path}{body_str}"
    mac = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None, retries=2, timeout=10):
    headers = {}
    timestamp = str(int(time.time() * 1000))
    body = params if params is not None else None
    sign = generate_signature(timestamp, method, endpoint, body, BITGET_SECRET_KEY)
    headers.update({
        "ACCESS-KEY": BITGET_API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": BITGET_PASSPHRASE,
        "Content-Type": "application/json"
    })
    url = BASE_URL + endpoint
    for attempt in range(retries):
        try:
            if method.upper() == "POST":
                r = requests.post(url, json=body, headers=headers, timeout=timeout)
            else:
                r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log(f"Bitget request error attempt {attempt+1}: {e}", "WARNING")
            time.sleep(0.5)
    return {"error": "request_failed"}

def set_leverage(symbol, leverage):
    # call twice for both sides if endpoint requires holdSide param
    for holdSide in ("long", "short"):
        res = bitget_request("POST", "/api/mix/v1/account/setLeverage", {
            "symbol": symbol,
            "marginCoin": "USDT",
            "leverage": int(leverage),
            "holdSide": holdSide
        })
        log(f"Set leverage {holdSide} -> {res}")

def place_order(symbol, side, size):
    """
    Place a market order.
    side: 'open_long'|'open_short'|'close_long'|'close_short' depending on desired action
    size: string or number (quantity)
    """
    res = bitget_request("POST", "/api/mix/v1/order/placeOrder", {
        "symbol": symbol,
        "marginCoin": "USDT",
        "side": side,
        "orderType": "market",
        "size": str(size),
        "timeInForceValue": "normal"
    })
    log(f"Place order {side} {size} -> {res}")
    return res

def get_positions(symbol):
    return bitget_request("GET", f"/api/mix/v1/position/singlePosition?symbol={symbol}&marginCoin=USDT")

def close_all_positions(symbol):
    pos = get_positions(symbol)
    if not pos:
        log("get_positions failed", "WARNING")
        return
    if pos.get("code") != "00000":
        # sometimes bitget returns code 0 or other; log and return
        log(f"get_positions response: {pos}", "WARNING")
        return
    for p in pos.get("data", []):
        try:
            total = float(p.get("total", 0))
            if total <= 0:
                continue
            holdSide = p.get("holdSide", "")
            # API expects close_long/close_short
            side = "close_long" if holdSide == "long" else "close_short"
            place_order(symbol, side, abs(total))
            log(f"Requested close for {holdSide} size {total}")
        except Exception as e:
            log(f"Error closing position entry: {e}", "ERROR")

def get_market_price(symbol):
    r = bitget_request("GET", f"/api/mix/v1/market/ticker?symbol={symbol}", retries=2, timeout=4)
    if not r:
        return None
    if r.get("code") == "00000" and r.get("data"):
        try:
            return float(r["data"]["last"])
        except Exception:
            return None
    return None

# -------------------------
# VirtualBalance class (persistent)
# -------------------------
class VirtualBalance:
    def __init__(self, initial_balance):
        self.initial_balance = float(initial_balance)
        self.starting_balance = float(initial_balance)      # starting capital for current cycle
        self.current_balance = float(initial_balance)
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = 0.0
        self.trade_history = []
        self.current_position = None  # dict with keys: side, entry_price, qty, tp_price, sl_price, open_time
        self.peak_balance = float(initial_balance)
        self.max_drawdown = 0.0
        self.consecutive_losses = 0
        self.reset_count = 0
        self.phase_1_resets = 0
        self.phase_2_resets = 0
        self.total_withdrawn = 0.0
        self.trading_paused = False
        # runtime helpers
        self.position_lock = threading.Lock()
        self.monitor_timer = None
        self.sync_timer = None
        self.last_sync_time = 0

    def save(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.__dict__, f, default=str, indent=2)
        except Exception as e:
            log(f"Failed to save state: {e}", "ERROR")

    def load(self):
        if not os.path.exists(STATE_FILE):
            log("State file not found, starting fresh")
            return
        try:
            with open(STATE_FILE, "r") as f:
                st = json.load(f)
            # load relevant keys defensively
            for k, v in st.items():
                if hasattr(self, k):
                    setattr(self, k, v)
            log("State loaded from disk")
        except Exception as e:
            log(f"Failed to load state: {e}", "ERROR")

    def get_phase(self):
        return "growth" if float(self.starting_balance) < PHASE_1_THRESHOLD else "extraction"

    def check_auto_reset(self):
        # Trigger auto-reset at +200% (PROFIT_RESET_THRESHOLD)
        try:
            if self.current_balance >= float(self.starting_balance) * (1 + PROFIT_RESET_THRESHOLD):
                profit = self.current_balance - self.starting_balance
                phase = self.get_phase()
                if phase == "growth":
                    reinvest_pct = 1.0
                    withdraw = profit * (1 - reinvest_pct)
                    self.phase_1_resets += 1
                else:
                    reinvest_pct = PHASE_2_REINVEST
                    withdraw = profit * (1 - reinvest_pct)
                    self.phase_2_resets += 1
                new_start = self.starting_balance + (profit * reinvest_pct)
                log(f"AUTO-RESET triggered. Profit {profit:.2f}, withdraw {withdraw:.2f}, new_start {new_start:.2f}")
                self.starting_balance = new_start
                self.current_balance = new_start
                self.peak_balance = new_start
                self.max_drawdown = 0.0
                self.total_withdrawn += withdraw
                self.total_pnl += profit
                self.reset_count += 1
                self.save()
                return True
        except Exception as e:
            log(f"Auto-reset check error: {e}", "ERROR")
        return False

    def check_emergency(self):
        if self.max_drawdown >= MAX_DRAWDOWN_STOP and not self.trading_paused:
            self.trading_paused = True
            log(f"EMERGENCY STOP: max_drawdown {self.max_drawdown:.2f}% >= limit {MAX_DRAWDOWN_STOP}%", "ERROR")
            # attempt to close positions on exchange
            try:
                close_all_positions(SYMBOL)
            except Exception as e:
                log(f"Error closing positions during emergency: {e}", "ERROR")
            self.save()
            return True
        return False

    def record_open(self, side, entry_price, qty):
        with self.position_lock:
            if self.current_position:
                log("Tried to open position but one already exists", "WARNING")
                return False
            tp, sl = self.calc_tp_sl(side, entry_price)
            self.current_position = {
                "side": side,
                "entry_price": float(entry_price),
                "qty": float(qty),
                "tp_price": float(tp),
                "sl_price": float(sl),
                "open_time": datetime.now().isoformat()
            }
            self.save()
            log(f"Recorded open: {side} {qty} @ {entry_price} | TP {tp} SL {sl}")
            # start monitor
            self._schedule_monitor()
            return True

    def calc_tp_sl(self, side, entry_price):
        entry_price = float(entry_price)
        if side == "long":
            tp = entry_price * (1 + TAKE_PROFIT_PCT / 100.0)
            sl = entry_price * (1 - STOP_LOSS_PCT / 100.0)
        else:
            tp = entry_price * (1 - TAKE_PROFIT_PCT / 100.0)
            sl = entry_price * (1 + STOP_LOSS_PCT / 100.0)
        return tp, sl

    def _schedule_monitor(self):
        # Cancel old timer if exists
        try:
            if self.monitor_timer and isinstance(self.monitor_timer, threading.Timer):
                try:
                    self.monitor_timer.cancel()
                except Exception:
                    pass
            self.monitor_timer = threading.Timer(PRICE_CHECK_INTERVAL, self._monitor_once)
            self.monitor_timer.daemon = True
            self.monitor_timer.start()
        except Exception as e:
            log(f"Failed to schedule monitor: {e}", "ERROR")

    def _monitor_once(self):
        with self.position_lock:
            if not self.current_position:
                return
            try:
                price = get_market_price(SYMBOL)
                if price is None:
                    # reschedule
                    self._schedule_monitor()
                    return
                side = self.current_position["side"]
                tp = float(self.current_position["tp_price"])
                sl = float(self.current_position["sl_price"])

                if (side == "long" and price >= tp) or (side == "short" and price <= tp):
                    log(f"TP hit on monitor: price {price} tp {tp}")
                    # close on exchange then record
                    close_all_positions(SYMBOL)
                    time.sleep(0.5)
                    self.record_close(price, reason="TP")
                    return
                if (side == "long" and price <= sl) or (side == "short" and price >= sl):
                    log(f"SL hit on monitor: price {price} sl {sl}")
                    close_all_positions(SYMBOL)
                    time.sleep(0.5)
                    self.record_close(price, reason="SL")
                    return
            except Exception as e:
                log(f"Monitor error: {e}\n{traceback.format_exc()}", "ERROR")
            finally:
                # if still open, reschedule
                if self.current_position:
                    self._schedule_monitor()

    def record_close(self, exit_price, reason="manual"):
        with self.position_lock:
            if not self.current_position:
                return None
            side = self.current_position["side"]
            entry = float(self.current_position["entry_price"])
            qty = float(self.current_position["qty"])
            # PnL calculation (not including fees) - aligns with earlier logic
            price_change = (exit_price - entry) / entry if side == "long" else (entry - exit_price) / entry
            pnl = qty * entry * price_change
            self.current_balance += pnl
            self.total_pnl += pnl
            self.total_trades += 1
            if pnl > 0:
                self.winning_trades += 1
                self.consecutive_losses = 0
            else:
                self.losing_trades += 1
                self.consecutive_losses += 1
            # update peak and calc drawdown
            if self.current_balance > self.peak_balance:
                self.peak_balance = self.current_balance
            drawdown = (self.peak_balance - self.current_balance) / self.peak_balance * 100 if self.peak_balance > 0 else 0.0
            if drawdown > self.max_drawdown:
                self.max_drawdown = drawdown
            # append trade record
            tr = {
                "side": side,
                "entry_price": entry,
                "exit_price": exit_price,
                "qty": qty,
                "pnl": pnl,
                "balance_after": self.current_balance,
                "close_time": datetime.now().isoformat(),
                "reason": reason
            }
            self.trade_history.append(tr)
            log(f"Recorded close: {side} PnL {pnl:.6f} | new balance {self.current_balance:.6f} | reason {reason}")
            self.current_position = None
            self.save()
            # run auto-reset and emergency checks
            self.check_auto_reset()
            self.check_emergency()
            return pnl

    def should_trade(self):
        if self.trading_paused:
            log("Trading paused (emergency)", "WARNING")
            return False
        # additional checks can be inserted here (daily drawdown, consecutive losses, etc.)
        return True

    def start_sync_loop(self):
        # use Timer to schedule periodic sync with exchange to detect external closes
        try:
            if self.sync_timer:
                try:
                    self.sync_timer.cancel()
                except Exception:
                    pass
            self.sync_timer = threading.Timer(POSITION_SYNC_INTERVAL, self._sync_once)
            self.sync_timer.daemon = True
            self.sync_timer.start()
        except Exception as e:
            log(f"Failed to schedule sync loop: {e}", "ERROR")

    def _sync_once(self):
        try:
            pos = get_positions(SYMBOL)
            # If the exchange has no position but we think we have one -> assume it closed externally
            if pos and pos.get("code") == "00000":
                data = pos.get("data", [])
                has_pos = any(float(p.get("total", 0)) > 0 for p in data)
                if self.current_position and not has_pos:
                    # assume external close: fetch market price and record close
                    price = get_market_price(SYMBOL)
                    if price is not None:
                        log("Sync: detected external close; recording close locally", "WARNING")
                        self.record_close(price, reason="external_close")
                elif not self.current_position and has_pos:
                    # recover: set a current_position record (best-effort)
                    p = next((p for p in data if float(p.get("total", 0)) > 0), None)
                    if p:
                        holdSide = p.get("holdSide")
                        qty = float(p.get("total", 0))
                        # use current price as entry approximation
                        price = get_market_price(SYMBOL)
                        if price:
                            self.current_position = {
                                "side": holdSide,
                                "entry_price": price,
                                "qty": qty,
                                "tp_price": None,
                                "sl_price": None,
                                "open_time": datetime.now().isoformat(),
                                "recovered": True
                            }
                            log("Sync: recovered position from exchange", "WARNING")
            else:
                # if API returned error code, just log
                log(f"Sync: get_positions error or no data: {pos}", "DEBUG")
        except Exception as e:
            log(f"Sync error: {e}", "ERROR")
        finally:
            # reschedule
            self.start_sync_loop()

# instantiate and load
vb = VirtualBalance(INITIAL_BALANCE)
vb.load()
vb.start_sync_loop()

# -------------------------
# Flask app & webhook handling
# -------------------------
app = Flask(__name__)
last_signal_time = 0.0
lock = threading.Lock()

@app.route("/", methods=["GET"])
def health():
    s = vb.__dict__.copy()
    # hide large internals
    s.pop("position_lock", None)
    s.pop("monitor_timer", None)
    s.pop("sync_timer", None)
    return jsonify({"status": "ok", "virtual_balance": s, "timestamp": datetime.now().isoformat()})

def process_signal_async(payload):
    """
    Runs in background thread. Expects payload dict with 'action' and 'secret' already validated.
    """
    try:
        global vb
        action = payload.get("action", "").upper()
        if action == "":
            log("Empty action in webhook payload", "WARNING")
            return

        # debounce using file/time lock
        now = time.time()
        with lock:
            global last_signal_time
            if now - last_signal_time < DEBOUNCE_SEC:
                log("Signal debounced (too fast)", "DEBUG")
                return
            last_signal_time = now

        if not vb.should_trade():
            log("Signal blocked: should_trade returned False", "WARNING")
            return

        # refresh leverage and margin mode
        try:
            set_leverage(SYMBOL, LEVERAGE)
        except Exception as e:
            log(f"set_leverage error: {e}", "WARNING")

        # fetch price
        price = get_market_price(SYMBOL)
        if price is None:
            log("Failed to fetch current price, aborting signal", "ERROR")
            return

        # If bot thinks it has position, and signal is opposite, close first
        with vb.position_lock:
            if vb.current_position:
                curr_side = vb.current_position.get("side")
                if (action in ("BUY","LONG") and curr_side == "long") or (action in ("SELL","SHORT") and curr_side == "short"):
                    log("Already in same direction, ignoring open signal", "DEBUG")
                    return
                else:
                    # close existing
                    log(f"Signal flip detected. Closing existing {curr_side} to open {action}", "INFO")
                    close_all_positions(SYMBOL)
                    time.sleep(0.5)
                    vb.record_close(price, reason="signal_flip")

        # calculate qty using virtual balance
        qty = (vb.current_balance * (RISK_PERCENT / 100.0) * LEVERAGE) / price
        # guard: minimal qty
        if qty <= 0:
            log("Calculated qty <= 0, aborting", "ERROR")
            return
        # round to 3 decimals (adjust if needed)
        qty = round(qty, 3)

        # place order on exchange
        side_cmd = "open_long" if action in ("BUY","LONG") else "open_short"
        res = place_order(SYMBOL, side_cmd, qty)
        # check response success
        ok = False
        if isinstance(res, dict) and res.get("code") == "00000":
            ok = True
        elif isinstance(res, dict) and res.get("error"):
            log(f"Order placement error: {res}", "ERROR")
        else:
            # sometimes exchange gives nonstandard responses; still try to proceed if looks successful
            log(f"Order response: {res}", "DEBUG")

        if ok:
            # record local virtual position
            vb.record_open("long" if side_cmd == "open_long" else "short", price, qty)
            # schedule sync / monitor
            vb.start_sync_loop()
            log("Order placed and virtual position recorded", "INFO")
        else:
            log("Order failed on exchange; not recording local position", "ERROR")

    except Exception as e:
        log(f"Unhandled process_signal error: {e}\n{traceback.format_exc()}", "ERROR")

@app.route("/webhook", methods=["POST"])
def webhook():
    # respond immediately to avoid Render timeout, process in background
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid_json"}), 400

    if not data or data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    # spawn background thread to handle the heavy lifting
    try:
        th = threading.Thread(target=process_signal_async, args=(data,), daemon=True)
        th.start()
    except Exception as e:
        log(f"Failed to start background thread: {e}", "ERROR")
        return jsonify({"error": "server_error"}), 500

    return jsonify({"status": "accepted"}), 200

# -------------------------
# Resume endpoint (manual override)
# -------------------------
@app.route("/resume", methods=["POST"])
def resume():
    try:
        data = request.get_json(force=True) or {}
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401
        if vb.trading_paused:
            vb.trading_paused = False
            vb.max_drawdown = 0.0
            vb.save()
            return jsonify({"success": True, "message": "trading resumed", "stats": vb.__dict__})
        return jsonify({"success": False, "message": "not paused"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -------------------------
# Startup / run
# -------------------------
if __name__ == "__main__":
    log("Starting Bitget webhook bot (production-ready)", "INFO")
    log(f"Symbol: {SYMBOL} | Leverage: {LEVERAGE} | Risk%: {RISK_PERCENT}% | TP%: {TAKE_PROFIT_PCT} | SL%: {STOP_LOSS_PCT}")
    # quick check: verify credentials present
    if not BITGET_API_KEY or not BITGET_SECRET_KEY or not BITGET_PASSPHRASE:
        log("WARNING: Bitget API keys or passphrase not set - live trading will fail unless env vars are provided", "WARNING")
    # launch Flask
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
