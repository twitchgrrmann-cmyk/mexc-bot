from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime
import threading
from collections import deque
import traceback

app = Flask(__name__)

# =====================
# CONFIG - MATCHES PINE SCRIPT
# =====================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

SYMBOL = os.environ.get('SYMBOL', 'TAOUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 25))
MARGIN_MODE = 'cross'
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE', 15.0))
INITIAL_BALANCE = float(os.environ.get('INITIAL_BALANCE', 20.0))
STATE_FILE = os.environ.get('STATE_FILE', 'vb_state.json')
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
        
        # Auto-reset tracking
        self.reset_count = 0
        self.phase_1_resets = 0
        self.phase_2_resets = 0
        self.total_withdrawn = 0.0
        self.total_profit_generated = 0.0
        self.trading_paused = False

    def get_current_phase(self):
        return "growth" if self.starting_balance < PHASE_1_THRESHOLD else "extraction"

    def check_auto_reset(self):
        """Check if we should trigger auto-reset at 200% profit"""
        if self.current_balance >= self.starting_balance * (1 + PROFIT_RESET_THRESHOLD):
            profit = self.current_balance - self.starting_balance
            phase = self.get_current_phase()
            
            if phase == "growth":
                # PHASE 1: Reinvest 100%
                reinvest_pct = PHASE_1_REINVEST
                withdraw_amount = profit * (1 - reinvest_pct)
                log(f"🚀 PHASE 1 RESET: Growth Mode - Reinvesting 100%", "INFO")
                self.phase_1_resets += 1
            else:
                # PHASE 2: Withdraw 95%, Reinvest 5%
                reinvest_pct = PHASE_2_REINVEST
                withdraw_amount = profit * (1 - reinvest_pct)
                log(f"💰 PHASE 2 RESET: Extraction Mode - Withdrawing 95%!", "INFO")
                self.phase_2_resets += 1
            
            new_starting = self.starting_balance + (profit * reinvest_pct)
            
            log(f"🎉 200% PROFIT RESET TRIGGERED!")
            log(f"📊 Profit This Cycle: ${profit:.2f}")
            log(f"💸 Withdraw: ${withdraw_amount:.2f}")
            log(f"📈 New Starting Capital: ${new_starting:.2f}")
            log(f"🔄 Total Resets: {self.reset_count + 1} (P1: {self.phase_1_resets}, P2: {self.phase_2_resets})")
            
            # Update state
            self.starting_balance = new_starting
            self.current_balance = new_starting
            self.peak_balance = new_starting
            self.max_drawdown = 0.0
            self.total_withdrawn += withdraw_amount
            self.total_profit_generated += profit
            self.reset_count += 1
            
            save_state()
            return True
        return False

    def check_emergency_stop(self):
        """Check if drawdown exceeded emergency threshold"""
        if self.max_drawdown >= MAX_DRAWDOWN_STOP and not self.trading_paused:
            log(f"🚨 EMERGENCY STOP TRIGGERED!", "ERROR")
            log(f"📉 Drawdown: {self.max_drawdown:.2f}% (Limit: {MAX_DRAWDOWN_STOP}%)")
            log(f"💰 Current Balance: ${self.current_balance:.2f}")
            log(f"📊 Peak Balance: ${self.peak_balance:.2f}")
            log(f"🛑 Trading PAUSED - Manual review required")
            
            self.trading_paused = True
            
            # Close any open positions
            if self.current_position:
                log("🚨 Closing open position due to emergency stop")
                close_all_positions(SYMBOL)
                time.sleep(1)
                current_price = get_current_price(SYMBOL)
                if current_price:
                    self.close_position(current_price, reason="emergency_stop")
            
            save_state()
            return True
        return False

    def start_sync_thread(self):
        """Start background thread to sync with Bitget positions"""
        try:
            if self.sync_thread is None or not self.sync_thread.is_alive():
                self.stop_syncing.clear()
                self.sync_thread = threading.Thread(target=self.sync_with_bitget, daemon=True)
                self.sync_thread.start()
                log("✅ Started position sync thread")
            else:
                log("Sync thread already running")
        except Exception as e:
            log(f"Failed to start sync thread: {e}", "ERROR")

    def sync_with_bitget(self):
        """Periodically check Bitget for position mismatches"""
        while not self.stop_syncing.is_set():
            try:
                time.sleep(POSITION_SYNC_INTERVAL)
                
                # Get actual Bitget position
                bitget_pos = get_positions(SYMBOL)
                if bitget_pos.get('code') != '00000':
                    continue
                
                positions = bitget_pos.get('data', [])
                has_bitget_position = False
                bitget_side = None
                bitget_qty = 0
                
                for p in positions:
                    qty = float(p.get('total', 0))
                    if qty > 0:
                        has_bitget_position = True
                        bitget_side = p['holdSide']
                        bitget_qty = qty
                        break
                
                with self.position_lock:
                    # Case 1: Bot thinks it has position, but Bitget doesn't
                    if self.current_position and not has_bitget_position:
                        log(f"⚠️ SYNC: Bot has position but Bitget doesn't - assuming closed externally", "WARNING")
                        current_price = get_current_price(SYMBOL)
                        if current_price:
                            self.close_position(current_price, reason="external_close")
                    
                    # Case 2: Bitget has position, but bot doesn't know about it
                    elif not self.current_position and has_bitget_position:
                        log(f"⚠️ SYNC: Bitget has {bitget_side} position but bot doesn't - recovering", "WARNING")
                        current_price = get_current_price(SYMBOL)
                        if current_price:
                            self.current_position = {
                                'side': bitget_side,
                                'entry_price': current_price,
                                'qty': bitget_qty,
                                'tp_price': self.calculate_tp_sl(bitget_side, current_price)[0],
                                'sl_price': self.calculate_tp_sl(bitget_side, current_price)[1],
                                'open_time': datetime.now().isoformat(),
                                'recovered': True
                            }
                            save_state()
                            self._start_monitoring()
                            log(f"✅ SYNC: Recovered {bitget_side} position, now monitoring")
                    
                    # Case 3: Both have position - verify they match
                    elif self.current_position and has_bitget_position:
                        if self.current_position['side'] != bitget_side:
                            log(f"⚠️ SYNC: Side mismatch! Bot: {self.current_position['side']}, Bitget: {bitget_side}", "ERROR")
                            current_price = get_current_price(SYMBOL)
                            if current_price:
                                self.close_position(current_price, reason="side_mismatch")
                                self.current_position = {
                                    'side': bitget_side,
                                    'entry_price': current_price,
                                    'qty': bitget_qty,
                                    'tp_price': self.calculate_tp_sl(bitget_side, current_price)[0],
                                    'sl_price': self.calculate_tp_sl(bitget_side, current_price)[1],
                                    'open_time': datetime.now().isoformat(),
                                    'recovered': True
                                }
                                save_state()
                                self._start_monitoring()
                
                self.last_sync_time = time.time()
                
            except Exception as e:
                log(f"Sync error: {e}\n{traceback.format_exc()}", "ERROR")
        
        log("Position sync thread stopped")

    def open_position(self, side, entry_price, qty):
        with self.position_lock:
            if self.current_position:
                log(f"Already have open position, skipping", "WARNING")
                return False
            
            tp_price, sl_price = self.calculate_tp_sl(side, entry_price)
            self.current_position = {
                'side': side,
                'entry_price': entry_price,
                'qty': qty,
                'tp_price': tp_price,
                'sl_price': sl_price,
                'open_time': datetime.now().isoformat()
            }
            save_state()
            log(f"📝 Opened {side} {qty} @ {entry_price} | TP: {tp_price:.2f}, SL: {sl_price:.2f}")
            
            self._start_monitoring()
            return True

    def _start_monitoring(self):
        """Start or restart monitoring thread"""
        self.stop_monitoring.clear()
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self.monitor_thread = threading.Thread(target=self.monitor_position, daemon=True)
            self.monitor_thread.start()
            log("Started position monitor thread")
        else:
            log("Monitor thread already running")

    def calculate_tp_sl(self, side, entry_price):
        if side=='long':
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
            sl_price = entry_price * (1 - STOP_LOSS_PCT / 100)
        else:
            tp_price = entry_price * (1 - TAKE_PROFIT_PCT / 100)
            sl_price = entry_price * (1 + STOP_LOSS_PCT / 100)
        return tp_price, sl_price

    def monitor_position(self):
        consecutive_failures = 0
        log("🔍 Started monitoring position")
        
        try:
            while not self.stop_monitoring.is_set():
                try:
                    with self.position_lock:
                        if not self.current_position:
                            log("Position closed externally, stopping monitor")
                            break
                        
                        current_price = get_current_price(SYMBOL)
                        if not current_price:
                            consecutive_failures += 1
                            log(f"Price fetch failed ({consecutive_failures}/{MAX_PRICE_FAILURES})", "WARNING")
                            if consecutive_failures >= MAX_PRICE_FAILURES:
                                log("Max price failures reached, emergency close", "ERROR")
                                self._emergency_close()
                                break
                            time.sleep(PRICE_CHECK_INTERVAL)
                            continue
                        
                        consecutive_failures = 0
                        side = self.current_position['side']
                        tp, sl = self.current_position['tp_price'], self.current_position['sl_price']
                        
                        # Check TP/SL
                        hit_tp = (side=='long' and current_price >= tp) or (side=='short' and current_price <= tp)
                        hit_sl = (side=='long' and current_price <= sl) or (side=='short' and current_price >= sl)
                        
                        if hit_tp or hit_sl:
                            reason = "TP" if hit_tp else "SL"
                            log(f"⚡ {reason} hit for {side} at {current_price}")
                            close_all_positions(SYMBOL)
                            time.sleep(1)
                            self.close_position(current_price, reason=reason)
                            break
                    
                    time.sleep(PRICE_CHECK_INTERVAL)
                    
                except Exception as e:
                    log(f"Monitor loop error: {e}\n{traceback.format_exc()}", "ERROR")
                    time.sleep(PRICE_CHECK_INTERVAL)
        
        except Exception as e:
            log(f"Monitor thread crashed: {e}\n{traceback.format_exc()}", "ERROR")
        finally:
            log("🛑 Monitor thread stopped")

    def _emergency_close(self):
        if self.current_position:
            log("🚨 Emergency closing position", "ERROR")
            close_all_positions(SYMBOL)
            time.sleep(1)
            self.close_position(self.current_position['entry_price'], reason="emergency")

    def close_position(self, exit_price, reason="normal"):
        with self.position_lock:
            if not self.current_position:
                return 0
            
            self.stop_monitoring.set()
            
            side = self.current_position['side']
            entry_price = self.current_position['entry_price']
            qty = self.current_position['qty']
            
            # Calculate P&L
            price_change = (exit_price - entry_price)/entry_price if side=='long' else (entry_price - exit_price)/entry_price
            pnl = qty * entry_price * price_change
            
            # Update balance and stats
            self.current_balance += pnl
            self.total_pnl += pnl
            self.total_trades += 1
            self.recent_pnls.append(pnl)
            
            if pnl > 0:
                self.winning_trades += 1
                self.consecutive_losses = 0
            else:
                self.losing_trades += 1
                self.consecutive_losses += 1
            
            # Track drawdown
            if self.current_balance > self.peak_balance:
                self.peak_balance = self.current_balance
            drawdown = (self.peak_balance - self.current_balance) / self.peak_balance * 100
            if drawdown > self.max_drawdown:
                self.max_drawdown = drawdown
            
            # Track daily trades
            today = datetime.now().date()
            if self.last_trade_date != today:
                self.daily_trade_count = 0
                self.last_trade_date = today
            self.daily_trade_count += 1
            
            # Save trade
            self.trade_history.append({
                'side': side,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'qty': qty,
                'pnl': pnl,
                'balance_after': self.current_balance,
                'close_time': datetime.now().isoformat(),
                'close_reason': reason
            })
            
            self.current_position = None
            save_state()
            
            log(f"💰 Closed {side} ({reason}) | P&L: {pnl:+.2f} | Balance: {self.current_balance:.2f} | DD: {drawdown:.2f}%")
            
            if self.consecutive_losses >= 3:
                log(f"WARNING: {self.consecutive_losses} consecutive losses!", "WARNING")
            
            # Check for auto-reset after closing position
            self.check_auto_reset()
            
            # Check for emergency stop
            self.check_emergency_stop()
            
            return pnl

    def should_trade(self):
        # Emergency stop override
        if self.trading_paused:
            log(f"Trading paused due to emergency stop", "WARNING")
            return False
        
        # Daily drawdown circuit breaker
        daily_drawdown = self._calculate_daily_drawdown()
        if daily_drawdown > 20.0:
            log(f"DAILY CIRCUIT BREAKER: {daily_drawdown:.2f}% loss today", "WARNING")
            return False
        
        # Consecutive losses
        if self.consecutive_losses >= 5:
            log(f"Too many consecutive losses: {self.consecutive_losses}", "WARNING")
            return False
        
        # Balance check
        if self.current_balance < self.initial_balance * 0.3:
            log(f"Balance too low: {self.current_balance:.2f}", "WARNING")
            return False
        
        return True
    
    def _calculate_daily_drawdown(self):
        if not self.trade_history:
            return 0.0
        
        today = datetime.now().date()
        today_trades = [t for t in self.trade_history 
                       if datetime.fromisoformat(t['close_time']).date() == today]
        
        if not today_trades:
            return 0.0
        
        daily_pnl = sum(t['pnl'] for t in today_trades)
        balance_start_of_day = self.current_balance - daily_pnl
        
        if balance_start_of_day <= 0:
            return 0.0
        
        return abs(min(0, daily_pnl) / balance_start_of_day * 100)

    def get_stats(self):
        win_rate = (self.winning_trades/self.total_trades*100) if self.total_trades else 0
        roi = ((self.current_balance - self.starting_balance)/self.starting_balance*100)
        avg_win = sum(p for p in self.recent_pnls if p > 0) / max(sum(1 for p in self.recent_pnls if p > 0), 1)
        avg_loss = sum(p for p in self.recent_pnls if p < 0) / max(sum(1 for p in self.recent_pnls if p < 0), 1)
        
        next_reset_at = self.starting_balance * (1 + PROFIT_RESET_THRESHOLD)
        
        return {
            'status': 'emergency_stopped' if self.trading_paused else 'live',
            'current_phase': self.get_current_phase(),
            'initial_balance': self.initial_balance,
            'starting_balance': self.starting_balance,
            'current_balance': self.current_balance,
            'total_pnl': self.total_pnl,
            'roi_percent': roi,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': win_rate,
            'max_drawdown': self.max_drawdown,
            'consecutive_losses': self.consecutive_losses,
            'daily_trades': self.daily_trade_count,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': abs(avg_win / avg_loss) if avg_loss != 0 else 0,
            'has_open_position': self.current_position is not None,
            'can_trade': self.should_trade(),
            'last_sync': datetime.fromtimestamp(self.last_sync_time).isoformat() if self.last_sync_time > 0 else 'never',
            'monitor_alive': self.monitor_thread.is_alive() if self.monitor_thread else False,
            'sync_alive': self.sync_thread.is_alive() if self.sync_thread else False,
            
            # Auto-reset stats
            'reset_count': self.reset_count,
            'phase_1_resets': self.phase_1_resets,
            'phase_2_resets': self.phase_2_resets,
            'total_withdrawn': self.total_withdrawn,
            'total_profit_generated': self.total_profit_generated,
            'next_reset_at': next_reset_at,
            'phase_1_threshold': PHASE_1_THRESHOLD,
            'emergency_stop_threshold': MAX_DRAWDOWN_STOP
        }

virtual_balance = VirtualBalance(INITIAL_BALANCE)

# =====================
# STATE
# =====================
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
            'trading_paused': virtual_balance.trading_paused
        }
        with open(STATE_FILE,'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"Failed to save state: {e}", "ERROR")

def load_state():
    global virtual_balance
    try:
        with open(STATE_FILE,'r') as f:
            st=json.load(f)
        vb=VirtualBalance(st.get('initial_balance', INITIAL_BALANCE))
        vb.starting_balance = st.get('starting_balance', INITIAL_BALANCE)
        vb.current_balance = st.get('current_balance', INITIAL_BALANCE)
        vb.total_trades = st.get('total_trades',0)
        vb.winning_trades = st.get('winning_trades',0)
        vb.losing_trades = st.get('losing_trades',0)
        vb.total_pnl = st.get('total_pnl',0.0)
        vb.trade_history = st.get('trade_history',[])
        vb.current_position = st.get('current_position',None)
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
        log("✅ Loaded virtual balance")
        
        # Start sync thread
        vb.start_sync_thread()
        
        # Restart monitoring if position exists
        if vb.current_position:
            log("🔄 Restarting position monitor after reload")
            vb._start_monitoring()
    except FileNotFoundError:
        log("ℹ️ No saved state found, starting fresh")
    except Exception as e:
        log(f"Failed to load state: {e}", "ERROR")

# =====================
# BITGET API
# =====================
def generate_signature(timestamp, method, request_path, body, secret):
    body_str = json.dumps(body) if body else ""
    message = timestamp+method+request_path+body_str
    return base64.b64encode(hmac.new(secret.encode(),message.encode(),hashlib.sha256).digest()).decode()

def bitget_request(method, endpoint, params=None, retries=3):
    for attempt in range(retries):
        try:
            timestamp=str(int(time.time()*1000))
            body=params if params else None
            sign=generate_signature(timestamp, method, endpoint, body, BITGET_SECRET_KEY)
            headers={
                'ACCESS-KEY':BITGET_API_KEY,
                'ACCESS-SIGN':sign,
                'ACCESS-TIMESTAMP':timestamp,
                'ACCESS-PASSPHRASE':BITGET_PASSPHRASE,
                'Content-Type':'application/json'
            }
            url=BASE_URL+endpoint
            
            if method=="POST":
                r=requests.post(url,json=body,headers=headers,timeout=10)
            else:
                r=requests.get(url,headers=headers,timeout=10)
            
            return r.json()
        except Exception as e:
            log(f"API request failed (attempt {attempt+1}/{retries}): {e}", "WARNING")
            if attempt < retries - 1:
                time.sleep(1)
    return {'error':'request_failed'}

def set_leverage(symbol, leverage):
    for side in ['long','short']:
        result = bitget_request("POST","/api/mix/v1/account/setLeverage",
                               {'symbol':symbol,'marginCoin':'USDT','leverage':leverage,'holdSide':side})
        if result.get('code') != '00000':
            log(f"Failed to set {side} leverage: {result}", "WARNING")

def get_current_price(symbol, retries=2):
    for attempt in range(retries):
        try:
            data=requests.get(BASE_URL+f"/api/mix/v1/market/ticker?symbol={symbol}",timeout=5).json()
            if data.get('code')=='00000' and data.get('data'):
                return float(data['data']['last'])
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(0.5)
    return None

def calculate_position_size(balance, price, leverage, risk_pct):
    position_value = balance * (risk_pct / 100.0) * leverage
    qty = round(position_value / price, 3)
    return max(qty, 0.001)

def place_order(symbol, side, size):
    endpoint="/api/mix/v1/order/placeOrder"
    params={'symbol':symbol,'marginCoin':'USDT','side':side,'orderType':'market','size':str(size)}
    result=bitget_request("POST",endpoint,params)
    log(f"📤 Order: {side} {size} -> {result}")
    return result

def get_positions(symbol):
    return bitget_request("GET",f"/api/mix/v1/position/singlePosition?symbol={symbol}&marginCoin=USDT")

def close_all_positions(symbol):
    pos = get_positions(symbol)
    if pos.get('code')=='00000':
        for p in pos.get('data',[]):
            if float(p.get('total',0))>0:
                side='close_long' if p['holdSide']=='long' else 'close_short'
                place_order(symbol,side,float(p['total']))
                log(f"✅ Closed {p['holdSide']} {p['total']}")

# =====================
# WEBHOOK
# =====================
@app.route('/webhook',methods=['POST','GET'])
def webhook():
    global last_signal_time
    
    if request.method=='GET':
        return jsonify({
            "virtual_balance":virtual_balance.get_stats(),
            "config": {
                "leverage": LEVERAGE,
                "risk_pct": RISK_PERCENTAGE,
                "tp_pct": TAKE_PROFIT_PCT,
                "sl_pct": STOP_LOSS_PCT,
                "phase_1_threshold": PHASE_1_THRESHOLD,
                "phase_2_withdraw_pct": 95.0,
                "emergency_stop_dd": MAX_DRAWDOWN_STOP
            },
            "uptime": datetime.now().isoformat()
        }),200

    raw_data=request.get_data(as_text=True)
    try:
        data=json.loads(raw_data)
    except:
        return jsonify({'error':'Invalid JSON'}),400
    
    if data.get('secret')!=WEBHOOK_SECRET:
        return jsonify({'error':'Unauthorized'}),401

    # Debounce
    now=time.time()
    if now-last_signal_time<DEBOUNCE_SEC:
        return jsonify({'success':True,'action':'debounced'})
    last_signal_time=now

    # Risk management check
    if not virtual_balance.should_trade():
        return jsonify({
            'success': False,
            'action': 'blocked',
            'reason': 'risk_limits_exceeded' if not virtual_balance.trading_paused else 'emergency_stopped',
            'stats': virtual_balance.get_stats()
        })

    action=data.get('action','').upper()
    price=get_current_price(SYMBOL)
    if not price:
        return jsonify({'error':'Price fetch failed'}),500

    # Handle existing position
    with virtual_balance.position_lock:
        if virtual_balance.current_position:
            current_side = virtual_balance.current_position['side']
            
            # Same direction - ignore
            if (action in ['BUY','LONG'] and current_side=='long') or \
               (action in ['SELL','SHORT'] and current_side=='short'):
                return jsonify({'success':True,'action':'ignored','reason':'already_in_position'})
            
            # Opposite direction - close first
            log(f"🔄 Closing {current_side} to open {action}")
            close_all_positions(SYMBOL)
            time.sleep(0.5)
            virtual_balance.close_position(price, reason="signal_flip")

    # Set leverage only
    set_leverage(SYMBOL,LEVERAGE)

    # Execute new position
    if action in ['BUY','LONG']:
        qty=calculate_position_size(virtual_balance.current_balance, price, LEVERAGE, RISK_PERCENTAGE)
        result = place_order(SYMBOL,'open_long',qty)
        if result.get('code') == '00000':
            virtual_balance.open_position('long',price,qty)
        else:
            return jsonify({'error':'Order failed','details':result}),500
            
    elif action in ['SELL','SHORT']:
        qty=calculate_position_size(virtual_balance.current_balance, price, LEVERAGE, RISK_PERCENTAGE)
        result = place_order(SYMBOL,'open_short',qty)
        if result.get('code') == '00000':
            virtual_balance.open_position('short',price,qty)
        else:
            return jsonify({'error':'Order failed','details':result}),500
            
    elif action=='CLOSE':
        if virtual_balance.current_position:
            close_all_positions(SYMBOL)
            time.sleep(0.5)
            virtual_balance.close_position(price, reason="manual_close")
        else:
            return jsonify({'success':True,'action':'no_position_to_close'})
    else:
        return jsonify({'error':f'Invalid action: {action}'}),400

    return jsonify({
        'success':True,
        'action':action,
        'price':price,
        'qty': qty if action in ['BUY','LONG','SELL','SHORT'] else None,
        'position_value': qty * price if action in ['BUY','LONG','SELL','SHORT'] else None,
        'virtual_balance':virtual_balance.get_stats(),
        'timestamp':datetime.now().isoformat()
    })

@app.route('/resume',methods=['POST'])
def resume_trading():
    """Resume trading after emergency stop"""
    try:
        data = request.json
        if data.get('secret') != WEBHOOK_SECRET:
            return jsonify({'error':'Unauthorized'}),401
        
        if virtual_balance.trading_paused:
            virtual_balance.trading_paused = False
            virtual_balance.max_drawdown = 0.0  # Reset drawdown tracker
            save_state()
            log("✅ Trading resumed by manual override")
            return jsonify({
                'success': True,
                'message': 'Trading resumed',
                'stats': virtual_balance.get_stats()
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Trading was not paused'
            })
    except Exception as e:
        log(f"Resume error: {e}", "ERROR")
        return jsonify({'error': str(e)}), 500

# =====================
# MAIN
# =====================
if __name__=="__main__":
    log("🚀 Bitget Auto-Reset Bot - PHASE 1 & 2")
    log(f"📊 Symbol: {SYMBOL} | Leverage: {LEVERAGE}x | Risk: {RISK_PERCENTAGE}%")
    log(f"🎯 TP: {TAKE_PROFIT_PCT}% | SL: {STOP_LOSS_PCT}%")
    log(f"💰 Phase 1 Threshold: ${PHASE_1_THRESHOLD} (100% reinvest)")
    log(f"💸 Phase 2: 95% withdraw, 5% reinvest")
    log(f"🛑 Emergency Stop: {MAX_DRAWDOWN_STOP}% drawdown")
    
    # Load state and start threads
    load_state()
    
    # Force start sync thread with retry
    max_retries = 3
    for attempt in range(max_retries):
        if virtual_balance.sync_thread and virtual_balance.sync_thread.is_alive():
            log("✅ Sync thread confirmed running")
            break
        log(f"Attempting to start sync thread (attempt {attempt + 1}/{max_retries})", "WARNING")
        virtual_balance.start_sync_thread()
        time.sleep(1)
    
    if not virtual_balance.sync_thread or not virtual_balance.sync_thread.is_alive():
        log("❌ CRITICAL: Sync thread failed to start!", "ERROR")
    
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT",5000)),debug=False)