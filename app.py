from flask import Flask, request, jsonify
import hmac, hashlib, requests, time, json, base64, os
from datetime import datetime
import threading
from collections import deque

app = Flask(__name__)

# =====================
# CONFIG - MATCH YOUR WINNING PINE SCRIPT SETTINGS
# =====================
BITGET_API_KEY = os.environ.get('BITGET_API_KEY', '')
BITGET_SECRET_KEY = os.environ.get('BITGET_SECRET_KEY', '')
BITGET_PASSPHRASE = os.environ.get('BITGET_PASSPHRASE', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', 'Grrtrades')

SYMBOL = os.environ.get('SYMBOL', 'TAOUSDT_UMCBL')
LEVERAGE = int(os.environ.get('LEVERAGE', 25))  # Changed from 15 to 25
MARGIN_MODE = 'cross'
RISK_PERCENTAGE = float(os.environ.get('RISK_PERCENTAGE', 40.0))  # Changed from 50 to 40
STARTING_BALANCE = float(os.environ.get('STARTING_BALANCE', 20.0))  # Changed from 15 to 20
MAX_POSITION_USD = float(os.environ.get('MAX_POSITION_USD', 300.0))  # NEW: Position cap
STATE_FILE = os.environ.get('STATE_FILE', 'vb_state.json')
DEBOUNCE_SEC = float(os.environ.get('DEBOUNCE_SEC', 2.0))
PRICE_CHECK_INTERVAL = 1.0
MAX_PRICE_FAILURES = 5

# TP/SL CONFIG - MATCH YOUR WINNING SETTINGS
TAKE_PROFIT_PCT = float(os.environ.get('TAKE_PROFIT_PCT', 1.2))  # Changed from 0.8 to 1.2
STOP_LOSS_PCT = float(os.environ.get('STOP_LOSS_PCT', 0.7))  # Changed from 0.9 to 0.7

LIVE_MODE = True
BASE_URL = "https://api.bitget.com"
last_signal_time = 0

# =====================
# VIRTUAL BALANCE WITH RISK MANAGEMENT
# =====================
class VirtualBalance:
    def __init__(self, starting_balance):
        self.starting_balance = starting_balance
        self.current_balance = starting_balance
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
        self.peak_balance = starting_balance
        self.consecutive_losses = 0
        self.recent_pnls = deque(maxlen=10)
        self.daily_trade_count = 0
        self.last_trade_date = None

    def open_position(self, side, entry_price, qty):
        with self.position_lock:
            if self.current_position:
                print(f"⚠️ Already have open position, skipping")
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
            print(f"📝 Opened {side} {qty} @ {entry_price} | TP: {tp_price:.2f}, SL: {sl_price:.2f}")
            
            # Start monitoring thread
            self.stop_monitoring.clear()
            self.monitor_thread = threading.Thread(target=self.monitor_position, daemon=True)
            self.monitor_thread.start()
            return True

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
        print(f"🔍 Started monitoring position")
        
        while not self.stop_monitoring.is_set():
            with self.position_lock:
                if not self.current_position:
                    print(f"✅ Position closed externally, stopping monitor")
                    break
                
                current_price = get_current_price(SYMBOL)
                if not current_price:
                    consecutive_failures += 1
                    print(f"⚠️ Price fetch failed ({consecutive_failures}/{MAX_PRICE_FAILURES})")
                    if consecutive_failures >= MAX_PRICE_FAILURES:
                        print(f"❌ Max price failures reached, emergency close")
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
                    print(f"⚡ {reason} hit for {side} at {current_price}")
                    self.close_position(current_price)
                    close_all_positions(SYMBOL)
                    break
            
            time.sleep(PRICE_CHECK_INTERVAL)
        
        print(f"🛑 Monitor thread stopped")

    def _emergency_close(self):
        if self.current_position:
            print(f"🚨 Emergency closing position")
            close_all_positions(SYMBOL)
            self.close_position(self.current_position['entry_price'])

    def close_position(self, exit_price):
        with self.position_lock:
            if not self.current_position:
                return 0
            
            self.stop_monitoring.set()
            
            side = self.current_position['side']
            entry_price = self.current_position['entry_price']
            qty = self.current_position['qty']
            
            # Calculate P&L
            price_change = (exit_price - entry_price)/entry_price if side=='long' else (entry_price - exit_price)/entry_price
            pnl = qty * entry_price * price_change * LEVERAGE
            
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
                'close_time': datetime.now().isoformat()
            })
            
            self.current_position = None
            save_state()
            
            print(f"💰 Closed {side} | P&L: {pnl:+.2f} | Balance: {self.current_balance:.2f} | DD: {drawdown:.2f}%")
            
            if self.consecutive_losses >= 3:
                print(f"⚠️ WARNING: {self.consecutive_losses} consecutive losses!")
            
            return pnl

    def should_trade(self):
        # Daily drawdown circuit breaker
        daily_drawdown = self._calculate_daily_drawdown()
        if daily_drawdown > 15.0:
            print(f"🚨 DAILY CIRCUIT BREAKER: {daily_drawdown:.2f}% loss today")
            return False
        
        # Max drawdown
        if self.max_drawdown > 30:
            print(f"🛑 Max drawdown {self.max_drawdown:.2f}% exceeded")
            return False
        
        # Consecutive losses
        if self.consecutive_losses >= 5:
            print(f"🛑 Too many consecutive losses: {self.consecutive_losses}")
            return False
        
        # Balance check
        if self.current_balance < self.starting_balance * 0.5:
            print(f"🛑 Balance too low: {self.current_balance:.2f}")
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
        
        return {
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
            'can_trade': self.should_trade()
        }

virtual_balance = VirtualBalance(STARTING_BALANCE)

# =====================
# STATE
# =====================
def save_state():
    try:
        state = {
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
            'consecutive_losses': virtual_balance.consecutive_losses
        }
        with open(STATE_FILE,'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to save state: {e}")

def load_state():
    global virtual_balance
    try:
        with open(STATE_FILE,'r') as f:
            st=json.load(f)
        vb=VirtualBalance(st.get('starting_balance', STARTING_BALANCE))
        vb.current_balance = st.get('current_balance', STARTING_BALANCE)
        vb.total_trades = st.get('total_trades',0)
        vb.winning_trades = st.get('winning_trades',0)
        vb.losing_trades = st.get('losing_trades',0)
        vb.total_pnl = st.get('total_pnl',0.0)
        vb.trade_history = st.get('trade_history',[])
        vb.current_position = st.get('current_position',None)
        vb.max_drawdown = st.get('max_drawdown', 0.0)
        vb.peak_balance = st.get('peak_balance', STARTING_BALANCE)
        vb.consecutive_losses = st.get('consecutive_losses', 0)
        virtual_balance = vb
        print("✅ Loaded virtual balance")
        
        # Restart monitoring if position exists
        if vb.current_position:
            print("🔄 Restarting position monitor after reload")
            vb.stop_monitoring.clear()
            vb.monitor_thread = threading.Thread(target=vb.monitor_position, daemon=True)
            vb.monitor_thread.start()
    except FileNotFoundError:
        print("ℹ️ No saved state found, starting fresh")
    except Exception as e:
        print(f"❌ Failed to load state: {e}")

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
            print(f"⚠️ API request failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(1)
    return {'error':'request_failed'}

def set_leverage(symbol, leverage):
    for side in ['long','short']:
        result = bitget_request("POST","/api/mix/v1/account/setLeverage",
                               {'symbol':symbol,'marginCoin':'USDT','leverage':leverage,'holdSide':side})
        if result.get('code') != '00000':
            print(f"⚠️ Failed to set {side} leverage: {result}")

def set_margin_mode(symbol, margin_mode):
    result = bitget_request("POST","/api/mix/v1/account/setMarginMode",
                           {'symbol':symbol,'marginCoin':'USDT','marginMode':margin_mode})
    if result.get('code') != '00000':
        print(f"⚠️ Failed to set margin mode: {result}")

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

def calculate_position_size(balance, price, leverage, risk_pct, max_position_usd=MAX_POSITION_USD):
    # Base calculation
    usable_balance = balance * (risk_pct / 100.0)
    base_position_value = usable_balance * leverage
    
    # Apply position cap
    capped_position_value = min(base_position_value, max_position_usd)
    
    # Calculate quantity
    qty = round(capped_position_value / price, 3)
    return max(qty, 0.001)

def place_order(symbol, side, size):
    endpoint="/api/mix/v1/order/placeOrder"
    params={'symbol':symbol,'marginCoin':'USDT','side':side,'orderType':'market','size':str(size)}
    result=bitget_request("POST",endpoint,params)
    print(f"📤 Order: {side} {size} -> {result}")
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
                print(f"✅ Closed {p['holdSide']} {p['total']}")

# =====================
# WEBHOOK
# =====================
@app.route('/webhook',methods=['POST','GET'])
def webhook():
    global last_signal_time
    
    if request.method=='GET':
        return jsonify({
            "status":"live",
            "virtual_balance":virtual_balance.get_stats(),
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
            'reason': 'risk_limits_exceeded',
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
            print(f"🔄 Closing {current_side} to open {action}")
            virtual_balance.close_position(price)
            close_all_positions(SYMBOL)
            time.sleep(0.5)

    # Set margin/leverage
    set_margin_mode(SYMBOL,MARGIN_MODE)
    set_leverage(SYMBOL,LEVERAGE)

    # Execute new position
    if action in ['BUY','LONG']:
        qty=calculate_position_size(virtual_balance.current_balance, price, LEVERAGE, RISK_PERCENTAGE, MAX_POSITION_USD)
        result = place_order(SYMBOL,'open_long',qty)
        if result.get('code') == '00000':
            virtual_balance.open_position('long',price,qty)
        else:
            return jsonify({'error':'Order failed','details':result}),500
            
    elif action in ['SELL','SHORT']:
        qty=calculate_position_size(virtual_balance.current_balance, price, LEVERAGE, RISK_PERCENTAGE, MAX_POSITION_USD)
        result = place_order(SYMBOL,'open_short',qty)
        if result.get('code') == '00000':
            virtual_balance.open_position('short',price,qty)
        else:
            return jsonify({'error':'Order failed','details':result}),500
            
    elif action=='CLOSE':
        if virtual_balance.current_position:
            virtual_balance.close_position(price)
            close_all_positions(SYMBOL)
        else:
            return jsonify({'success':True,'action':'no_position_to_close'})
    else:
        return jsonify({'error':f'Invalid action: {action}'}),400

    return jsonify({
        'success':True,
        'action':action,
        'price':price,
        'virtual_balance':virtual_balance.get_stats(),
        'timestamp':datetime.now().isoformat()
    })

# =====================
# MAIN
# =====================
if __name__=="__main__":
    print("🚀 Bitget Micro-Scalper - OPTIMIZED")
    print(f"📊 Symbol: {SYMBOL} | Leverage: {LEVERAGE}x | Risk: {RISK_PERCENTAGE}%")
    print(f"🎯 TP: {TAKE_PROFIT_PCT}% | SL: {STOP_LOSS_PCT}% | Max Position: ${MAX_POSITION_USD}")
    load_state()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT",5000)),debug=False)