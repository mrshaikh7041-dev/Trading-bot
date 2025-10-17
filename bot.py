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
from datetime import datetime, timedelta, timezone
from collections import deque

# =================== CONFIG ===================
SYMBOL = 'BNB/USDT'            # trading pair
WS_SYMBOL = 'bnbusdt'         # for websocket stream (lowercase, no slash)
TIMEFRAME = '1m'
LOT_SIZE = 0.10
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 100
COOLDOWN_MINUTES = 30
CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'
DRY_RUN = False  # set True to test without placing live orders

# =================== API KEYS (safe fallback) ===================
# Preferred: set environment variables BINANCE_API_KEY and BINANCE_API_SECRET
API_KEY = os.getenv('BINANCE_API_KEY') or 'czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ'
API_SECRET = os.getenv('BINANCE_API_SECRET') or 'cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO'

# =================== LOGGING ===================
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)
# also output INFO to console for immediate feedback
console = logging.StreamHandler()
console.setLevel(logging.INFO)
log.addHandler(console)

# =================== TIMEZONE ===================
KOLKATA = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)
def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== EXCHANGE (ccxt futures client) ===================
try:
    exchange = ccxt.binanceusdm({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
        'timeout': 20000,
    })
    exchange.options['adjustForTimeDifference'] = True
    exchange.load_markets()
    log.info("Initialized ccxt.binanceusdm client")
except Exception as e:
    log.exception("Could not initialize exchange client: %s", e)
    print("[FATAL] Could not initialize exchange client. Check API keys and ccxt installation.", flush=True)
    sys.exit(1)

# =================== STATE & LOCKS ===================
in_position = False
position = None
cooldown_until = None
last_processed_candle_time = None  # will store integer ms (candle start time)
balance_lock = threading.Lock()
state_lock = threading.Lock()
current_balance = None

# For kline history (we'll keep last 300 candles)
kline_deque = deque(maxlen=300)  # store [open,high,low,close,volume,time_ms]

# For user-data listenKey management
listen_key = None
listen_key_lock = threading.Lock()
ws_user = None
ws_kline = None
stop_all = False

# =================== UTILITIES ===================
def fetch_usdt_balance():
    try:
        bal = exchange.fetch_balance({'type': 'future'})
        if isinstance(bal, dict):
            # try common shapes
            if 'USDT' in bal and isinstance(bal['USDT'], dict):
                if bal['USDT'].get('total') is not None:
                    return float(bal['USDT'].get('total'))
                if bal['USDT'].get('free') is not None:
                    return float(bal['USDT'].get('free'))
            if isinstance(bal.get('total'), dict) and 'USDT' in bal.get('total'):
                return float(bal.get('total')['USDT'])
            if 'info' in bal and isinstance(bal['info'], dict):
                # Binance futures sometimes returns totalWalletBalance
                info = bal['info']
                if 'totalWalletBalance' in info:
                    try:
                        return float(info.get('totalWalletBalance'))
                    except:
                        pass
        return None
    except Exception as e:
        log.warning(f"fetch_usdt_balance failed: {e}")
        return None

def append_trade_csv(record):
    header = ['time', 'dir', 'entry', 'exit', 'outcome', 'pnl', 'balance']
    exists = os.path.isfile(CSV_FN)
    try:
        with open(CSV_FN, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=header)
            if not exists:
                writer.writeheader()
            writer.writerow(record)
    except Exception as e:
        log.exception("append_trade_csv failed: %s", e)

def _round_amount(symbol, amount):
    try:
        market = exchange.markets.get(symbol)
        precision = market.get('precision', {}).get('amount') if market else None
        if precision is not None:
            fmt = "{:0." + str(precision) + "f}"
            return float(fmt.format(amount))
    except Exception:
        pass
    # fallback
    return float(round(amount, 3))

# =================== ORDER HELPERS ===================
def set_leverage(symbol, leverage):
    try:
        sym = symbol.replace('/', '')
        # try several ccxt method names / endpoints
        if hasattr(exchange, 'fapiPrivate_post_leverage'):
            exchange.fapiPrivate_post_leverage({'symbol': sym, 'leverage': int(leverage)})
        elif hasattr(exchange, 'private_post_leverage'):
            exchange.private_post_leverage({'symbol': sym, 'leverage': int(leverage)})
        else:
            # try unified helper
            try:
                exchange.set_leverage(leverage, symbol)
            except Exception:
                pass
        logging.info(f"Leverage {leverage} set for {symbol}")
    except Exception as e:
        logging.warning(f"Set leverage failed: {e}")

def create_market_entry(symbol, side, amount):
    amount_rounded = _round_amount(symbol, amount)
    if DRY_RUN:
        log.info(f"[DRY RUN] create_market_entry {symbol} {side} {amount_rounded}")
        return {'id': 'dryrun-entry', 'average': None, 'price': None, 'info': {}}
    return exchange.create_order(symbol, 'market', side.lower(), amount_rounded, None, {'reduceOnly': False})

def place_tp_sl(symbol, side, amount, tp_price, sl_price):
    close_side = 'sell' if side == 'BUY' else 'buy'
    amount_rounded = _round_amount(symbol, amount)
    tp_order = None; sl_order = None
    if DRY_RUN:
        log.info(f"[DRY RUN] TP {tp_price} SL {sl_price} for {side} {amount_rounded}")
        return {'id':'dryrun-tp'}, {'id':'dryrun-sl'}
    try:
        try:
            tp_order = exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', close_side, amount_rounded, None,
                                             {'stopPrice': float(tp_price), 'reduceOnly': True})
        except Exception as e:
            log.debug("TP create_order failed, trying fallback: %s", e)
            # fallback to raw endpoint if available
            if hasattr(exchange, 'fapiPrivate_post_order'):
                exchange.fapiPrivate_post_order({'symbol': symbol.replace('/', ''), 'side': close_side.upper(),
                                                'type': 'TAKE_PROFIT_MARKET', 'quantity': amount_rounded, 'stopPrice': float(tp_price), 'reduceOnly': True})
                tp_order = {'id': None}
            else:
                raise
    except Exception as e:
        logging.warning(f"TP placement failed: {e}")
    try:
        try:
            sl_order = exchange.create_order(symbol, 'STOP_MARKET', close_side, amount_rounded, None,
                                             {'stopPrice': float(sl_price), 'reduceOnly': True})
        except Exception as e:
            log.debug("SL create_order failed, trying fallback: %s", e)
            if hasattr(exchange, 'fapiPrivate_post_order'):
                exchange.fapiPrivate_post_order({'symbol': symbol.replace('/', ''), 'side': close_side.upper(),
                                                'type': 'STOP_MARKET', 'quantity': amount_rounded, 'stopPrice': float(sl_price), 'reduceOnly': True})
                sl_order = {'id': None}
            else:
                raise
    except Exception as e:
        logging.warning(f"SL placement failed: {e}")
    return tp_order, sl_order

# =================== EMA STRATEGY (on closed candle) ===================
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
    last_closed = df.iloc[-1]
    try:
        c = float(last_closed['close'])
        l = float(last_closed['low'])
        h = float(last_closed['high'])
        ema5 = float(last_closed['ema5'])
        ema9 = float(last_closed['ema9'])
        ema15 = float(last_closed['ema15'])
        ema21 = float(last_closed['ema21'])
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

# =================== KLINE WEBSOCKET (public) ===================
def on_kline_message(ws, message):
    global last_processed_candle_time, in_position, position, cooldown_until
    try:
        data = json.loads(message)
    except Exception as e:
        print(f"[kline] invalid json: {e}", flush=True)
        return

    if 'data' in data and isinstance(data['data'], dict):
        payload = data['data']
    else:
        payload = data

    k = payload.get('k') or {}
    is_closed = k.get('x')
    if is_closed:
        try:
            o = float(k.get('o')); h = float(k.get('h')); l = float(k.get('l')); c = float(k.get('c')); v = float(k.get('v'))
            t = int(k.get('t'))  # start time in ms
            kline_deque.append([o,h,l,c,v,t])

            df = compute_emas_from_deque()
            if df is None:
                return
            signal = check_signal_from_df(df)

            # cooldown check
            if cooldown_until is not None and now_ist() < cooldown_until:
                print(f"[{now_str()}] In cooldown until {cooldown_until} -> skipping signal", flush=True)
                return

            if signal and not in_position:
                # avoid reprocessing same candle by comparing integer ms
                with state_lock:
                    if last_processed_candle_time == t:
                        return

                entry_price = float(c)
                if signal == 'BUY':
                    tp_price = entry_price + TP_POINTS
                    sl_price = entry_price - SL_POINTS
                else:
                    tp_price = entry_price - TP_POINTS
                    sl_price = entry_price + SL_POINTS

                try:
                    print(f"[{now_str()}] Signal {signal} detected from kline close. Placing market entry...", flush=True)
                    log.info(f"Signal {signal} -> entry at market, tp={tp_price}, sl={sl_price}")
                    try:
                        set_leverage(SYMBOL, LEVERAGE)
                    except Exception:
                        pass

                    side_ccxt = 'buy' if signal == 'BUY' else 'sell'
                    entry_order = create_market_entry(SYMBOL, side_ccxt, LOT_SIZE)
                    # derive entry price
                    entry_price_actual = None
                    try:
                        entry_price_actual = entry_order.get('average') or entry_order.get('price') or (entry_order.get('info') or {}).get('avgPrice')
                    except Exception:
                        entry_price_actual = None
                    if not entry_price_actual:
                        try:
                            entry_price_actual = float(k.get('c'))
                        except:
                            entry_price_actual = entry_price

                    tp_order, sl_order = place_tp_sl(SYMBOL, signal, LOT_SIZE, tp_price, sl_price)

                    with state_lock:
                        position = {
                            'dir': signal,
                            'entry': float(entry_price_actual),
                            'tp_price': float(tp_price),
                            'sl_price': float(sl_price),
                            'entry_time': now_ist(),
                            'entry_id': str(entry_order.get('id')) if entry_order and entry_order.get('id') is not None else None,
                            'tp_id': str(tp_order.get('id')) if tp_order and tp_order.get('id') is not None else None,
                            'sl_id': str(sl_order.get('id')) if sl_order and sl_order.get('id') is not None else None
                        }
                        in_position = True
                        last_processed_candle_time = t

                    print(f"[{now_str()}] OPENED {signal} entry={position['entry']} tp_id={position['tp_id']} sl_id={position['sl_id']}", flush=True)
                    log.info(f"Opened live {signal}: {position}")
                except Exception as e:
                    print(f"[{now_str()}] Error placing live trade: {e}", flush=True)
                    log.error(f"Error placing live trade: {e}")
                    with state_lock:
                        in_position = False
                        position = None
        except Exception as e:
            logging.debug(f"kline parse error: {e}", exc_info=True)

def on_kline_error(ws, err):
    print(f"[kline WS ERROR] {err}", flush=True)
    log.error(f"kline WS ERROR: {err}")

def on_kline_close(ws, code, reason):
    print("[kline WS] closed", flush=True)
    log.info("kline ws closed")

def on_kline_open(ws):
    print("[kline WS] connected", flush=True)
    log.info("kline ws connected")

def start_kline_ws():
    global ws_kline, stop_all
    stream = f"{WS_SYMBOL}@kline_1m"
    ws_url = f"wss://fstream.binance.com/ws/{stream}"
    while not stop_all:
        try:
            ws_kline = websocket.WebSocketApp(ws_url,
                                             on_message=on_kline_message,
                                             on_error=on_kline_error,
                                             on_close=on_kline_close,
                                             on_open=on_kline_open)
            ws_kline.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            print(f"[kline WS run_forever error] {e}", flush=True)
            log.error(f"kline ws run_forever error: {e}", exc_info=True)
        time.sleep(2)

# =================== USER DATA WEBSOCKET (listenKey) ===================
def create_listen_key():
    """
    Try several ccxt method names to create a user-data listenKey for futures.
    Returns listenKey string or None.
    """
    try:
        # most specific: fapiPrivate_post_listenKey
        if hasattr(exchange, 'fapiPrivate_post_listenKey'):
            res = exchange.fapiPrivate_post_listenKey()
            if isinstance(res, dict):
                # try common keys
                return res.get('listenKey') or res.get('listen_key') or res.get('key') or res.get('listenkey')
            return res
        # older naming or unified private_post_listenkey
        if hasattr(exchange, 'private_post_listenkey'):
            res = exchange.private_post_listenkey()
            if isinstance(res, dict):
                return res.get('listenKey') or res.get('listen_key') or res.get('key') or res.get('listenkey')
            return res
        # sapi_post_listenKey fallback
        if hasattr(exchange, 'sapi_post_listenKey'):
            res = exchange.sapi_post_listenKey()
            if isinstance(res, dict):
                return res.get('listenKey') or res.get('listen_key') or res.get('key')
            return res
        # try raw request to fapi endpoint
        try:
            res = exchange.request('listenKey', 'fapi', 'POST', {})
            if isinstance(res, dict):
                return res.get('listenKey')
        except Exception:
            pass
    except Exception as e:
        log.debug("create_listen_key error: %s", e, exc_info=True)
    return None

def on_user_message(ws, message):
    global in_position, position, current_balance, cooldown_until
    try:
        data = json.loads(message)
    except Exception as e:
        print(f"[user WS] invalid json: {e}", flush=True)
        return

    if 'data' in data and isinstance(data['data'], dict):
        payload = data['data']
    else:
        payload = data

    evt_type = payload.get('e')
    if evt_type != 'ORDER_TRADE_UPDATE':
        return

    o = payload.get('o') or {}
    status = o.get('X')  # e.g., NEW, PARTIALLY_FILLED, FILLED, CANCELED
    order_id_raw = o.get('i')
    order_id = str(order_id_raw) if order_id_raw is not None else None
    side = (o.get('S') or '').upper()
    avg_price_str = o.get('ap')  # average price string maybe '0' if none
    try:
        avg_price = float(avg_price_str) if avg_price_str not in (None, '', '0') else None
    except:
        avg_price = None

    # read state under lock
    with state_lock:
        local_in_position = in_position
        local_position = dict(position) if position else None

    if not local_in_position or not local_position:
        return

    tp_id = str(local_position.get('tp_id')) if local_position.get('tp_id') is not None else None
    sl_id = str(local_position.get('sl_id')) if local_position.get('sl_id') is not None else None

    if order_id in (tp_id, sl_id) and status == 'FILLED':
        outcome = 'TP' if order_id == tp_id else 'SL'
        exit_price = avg_price if avg_price else (local_position.get('tp_price') if outcome=='TP' else local_position.get('sl_price'))
        entry_price = float(local_position.get('entry'))
        dir_side = local_position.get('dir')
        if dir_side == 'BUY':
            pnl = (exit_price - entry_price) * LOT_SIZE
        else:
            pnl = (entry_price - exit_price) * LOT_SIZE

        with balance_lock:
            current_balance = fetch_usdt_balance()
            bal = current_balance

        rec = {
            'time': local_position.get('entry_time').isoformat() if local_position.get('entry_time') else str(now_ist().isoformat()),
            'dir': dir_side,
            'entry': entry_price,
            'exit': exit_price,
            'outcome': outcome,
            'pnl': round(pnl,6),
            'balance': round(bal,6) if bal is not None else None
        }
        append_trade_csv(rec)
        print(f"[{now_str()}] {outcome} closed via user WS. PnL: {round(pnl,6)} | Account USDT: {rec['balance']}", flush=True)
        log.info(f"{outcome} closed via user WS. {rec}")

        # cleanup under lock
        with state_lock:
            in_position = False
            position = None
        if outcome == 'SL':
            cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
            print(f"[{now_str()}] SL hit → cooldown until {cooldown_until}", flush=True)
            log.info(f"SL cooldown until {cooldown_until}")
        else:
            print(f"[{now_str()}] TP occurred → re-entry blocked until next candle.", flush=True)
            log.info("Re-entry blocked on same candle after TP (exchange).")

def on_user_error(ws, err):
    print(f"[user WS ERROR] {err}", flush=True)
    log.error(f"user WS ERROR: {err}")

def on_user_close(ws, code, reason):
    print("[user WS] closed", flush=True)
    log.info("user ws closed")

def on_user_open(ws):
    print("[user WS] connected", flush=True)
    log.info("user ws connected")

def start_user_ws():
    global listen_key, ws_user, stop_all
    while not stop_all:
        try:
            lk = create_listen_key()
            if not lk:
                print("[user WS] listenKey creation failed", flush=True)
                log.error("listenKey creation returned no key")
                time.sleep(5)
                continue
            with listen_key_lock:
                listen_key = lk
            ws_url = f"wss://fstream.binance.com/ws/{listen_key}"
            # start keepalive thread for this listen_key
            ka_thread = threading.Thread(target=listenkey_keepalive_worker, args=(listen_key,), daemon=True)
            ka_thread.start()

            ws_user = websocket.WebSocketApp(ws_url,
                                             on_message=on_user_message,
                                             on_error=on_user_error,
                                             on_close=on_user_close,
                                             on_open=on_user_open)
            ws_user.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            print(f"[user WS run_forever error] {e}", flush=True)
            log.error(f"user ws run_forever error: {e}", exc_info=True)
        time.sleep(2)

def listenkey_keepalive_worker(lk):
    while not stop_all:
        try:
            time.sleep(60 * 25)  # wake earlier than 30m to be safe
            try:
                if hasattr(exchange, 'fapiPrivate_put_listenKey'):
                    exchange.fapiPrivate_put_listenKey({'listenKey': lk})
                elif hasattr(exchange, 'private_put_listenkey'):
                    exchange.private_put_listenkey({'listenKey': lk})
                elif hasattr(exchange, 'sapi_put_listenKey'):
                    exchange.sapi_put_listenKey({'listenKey': lk})
                else:
                    # try raw request
                    try:
                        exchange.request('listenKey', 'fapi', 'PUT', {'listenKey': lk})
                    except Exception:
                        pass
                logging.debug("Sent listenKey keepalive")
            except Exception as e:
                logging.debug(f"listenKey keepalive error: {e}", exc_info=True)
        except Exception as e:
            logging.debug(f"listenkey worker outer exception: {e}", exc_info=True)

# =================== STARTUP ===================
print(f"[{now_str()}] 🚀 [EMA LIVE BOT] {SYMBOL} | Binance Perpetual | FULL WebSocket Mode", flush=True)
log.info("Starting full websocket EMA bot")

with balance_lock:
    current_balance = fetch_usdt_balance()
if current_balance is not None:
    print(f"[{now_str()}] Starting account USDT balance: {current_balance}", flush=True)
    log.info(f"Starting account USDT balance: {current_balance}")
else:
    print(f"[{now_str()}] Warning: could not fetch starting USDT balance.", flush=True)
    log.warning("Could not fetch starting USDT balance.")

# start user-data ws thread (daemon)
t_user = threading.Thread(target=start_user_ws, daemon=True)
t_user.start()
# start kline ws thread (daemon)
t_kline = threading.Thread(target=start_kline_ws, daemon=True)
t_kline.start()

# set leverage once at startup (best-effort)
try:
    set_leverage(SYMBOL, LEVERAGE)
except Exception as e:
    logging.warning(f"Leverage set failed at startup: {e}")

# =================== MAIN LOOP (minimal) ===================
try:
    while True:
        time.sleep(1)
        if stop_all:
            break
except KeyboardInterrupt:
    print("\n[INFO] KeyboardInterrupt received. Shutting down...", flush=True)
    stop_all = True
    try:
        if ws_kline:
            ws_kline.close()
        if ws_user:
            ws_user.close()
    except Exception:
        pass
    sys.exit(0)
