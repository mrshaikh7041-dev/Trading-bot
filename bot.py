# live_binance_perp_ws_full_ready_final.py
import ccxt, pandas as pd, time, threading, websocket, json, os, csv, logging, sys, requests
from collections import deque
from datetime import datetime, timedelta

# ========== CONFIG ==========
SYMBOL = 'BNB/USDT'
WS_SYMBOL = 'bnbusdt'
LOT_SIZE = 0.01
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 75
COOLDOWN_MINUTES = 30

CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'
API_KEY = 'czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ'
API_SECRET = 'cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO'
FUTURES_REST_BASE = 'https://fapi.binance.com'

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

exchange = ccxt.binance({'apiKey': API_KEY,'secret': API_SECRET,'enableRateLimit': True,'options': {'defaultType': 'future'},'timeout': 20000})
exchange.options['adjustForTimeDifference'] = True
exchange.load_markets()

# ========== STATE ==========
in_position = False
position = None
cooldown_until = None
last_processed_candle_time = None
kline_deque = deque(maxlen=500)
listen_key = None
listen_key_lock = threading.Lock()
ws_user = ws_kline = None
stop_all = False
balance_lock = threading.Lock()
current_balance = None
user_ws_alive = False

# ========== UTILITIES ==========
def fetch_usdt_balance():
    try:
        bal = exchange.fetch_balance({'type': 'future'})
        return float(bal['USDT'].get('total') or bal['USDT'].get('free') or 0)
    except: return 0

def append_trade_csv(record):
    header = ['time', 'dir', 'entry', 'exit', 'outcome', 'pnl', 'balance']
    exists = os.path.isfile(CSV_FN)
    with open(CSV_FN,'a',newline='',encoding='utf-8') as f:
        writer = csv.DictWriter(f,fieldnames=header)
        if not exists: writer.writeheader()
        writer.writerow(record)

def get_price_precision(symbol):
    try:
        m = exchange.markets.get(symbol)
        if m: return m.get('precision', {}).get('price', 2)
    except: pass
    return 2

def price_round(symbol, price):
    return round(price, get_price_precision(symbol))

def _round_amount(symbol, amount):
    try:
        precision = exchange.markets.get(symbol).get('precision', {}).get('amount')
        if precision is not None: return round(amount, precision)
    except: pass
    return amount

# ========== ORDERS ==========
def set_leverage(symbol, leverage):
    try:
        sym = symbol.replace('/','')
        exchange.request('fapi/v1/leverage','POST',{'symbol':sym,'leverage':int(leverage)})
    except Exception as e:
        logging.warning(f"Set leverage failed: {e}")

def create_market_entry(symbol, side, amount):
    try:
        amt = _round_amount(symbol, amount)
        order = exchange.create_order(symbol, 'market', side.lower(), amt, None, {'reduceOnly': False})
        return order
    except Exception as e:
        logging.error(f"Market entry failed: {e}")
        raise

def place_tp_sl(symbol, side, amount, tp_price, sl_price):
    close_side = 'sell' if side=='BUY' else 'buy'
    amt = _round_amount(symbol, amount)
    tp_order = sl_order = None
    try: tp_order = exchange.create_order(symbol,'TAKE_PROFIT_MARKET',close_side,amt,None,{'stopPrice':float(tp_price),'reduceOnly':True})
    except: pass
    try: sl_order = exchange.create_order(symbol,'STOP_MARKET',close_side,amt,None,{'stopPrice':float(sl_price),'reduceOnly':True})
    except: pass
    return tp_order, sl_order

# ========== EMA STRATEGY ==========
def compute_emas_from_deque():
    if len(kline_deque)<12: return None
    df = pd.DataFrame(list(kline_deque),columns=['open','high','low','close','volume','time_ms'])
    df['close'] = df['close'].astype(float)
    df['ema5'] = df['close'].ewm(span=5,adjust=False).mean()
    df['ema9'] = df['close'].ewm(span=9,adjust=False).mean()
    df['ema15'] = df['close'].ewm(span=15,adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21,adjust=False).mean()
    return df

def check_signal_from_df(df):
    last = df.iloc[-1]
    c,l,h = float(last['close']),float(last['low']),float(last['high'])
    ema5,ema9,ema15,ema21 = float(last['ema5']),float(last['ema9']),float(last['ema15']),float(last['ema21'])
    if c>=ema5 and c>=ema9 and c>=ema15 and c>ema21: return 'BUY'
    if c<=ema5 and c<=ema9 and c<=ema15 and c<ema21: return 'SELL'
    if l<=ema15<=h and c>ema5: return 'BUY'
    if l<=ema15<=h and c<ema5: return 'SELL'
    return None

# ========== KLINE WS ==========
def on_kline_message(ws,message):
    global last_processed_candle_time,in_position,position,cooldown_until
    try:
        data=json.loads(message)
        payload=data.get('data') if isinstance(data,dict) and data.get('data') else data
        k=payload.get('k') or {}
        if not k or not k.get('x'): return
        o,h,l,c,v,t=float(k.get('o')),float(k.get('h')),float(k.get('l')),float(k.get('c')),float(k.get('v')),int(k.get('t'))
        kline_deque.append([o,h,l,c,v,t])
        df=compute_emas_from_deque()
        if df is None: print("[DEBUG] Not enough candles for EMA"); return
        signal=check_signal_from_df(df)
        if cooldown_until and datetime.utcnow()<cooldown_until: return
        if signal and not in_position:
            if last_processed_candle_time==t: return
            entry_price=c
            if signal=='BUY': tp,sl=price_round(SYMBOL,entry_price+TP_POINTS),price_round(SYMBOL,entry_price-SL_POINTS)
            else: tp,sl=price_round(SYMBOL,entry_price-TP_POINTS),price_round(SYMBOL,entry_price+SL_POINTS)
            try:
                set_leverage(SYMBOL,LEVERAGE)
                side_ccxt='buy' if signal=='BUY' else 'sell'
                entry_ord=create_market_entry(SYMBOL,side_ccxt,LOT_SIZE)
                entry_price_actual=entry_ord.get('average') or entry_ord.get('price') or entry_price
                tp_ord,sl_ord=place_tp_sl(SYMBOL,signal,LOT_SIZE,tp,sl)
                position={'dir':signal,'entry':float(entry_price_actual),'tp_price':float(tp),'sl_price':float(sl),'entry_time':datetime.utcnow(),'entry_id':str(entry_ord.get('id')),'tp_id':str(tp_ord.get('id')) if tp_ord else None,'sl_id':str(sl_ord.get('id')) if sl_ord else None}
                in_position=True
                last_processed_candle_time=t
                print(f"[INFO] OPENED {signal} entry={position['entry']} TP={position['tp_price']} SL={position['sl_price']}",flush=True)
            except Exception as e: logging.error(f"Trade placement error: {e}")
    except Exception as e: logging.debug(f"Kline parse error: {e}")

def on_kline_open(ws): print("[INFO] Kline WS connected",flush=True)
def on_kline_close(ws,code,reason): print(f"[WARNING] Kline WS closed: {code} {reason}",flush=True)
def on_kline_error(ws,err): print(f"[ERROR] Kline WS error: {err}",flush=True)

def start_kline_ws():
    global ws_kline, stop_all
    ws_url=f"wss://fstream.binance.com/ws/{WS_SYMBOL}@kline_1m"
    while not stop_all:
        try:
            ws_kline=websocket.WebSocketApp(ws_url,on_message=on_kline_message,on_open=on_kline_open,on_close=on_kline_close,on_error=on_kline_error)
            ws_kline.run_forever(ping_interval=60,ping_timeout=10)
        except Exception as e: logging.error(f"Kline WS run_forever error: {e}")
        time.sleep(2)

# ========== USER WS ==========
def create_listenkey_via_requests():
    try:
        r=requests.post(FUTURES_REST_BASE+'/fapi/v1/listenKey',headers={'X-MBX-APIKEY':API_KEY},timeout=10)
        r.raise_for_status(); return r.json().get('listenKey')
    except: return None

def keepalive_listenkey_worker(lk):
    while not stop_all:
        try: time.sleep(25*60); requests.put(FUTURES_REST_BASE+'/fapi/v1/listenKey',headers={'X-MBX-APIKEY':API_KEY},params={'listenKey':lk},timeout=10)
        except: pass

def on_user_message(ws,message):
    global in_position,position,current_balance,cooldown_until
    try:
        data=json.loads(message)
        payload=data.get('data') if isinstance(data,dict) and data.get('data') else data
        if payload.get('e')!='ORDER_TRADE_UPDATE' or not in_position or not position: return
        o=payload.get('o') or {}; status=o.get('X'); order_id=str(o.get('i')) if o.get('i') else None
        side=(o.get('S') or '').upper(); avg_price=float(o.get('ap') or 0)
        if order_id in (position.get('tp_id'),position.get('sl_id')) and status=='FILLED':
            outcome='TP' if order_id==position.get('tp_id') else 'SL'
            exit_price=avg_price or (position.get('tp_price') if outcome=='TP' else position.get('sl_price'))
            entry_price=float(position.get('entry')); dir_side=position.get('dir')
            pnl=(exit_price-entry_price)*LOT_SIZE if dir_side=='BUY' else (entry_price-exit_price)*LOT_SIZE
            with balance_lock: current_balance=fetch_usdt_balance()
            rec={'time':str(datetime.utcnow()),'dir':dir_side,'entry':entry_price,'exit':exit_price,'outcome':outcome,'pnl':round(pnl,6),'balance':round(current_balance,6)}
            append_trade_csv(rec)
            print(f"[INFO] {outcome} closed via user WS. PnL: {round(pnl,6)} | Balance: {rec['balance']}",flush=True)
            in_position=False; position=None
            if outcome=='SL': cooldown_until=datetime.utcnow()+timedelta(minutes=COOLDOWN_MINUTES)
    except Exception as e: logging.debug(f"User WS message error: {e}")

def on_user_open(ws): print("[INFO] User WS connected",flush=True)
def on_user_close(ws,code,reason): print(f"[WARNING] User WS closed: {code} {reason}",flush=True)
def on_user_error(ws,err): print(f"[ERROR] User WS error: {err}",flush=True)

def start_user_ws():
    global listen_key, ws_user, stop_all
    while not stop_all:
        try:
            lk=None
            try:
                res=exchange.fapiPrivatePostListenKey(); lk=res.get('listenKey') if isinstance(res,dict) else res
            except: lk=None
            if not lk: lk=create_listenkey_via_requests()
            if not lk: time.sleep(5); continue
            with listen_key_lock: listen_key=lk
            threading.Thread(target=keepalive_listenkey_worker,args=(lk,),daemon=True).start()
            ws_user=websocket.WebSocketApp(f"wss://fstream.binance.com/ws/{lk}",on_message=on_user_message,on_open=on_user_open,on_close=on_user_close,on_error=on_user_error)
            ws_user.run_forever(ping_interval=60,ping_timeout=10)
        except Exception as e: logging.error(f"User WS run_forever exception: {e}")
        time.sleep(2)

# ========== FALLBACK POLLING ==========
def fallback_position_poller():
    global in_position, position
    while not stop_all:
        try:
            time.sleep(15)
            if not in_position: continue
            try: pos_list=exchange.fapiPrivate_get_positionrisk()
            except: pos_list=None
            if not pos_list: continue
            found=None
            for p in pos_list:
                sym=p.get('symbol') or p.get('symbol',None)
                if sym and sym.replace('USDT','/USDT')==SYMBOL.replace('/','') or p.get('symbol')==SYMBOL:
                    found=p; break
            if not found: continue
            pos_amt=float(found.get('positionAmt',0))
            if abs(pos_amt)<1e-8: in_position=False; position=None; print("[INFO] External/manual close detected by poll.",flush=True)
        except: pass

# ========== STARTUP ==========
print(f"[INFO] 🚀 EMA LIVE BOT START — {SYMBOL} | Leverage {LEVERAGE} | TP {TP_POINTS} SL {SL_POINTS}",flush=True)
with balance_lock: current_balance=fetch_usdt_balance()
print(f"[INFO] Starting USDT balance: {current_balance}",flush=True)
log.info("Bot startup")

t_user=threading.Thread(target=start_user_ws,daemon=True); t_user.start()
t_k=threading.Thread(target=start_kline_ws,daemon=True); t_k.start()
t_poll=threading.Thread(target=fallback_position_poller,daemon=True); t_poll.start()

try: set_leverage(SYMBOL,LEVERAGE)
except: pass

try:
    while True: time.sleep(1); 
    if stop_all: break
except KeyboardInterrupt:
    print("\n[INFO] KeyboardInterrupt: shutting down",flush=True)
    stop_all=True
    try: ws_kline.close(); ws_user.close()
    except: pass
    sys.exit(0)
