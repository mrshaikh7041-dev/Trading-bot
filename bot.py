# live_binance_perp_ws_full_ready.py
"""
Live Binance USDT-Perp EMA bot — Full WebSocket + ListenKey + Fallback Polling
Symbol: BNB/USDT
Leverage: 75x
TP_POINTS = 6.0, SL_POINTS = 3.0
"""
import ccxt
import pandas as pd
import time
import threading
import websocket
import json
import os
import csv
import logging
import sys
import requests
from datetime import datetime, timedelta, timezone
from collections import deque

# =================== CONFIG ===================
SYMBOL = 'BNB/USDT'
WS_SYMBOL = 'bnbusdt'
TIMEFRAME = '1m'
LOT_SIZE = 0.01
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 75
COOLDOWN_MINUTES = 30

CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'

# Fill your keys locally (do NOT share)
API_KEY = 'czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ'
API_SECRET = 'cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO'

# Binance REST base for futures (mainnet)
FUTURES_REST_BASE = 'https://fapi.binance.com'

# =================== LOGGING ===================
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# =================== TIME ===================
KOLKATA = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)
def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== CCXT EXCHANGE ===================
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
    'timeout': 20000
})
exchange.options['adjustForTimeDifference'] = True
exchange.load_markets()

# =================== STATE ===================
in_position = False
position = None
cooldown_until = None
last_processed_candle_time = None

# Kline storage for EMA calculation
kline_deque = deque(maxlen=500)  # [open, high, low, close, volume, startTime]

# ListenKey / WS objects
listen_key = None
listen_key_lock = threading.Lock()
ws_user = None
ws_kline = None
stop_all = False

# thread-safe balance
balance_lock = threading.Lock()
current_balance = None

# track user-WS alive
user_ws_alive = False

# =================== UTILITIES ===================
def fetch_usdt_balance():
    try:
        bal = exchange.fetch_balance({'type': 'future'})
        if isinstance(bal, dict):
            if 'USDT' in bal and isinstance(bal['USDT'], dict):
                if bal['USDT'].get('total') is not None:
                    return float(bal['USDT'].get('total'))
                if bal['USDT'].get('free') is not None:
                    return float(bal['USDT'].get('free'))
            total = bal.get('total')
            if isinstance(total, dict) and 'USDT' in total:
                return float(total['USDT'])
        return None
    except Exception as e:
        logging.warning(f"fetch_usdt_balance failed: {e}")
        return None

def append_trade_csv(record):
    header = ['time', 'dir', 'entry', 'exit', 'outcome', 'pnl', 'balance']
    exists = os.path.isfile(CSV_FN)
    with open(CSV_FN,'a',newline='',encoding='utf-8') as f:
        writer = csv.DictWriter(f,fieldnames=header)
        if not exists:
            writer.writeheader()
        writer.writerow(record)

def get_price_precision(symbol):
    try:
        m = exchange.markets.get(symbol)
        if m:
            return m.get('precision', {}).get('price', 2)
    except Exception:
        pass
    return 2

def price_round(symbol, price):
    prec = get_price_precision(symbol)
    fmt = '{:.' + str(prec) + 'f}'
    try:
        return float(fmt.format(price))
    except:
        return round(price, prec)

# =================== ORDER HELPERS ===================
def set_leverage(symbol, leverage):
    try:
        sym = symbol.replace('/','')
        # Some ccxt versions expect this exact method name:
        if hasattr(exchange, 'fapiPrivatePostLeverage'):
            exchange.fapiPrivatePostLeverage({'symbol': sym, 'leverage': int(leverage)})
        elif hasattr(exchange, 'fapiPrivatePostLeverage'):
            exchange.fapiPrivatePostLeverage({'symbol': sym, 'leverage': int(leverage)})
        else:
            # try generic
            exchange.request('fapi/v1/leverage','POST',{'symbol':sym,'leverage':int(leverage)})
        logging.info(f"Leverage {leverage} set for {symbol}")
    except Exception as e:
        logging.warning(f"Set leverage failed: {e}")

def _round_amount(symbol, amount):
    try:
        market = exchange.markets.get(symbol)
        precision = market.get('precision', {}).get('amount')
        if precision is not None:
            return float(round(amount, precision))
    except Exception:
        pass
    return amount

def create_market_entry(symbol, side, amount):
    try:
        amt = _round_amount(symbol, amount)
        logging.info(f"Placing market entry: {side} {amt} {symbol}")
        order = exchange.create_order(symbol, 'market', side.lower(), amt, None, {'reduceOnly': False})
        logging.info(f"Entry order placed id={order.get('id')}")
        return order
    except Exception as e:
        logging.error(f"Market entry failed: {e}")
        raise

def place_tp_sl(symbol, side, amount, tp_price, sl_price):
    close_side = 'sell' if side == 'BUY' else 'buy'
    amt = _round_amount(symbol, amount)
    tp_order = None
    sl_order = None
    try:
        tp_order = exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', close_side, amt, None,
                                         {'stopPrice': float(tp_price), 'reduceOnly': True})
        logging.info(f"TP order placed id={tp_order.get('id')}")
    except Exception as e:
        logging.warning(f"TP placement failed: {e}")
    try:
        sl_order = exchange.create_order(symbol, 'STOP_MARKET', close_side, amt, None,
                                         {'stopPrice': float(sl_price), 'reduceOnly': True})
        logging.info(f"SL order placed id={sl_order.get('id')}")
    except Exception as e:
        logging.warning(f"SL placement failed: {e}")
    return tp_order, sl_order

# =================== EMA STRATEGY ===================
def compute_emas_from_deque():
    if len(kline_deque) < 12:
        return None
    df = pd.DataFrame(list(kline_deque), columns=['open','high','low','close','volume','time_ms'])
    df['close'] = df['close'].astype(float)
    df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema15'] = df['close'].ewm(span=15, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    return df

def check_signal_from_df(df):
    last = df.iloc[-1]
    try:
        c = float(last['close']); l = float(last['low']); h = float(last['high'])
        ema5 = float(last['ema5']); ema9 = float(last['ema9']); ema15 = float(last['ema15']); ema21 = float(last['ema21'])
    except Exception:
        return None
    if c >= ema5 and c >= ema9 and c >= ema15 and c > ema21:
        return 'BUY'
    if c <= ema5 and c <= ema9 and c <= ema15 and c < ema21:
        return 'SELL'
    if l <= ema15 <= h and c > ema5:
        return 'BUY'
    if l <= ema15 <= h and c < ema5:
        return 'SELL'
    return None

# =================== KLINE WS (public) ===================
def on_kline_message(ws, message):
    global last_processed_candle_time, in_position, position, cooldown_until
    try:
        data = json.loads(message)
    except Exception:
        return
    # wrapper stream vs direct
    payload = data.get('data') if isinstance(data, dict) and data.get('data') else data
    k = payload.get('k') or {}
    if not k:
        return
    is_closed = k.get('x')
    if is_closed:
        try:
            o = float(k.get('o')); h = float(k.get('h')); l = float(k.get('l')); c = float(k.get('c')); v = float(k.get('v')); t = int(k.get('t'))
            kline_deque.append([o,h,l,c,v,t])
            df = compute_emas_from_deque()
            if df is None:
                return
            signal = check_signal_from_df(df)
            # convert time
            last_iso = pd.to_datetime(t, unit='ms', utc=True).tz_convert('Asia/Kolkata').isoformat()
            # cooldown check
            if cooldown_until is not None and now_ist() < cooldown_until:
                logging.info(f"In cooldown until {cooldown_until} -> skipping")
                return
            if signal and not in_position:
                if last_processed_candle_time == last_iso:
                    return
                # entry using current close (we place market order)
                entry_price = c
                # compute TP/SL with rounding
                if signal == 'BUY':
                    tp = price_round(SYMBOL, entry_price + TP_POINTS)
                    sl = price_round(SYMBOL, entry_price - SL_POINTS)
                else:
                    tp = price_round(SYMBOL, entry_price - TP_POINTS)
                    sl = price_round(SYMBOL, entry_price + SL_POINTS)
                # place trade
                try:
                    logging.info(f"Signal {signal} detected. Creating entry market and TP/SL (TP={tp}, SL={sl})")
                    # ensure leverage
                    try:
                        set_leverage(SYMBOL, LEVERAGE)
                    except Exception:
                        pass
                    side_ccxt = 'buy' if signal == 'BUY' else 'sell'
                    entry_ord = create_market_entry(SYMBOL, side_ccxt, LOT_SIZE)
                    # derive entry price
                    entry_price_actual = None
                    try:
                        entry_price_actual = entry_ord.get('average') or entry_ord.get('price') or (entry_ord.get('info') or {}).get('avgPrice')
                    except:
                        entry_price_actual = None
                    if not entry_price_actual:
                        entry_price_actual = entry_price
                    # place TP & SL
                    tp_ord, sl_ord = place_tp_sl(SYMBOL, signal, LOT_SIZE, tp, sl)
                    position = {
                        'dir': signal,
                        'entry': float(entry_price_actual),
                        'tp_price': float(tp),
                        'sl_price': float(sl),
                        'entry_time': now_ist(),
                        'entry_id': str(entry_ord.get('id')) if entry_ord and entry_ord.get('id') else None,
                        'tp_id': str(tp_ord.get('id')) if tp_ord and tp_ord.get('id') else None,
                        'sl_id': str(sl_ord.get('id')) if sl_ord and sl_ord.get('id') else None
                    }
                    in_position = True
                    last_processed_candle_time = last_iso
                    logging.info(f"Opened live {signal}: {position}")
                    print(f"[{now_str()}] OPENED {signal} entry={position['entry']} TP={position['tp_price']} SL={position['sl_price']}", flush=True)
                except Exception as e:
                    logging.error(f"Error placing trade: {e}")
                    in_position = False
                    position = None
        except Exception as e:
            logging.debug(f"kline parse error: {e}")

def on_kline_open(ws):
    logging.info("kline ws connected")
    print(f"[{now_str()}] kline ws connected", flush=True)

def on_kline_close(ws, code, reason):
    logging.warning("kline ws closed")

def on_kline_error(ws, err):
    logging.error(f"kline ws error: {err}")

def start_kline_ws():
    global ws_kline, stop_all
    stream = f"{WS_SYMBOL}@kline_1m"
    ws_url = f"wss://fstream.binance.com/ws/{stream}"
    while not stop_all:
        try:
            ws_kline = websocket.WebSocketApp(ws_url, on_message=on_kline_message,
                                             on_open=on_kline_open, on_close=on_kline_close, on_error=on_kline_error)
            ws_kline.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            logging.error(f"kline ws run_forever error: {e}")
        time.sleep(2)

# =================== USER DATA (listenKey) via REST + WS ===================
def create_listenkey_via_requests():
    url = FUTURES_REST_BASE + '/fapi/v1/listenKey'
    headers = {'X-MBX-APIKEY': API_KEY}
    try:
        r = requests.post(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get('listenKey')
    except Exception as e:
        logging.error(f"listenKey creation via requests failed: {e}")
        return None

def keepalive_listenkey_worker(lk):
    url = FUTURES_REST_BASE + '/fapi/v1/listenKey'
    headers = {'X-MBX-APIKEY': API_KEY}
    while not stop_all:
        try:
            time.sleep(60 * 25)  # every 25 minutes
            requests.put(url, headers=headers, params={'listenKey': lk}, timeout=10)
            logging.debug("Sent listenKey keepalive")
        except Exception as e:
            logging.debug(f"listenKey keepalive error: {e}")

def on_user_message(ws, message):
    global in_position, position, current_balance, cooldown_until, user_ws_alive
    user_ws_alive = True
    try:
        data = json.loads(message)
    except Exception:
        return
    # unwrap
    payload = data.get('data') if isinstance(data, dict) and data.get('data') else data
    evt_type = payload.get('e')
    if evt_type not in ('ORDER_TRADE_UPDATE','ACCOUNT_UPDATE'):
        return
    # ORDER_TRADE_UPDATE handling
    if evt_type == 'ORDER_TRADE_UPDATE':
        o = payload.get('o') or {}
        status = o.get('X')
        order_id = str(o.get('i')) if o.get('i') is not None else None
        side = (o.get('S') or '').upper()
        avg_price_str = o.get('ap')
        try:
            avg_price = float(avg_price_str) if avg_price_str and avg_price_str != '0' else None
        except:
            avg_price = None
        if not in_position or not position:
            # maybe manual close or other user order - we still want to detect if our position is closed
            return
        tp_id = str(position.get('tp_id')) if position.get('tp_id') else None
        sl_id = str(position.get('sl_id')) if position.get('sl_id') else None
        # if our tp/sl order filled:
        if order_id in (tp_id, sl_id) and status == 'FILLED':
            outcome = 'TP' if order_id == tp_id else 'SL'
            exit_price = avg_price if avg_price else (position.get('tp_price') if outcome=='TP' else position.get('sl_price'))
            entry_price = float(position.get('entry'))
            dir_side = position.get('dir')
            pnl = (exit_price - entry_price) * LOT_SIZE if dir_side == 'BUY' else (entry_price - exit_price) * LOT_SIZE
            with balance_lock:
                current_balance = fetch_usdt_balance()
                bal = current_balance
            rec = {
                'time': position.get('entry_time').isoformat() if position.get('entry_time') else str(now_ist().isoformat()),
                'dir': dir_side,
                'entry': entry_price,
                'exit': exit_price,
                'outcome': outcome,
                'pnl': round(pnl,6),
                'balance': round(bal,6) if bal is not None else None
            }
            append_trade_csv(rec)
            print(f"[{now_str()}] {outcome} closed via user WS. PnL: {round(pnl,6)} | Balance: {rec['balance']}", flush=True)
            log.info(f"{outcome} closed via user WS. {rec}")
            # cleanup
            in_position = False
            position = None
            if outcome == 'SL':
                cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
                print(f"[{now_str()}] SL hit → cooldown until {cooldown_until}", flush=True)
            else:
                print(f"[{now_str()}] TP occurred → re-entry blocked until next candle.", flush=True)
    # ACCOUNT_UPDATE could be used to detect balance/position changes if needed

def on_user_open(ws):
    global user_ws_alive
    user_ws_alive = True
    logging.info("user ws connected")
    print(f"[{now_str()}] user ws connected", flush=True)

def on_user_close(ws, code, reason):
    global user_ws_alive
    user_ws_alive = False
    logging.warning("user ws closed")

def on_user_error(ws, err):
    logging.error(f"user ws error: {err}")

def start_user_ws():
    global listen_key, ws_user, stop_all, user_ws_alive
    while not stop_all:
        try:
            # Try ccxt helper first if available
            lk = None
            try:
                if hasattr(exchange, 'fapiPrivatePostListenKey'):
                    res = exchange.fapiPrivatePostListenKey()
                    if isinstance(res, dict):
                        lk = res.get('listenKey')
                    elif isinstance(res, str):
                        lk = res
            except Exception:
                lk = None
            # fallback to requests
            if not lk:
                lk = create_listenkey_via_requests()
            if not lk:
                logging.error("Cannot start user WS without listenKey. Retry in 5s.")
                time.sleep(5)
                continue
            with listen_key_lock:
                listen_key = lk
            # spawn keepalive
            ka = threading.Thread(target=keepalive_listenkey_worker, args=(lk,), daemon=True)
            ka.start()
            ws_url = f"wss://fstream.binance.com/ws/{lk}"
            ws_user = websocket.WebSocketApp(ws_url, on_message=on_user_message, on_open=on_user_open,
                                             on_close=on_user_close, on_error=on_user_error)
            logging.info(f"Connecting user ws to {ws_url}")
            ws_user.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            logging.error(f"user ws run_forever exception: {e}")
        time.sleep(2)

# =================== FALLBACK POLLING (safety) ===================
def fallback_position_poller():
    """
    If user-WS is down or misses events, poll positions every 15s and detect external closes.
    """
    global in_position, position
    while not stop_all:
        try:
            time.sleep(15)
            if not in_position:
                continue
            # fetch positions
            try:
                pos_list = exchange.fapiPrivate_get_positionrisk()  # ccxt raw request
            except Exception:
                # fallback to ccxt fetch_positions if available
                try:
                    pos_list = exchange.fetch_positions([SYMBOL])
                except Exception:
                    pos_list = None
            if not pos_list:
                continue
            # pos_list may be a list of dicts (positionRisk)
            # find our symbol
            found = None
            for p in pos_list:
                sym = p.get('symbol') or p.get('symbol', None)
                if sym and sym.replace('USDT','/USDT') == SYMBOL.replace('/',''):
                    found = p
                    break
                # ccxt fetch_positions uses 'symbol' like 'BNB/USDT'
                if p.get('symbol') == SYMBOL:
                    found = p
                    break
            if not found:
                # couldn't find symbol -> skip
                continue
            # determine if position size is zero
            # in positionRisk API, 'positionAmt' indicates qty (string)
            pos_amt = None
            if 'positionAmt' in found:
                try:
                    pos_amt = float(found.get('positionAmt', 0))
                except:
                    pos_amt = None
            elif isinstance(found.get('contracts', None), (int, float, str)):
                try:
                    pos_amt = float(found.get('contracts', 0))
                except:
                    pos_amt = None
            # if zero or near zero -> external close
            if pos_amt is not None and abs(pos_amt) < 1e-8:
                # external/manual close detected
                print(f"[{now_str()}] External/manual close detected by poll. Resetting state.", flush=True)
                log.info("External/manual close detected by poll.")
                # No PnL calculation here; rely on prior order fills or manual judgement
                in_position = False
                position = None
        except Exception as e:
            logging.debug(f"fallback poll error: {e}")

# =================== STARTUP ===================
print(f"[{now_str()}] 🚀 EMA LIVE BOT START — {SYMBOL} | Leverage {LEVERAGE} | TP {TP_POINTS} SL {SL_POINTS}", flush=True)
with balance_lock:
    current_balance = fetch_usdt_balance()
print(f"[{now_str()}] Starting USDT balance: {current_balance}", flush=True)
log.info("Bot startup")

# Start user ws thread
t_user = threading.Thread(target=start_user_ws, daemon=True)
t_user.start()
# Start kline ws thread
t_k = threading.Thread(target=start_kline_ws, daemon=True)
t_k.start()
# Start fallback poller
t_poll = threading.Thread(target=fallback_position_poller, daemon=True)
t_poll.start()

# set leverage once
try:
    set_leverage(SYMBOL, LEVERAGE)
except Exception as e:
    logging.warning(f"Leverage set failed at startup: {e}")

# main thread: keep alive & minimal status prints
try:
    while True:
        time.sleep(1)
        if stop_all:
            break
except KeyboardInterrupt:
    print("\n[INFO] KeyboardInterrupt: shutting down", flush=True)
    stop_all = True
    try:
        if ws_kline: ws_kline.close()
        if ws_user: ws_user.close()
    except:
        pass
    sys.exit(0)
