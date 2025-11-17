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
WS_SYMBOL = 'bnbusdt'          # for websocket stream (lowercase, no slash)
TIMEFRAME = '1m'
LOT_SIZE = 0.02                # your lot size (YEHI DIRECT JAYEGA)
SL_POINTS = 3.0
TP_POINTS = 5.9
LEVERAGE = 75
COOLDOWN_MINUTES = 30
CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'

# 🔹 RSI STRATEGY CONFIG
RSI_PERIOD = 14
RSI_LOW = 20
RSI_HIGH = 60

# 🔹 LIMIT ENTRY CONFIG
LIMIT_BUFFER = 0.10          # price offset for limit orders
ENTRY_WAIT_SECONDS = 5       # wait after each limit attempt
MAX_LIMIT_ATTEMPTS = 2       # after 2 attempts -> market fallback

# API KEYS - FILL LOCALLY
API_KEY = 'czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ'
API_SECRET = 'cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO'

# =================== LOGGING ===================
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# =================== TIMEZONE ===================
KOLKATA = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)
def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== EXCHANGE (ccxt) ===================
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
    'timeout': 20000,
})
exchange.options['adjustForTimeDifference'] = True
exchange.load_markets()

# =================== STATE & LOCKS ===================
in_position = False
position = None
cooldown_until = None
last_processed_candle_time = None
balance_lock = threading.Lock()
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
            if 'USDT' in bal and isinstance(bal['USDT'], dict):
                if bal['USDT'].get('total') is not None:
                    return float(bal['USDT'].get('total'))
                if bal['USDT'].get('free') is not None:
                    return float(bal['USDT'].get('free'))
            if isinstance(bal.get('total'), dict) and 'USDT' in bal.get('total'):
                return float(bal.get('total')['USDT'])
        return None
    except Exception as e:
        logging.warning(f"fetch_usdt_balance failed: {e}")
        return None

def append_trade_csv(record):
    header = ['time', 'dir', 'entry', 'exit', 'outcome', 'pnl', 'balance']
    exists = os.path.isfile(CSV_FN)
    with open(CSV_FN, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not exists:
            writer.writeheader()
        writer.writerow(record)

# qty rounding ab USE nahi ho rahi, sirf price rounding rakha hai
def _round_price(symbol, price):
    try:
        market = exchange.markets.get(symbol)
        precision = market.get('precision', {}).get('price')
        if precision is not None:
            return float(round(price, int(precision)))
    except Exception:
        pass
    return float(price)

# =================== ORDER HELPERS ===================
def set_leverage(symbol, leverage):
    try:
        sym = symbol.replace('/', '')
        # ccxt camelCase method
        exchange.fapiPrivatePostLeverage({'symbol': sym, 'leverage': int(leverage)})
        logging.info(f"Leverage {leverage} set for {symbol}")
    except Exception as e:
        logging.warning(f"Set leverage failed: {e}")

def create_limit_entry(symbol, side_ccxt, amount, limit_price):
    # YAHAN DIRECT amount (LOT_SIZE) use ho raha hai
    limit_price_rounded = _round_price(symbol, limit_price)
    order = exchange.create_order(
        symbol, 'limit', side_ccxt, amount, limit_price_rounded,
        {'reduceOnly': False}
    )
    return order

def create_market_entry(symbol, side_ccxt, amount):
    # YAHAN BHI DIRECT amount
    order = exchange.create_order(
        symbol, 'market', side_ccxt, amount, None,
        {'reduceOnly': False}
    )
    return order

def place_tp_sl(symbol, dir_signal, amount, tp_price, sl_price):
    """
    TP = limit reduceOnly
    SL = STOP_MARKET reduceOnly
    """
    close_side = 'sell' if dir_signal == 'BUY' else 'buy'
    tp_order = None
    sl_order = None

    # TP LIMIT reduceOnly
    try:
        tp_price_rounded = _round_price(symbol, tp_price)
        tp_order = exchange.create_order(
            symbol, 'limit', close_side, amount, tp_price_rounded,
            {'reduceOnly': True}
        )
    except Exception as e:
        logging.warning(f"TP placement failed: {e}")

    # SL STOP_MARKET reduceOnly
    try:
        sl_price_rounded = _round_price(symbol, sl_price)
        sl_order = exchange.create_order(
            symbol, 'STOP_MARKET', close_side, amount, None,
            {'stopPrice': float(sl_price_rounded), 'reduceOnly': True}
        )
    except Exception as e:
        logging.warning(f"SL placement failed: {e}")

    return tp_order, sl_order

# =================== 🔁 RSI STRATEGY (on closed candle) ===================
def compute_rsi_from_deque():
    """
    Build DataFrame from kline_deque and compute RSI (Wilder) on closes.
    Returns df with 'rsi' column, or None if not enough data.
    """
    if len(kline_deque) < RSI_PERIOD + 2:
        return None
    df = pd.DataFrame(list(kline_deque), columns=['open','high','low','close','volume','time_ms'])
    df['close'] = df['close'].astype(float)

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    return df

def check_rsi_signal_from_df(df):
    try:
        last = df.iloc[-1]
        r = float(last['rsi'])
    except Exception:
        return None
    if pd.isna(r):
        return None
    if r < RSI_LOW:
        return 'BUY'
    if r > RSI_HIGH:
        return 'SELL'
    return None

# =================== SMART LIMIT ENTRY ENGINE ===================
def smart_open_position(signal, approx_price):
    """
    Smart limit entry:
      - Attempt 1: limit at approx_price +/- LIMIT_BUFFER
      - Wait ENTRY_WAIT_SECONDS and check fill
      - Attempt 2: limit at fresh price +/- LIMIT_BUFFER
      - If still not filled: market fallback
    On success: sets global in_position, position, places TP/SL.
    """
    global in_position, position

    side_ccxt = 'buy' if signal == 'BUY' else 'sell'
    qty = LOT_SIZE  # EXACT qty

    try:
        set_leverage(SYMBOL, LEVERAGE)
    except Exception:
        pass

    def get_limit_price(base_price):
        if signal == 'BUY':
            return base_price + LIMIT_BUFFER
        else:
            return base_price - LIMIT_BUFFER

    entry_order = None
    filled = False
    entry_price_actual = None

    current_price_ref = approx_price
    for attempt in range(1, MAX_LIMIT_ATTEMPTS + 1):
        try:
            limit_price = _round_price(SYMBOL, get_limit_price(current_price_ref))
            log.info(f"[ENTRY] Attempt {attempt} LIMIT {signal} qty={qty} @ {limit_price}")
            print(f"[{now_str()}] ENTRY attempt {attempt}: LIMIT {signal} @ {limit_price}", flush=True)

            order = create_limit_entry(SYMBOL, side_ccxt, qty, limit_price)
            order_id = str(order.get('id')) if order and order.get('id') is not None else None

            t0 = time.time()
            while time.time() - t0 < ENTRY_WAIT_SECONDS:
                time.sleep(1)
                if not order_id:
                    break
                try:
                    ord_info = exchange.fetch_order(order_id, SYMBOL)
                    status = (ord_info.get('status') or '').lower()
                    if status in ('closed', 'filled'):
                        entry_order = ord_info
                        filled = True
                        break
                except Exception:
                    pass

            if filled:
                break

            if order_id:
                try:
                    exchange.cancel_order(order_id, SYMBOL)
                    log.info(f"[ENTRY] Canceled unfilled limit order {order_id}")
                except Exception as e:
                    log.warning(f"[ENTRY] Cancel failed for {order_id}: {e}")

            try:
                ticker = exchange.fetch_ticker(SYMBOL)
                last = ticker.get('last') or ticker.get('close')
                if last:
                    current_price_ref = float(last)
            except Exception:
                pass

        except Exception as e:
            log.error(f"[ENTRY] Limit attempt {attempt} error: {e}")
            time.sleep(1)

    if not filled:
        try:
            log.info(f"[ENTRY] Limit attempts failed -> MARKET fallback {signal}")
            print(f"[{now_str()}] Limit not filled -> MARKET fallback {signal}", flush=True)
            entry_order = create_market_entry(SYMBOL, side_ccxt, qty)
        except Exception as e:
            log.error(f"[ENTRY] Market fallback error: {e}")
            print(f"[{now_str()}] Market fallback error: {e}", flush=True)
            return False

    try:
        entry_price_actual = entry_order.get('average') or entry_order.get('price') or (entry_order.get('info') or {}).get('avgPrice')
    except Exception:
        entry_price_actual = None
    if not entry_price_actual:
        entry_price_actual = approx_price
    entry_price_actual = float(entry_price_actual)

    if signal == 'BUY':
        tp_price = entry_price_actual + TP_POINTS
        sl_price = entry_price_actual - SL_POINTS
    else:
        tp_price = entry_price_actual - TP_POINTS
        sl_price = entry_price_actual + SL_POINTS

    tp_order, sl_order = place_tp_sl(SYMBOL, signal, qty, tp_price, sl_price)

    position_local = {
        'dir': signal,
        'entry': float(entry_price_actual),
        'tp_price': float(tp_price),
        'sl_price': float(sl_price),
        'entry_time': now_ist(),
        'entry_id': str(entry_order.get('id')) if entry_order and entry_order.get('id') is not None else None,
        'tp_id': str(tp_order.get('id')) if tp_order and tp_order.get('id') is not None else None,
        'sl_id': str(sl_order.get('id')) if sl_order and sl_order.get('id') is not None else None
    }
    position = position_local
    in_position = True

    log.info(f"[OPEN] {signal} entry={position['entry']} tp={position['tp_price']} sl={position['sl_price']} tp_id={position['tp_id']} sl_id={position['sl_id']}")
    print(f"[{now_str()}] OPENED {signal} entry={position['entry']} tp={position['tp_price']} sl={position['sl_price']}", flush=True)
    return True

# =================== KLINE WEBSOCKET (public) ===================
def on_kline_message(ws, message):
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
            t = int(k.get('t'))
            kline_deque.append([o,h,l,c,v,t])

            df = compute_rsi_from_deque()
            if df is None:
                return
            signal = check_rsi_signal_from_df(df)

            global last_processed_candle_time, in_position, position, cooldown_until
            last_iso = pd.to_datetime(k.get('t'), unit='ms', utc=True).tz_convert('Asia/Kolkata').isoformat()

            if cooldown_until is not None and now_ist() < cooldown_until:
                print(f"[{now_str()}] In cooldown until {cooldown_until} -> skipping signal", flush=True)
                return

            if signal and not in_position:
                if last_processed_candle_time == last_iso:
                    return

                approx_entry_price = float(c)
                print(f"[{now_str()}] RSI signal {signal} detected (RSI strategy). Using smart limit entry...", flush=True)
                log.info(f"RSI signal {signal} -> smart limit entry from approx price {approx_entry_price}")

                ok = smart_open_position(signal, approx_entry_price)
                if ok:
                    last_processed_candle_time = last_iso
                else:
                    print(f"[{now_str()}] Failed to open position on signal {signal}", flush=True)
                    log.error(f"Failed to open position on signal {signal}")

        except Exception as e:
            logging.debug(f"kline parse error: {e}")

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
            log.error(f"kline ws run_forever error: {e}")
        time.sleep(2)

# =================== USER DATA WEBSOCKET (listenKey) ===================
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
    status = o.get('X')
    order_id_raw = o.get('i')
    order_id = str(order_id_raw) if order_id_raw is not None else None
    avg_price_str = o.get('ap')
    try:
        avg_price = float(avg_price_str) if avg_price_str not in (None, '', '0') else None
    except:
        avg_price = None

    if not in_position or not position:
        return

    tp_id = str(position.get('tp_id')) if position.get('tp_id') is not None else None
    sl_id = str(position.get('sl_id')) if position.get('sl_id') is not None else None

    if order_id in (tp_id, sl_id) and status == 'FILLED':
        outcome = 'TP' if order_id == tp_id else 'SL'
        exit_price = avg_price if avg_price else (position.get('tp_price') if outcome=='TP' else position.get('sl_price'))
        entry_price = float(position.get('entry'))
        dir_side = position.get('dir')
        if dir_side == 'BUY':
            pnl = (exit_price - entry_price) * LOT_SIZE
        else:
            pnl = (entry_price - exit_price) * LOT_SIZE

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
        print(f"[{now_str()}] {outcome} closed via user WS. PnL: {round(pnl,6)} | Account USDT: {rec['balance']}", flush=True)
        log.info(f"{outcome} closed via user WS. {rec}")

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
            # ccxt camelCase method
            res = exchange.fapiPrivatePostListenKey()
            if isinstance(res, dict):
                lk = res.get('listenKey')
            else:
                lk = res
            if not lk:
                print("[user WS] listenKey creation failed", flush=True)
                log.error("listenKey creation returned no key")
                time.sleep(5)
                continue
            with listen_key_lock:
                listen_key = lk
            ws_url = f"wss://fstream.binance.com/ws/{listen_key}"
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
            log.error(f"user ws run_forever error: {e}")
        time.sleep(2)

def listenkey_keepalive_worker(lk):
    while not stop_all:
        try:
            time.sleep(60 * 25)
            # ccxt camelCase method
            exchange.fapiPrivatePutListenKey({'listenKey': lk})
            logging.debug("Sent listenKey keepalive")
        except Exception as e:
            logging.debug(f"listenKey keepalive error: {e}")

# =================== MONITOR POLLER ===================
def handle_detected_close(outcome, exit_price=None):
    global in_position, position, cooldown_until, current_balance
    try:
        if not position:
            return
        entry_price = float(position.get('entry'))
        dir_side = position.get('dir')
        if exit_price is None:
            try:
                ticker = exchange.fetch_ticker(SYMBOL)
                exit_price = float(ticker.get('last') or ticker.get('close'))
            except:
                exit_price = entry_price
        if dir_side == 'BUY':
            pnl = (exit_price - entry_price) * LOT_SIZE
        else:
            pnl = (entry_price - exit_price) * LOT_SIZE

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
        print(f"[{now_str()}] {outcome} detected by monitor. PnL: {round(pnl,6)} | Account USDT: {rec['balance']}", flush=True)
        log.info(f"{outcome} detected by monitor. {rec}")

        in_position = False
        position = None
        if outcome == 'SL':
            cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
            print(f"[{now_str()}] SL hit (monitor) → cooldown until {cooldown_until}", flush=True)
            log.info(f"SL cooldown until {cooldown_until}")
        else:
            print(f"[{now_str()}] TP/manual close detected (monitor). Ready for next valid setup.", flush=True)
    except Exception as e:
        logging.debug(f"handle_detected_close error: {e}")

def monitor_positions_poller(poll_interval=5):
    global in_position, position, stop_all
    while not stop_all:
        try:
            time.sleep(poll_interval)
            if not in_position or not position:
                continue

            tp_id = position.get('tp_id')
            sl_id = position.get('sl_id')

            found_fill = False
            if tp_id:
                try:
                    ord_tp = exchange.fetch_order(tp_id, SYMBOL)
                    status = (ord_tp.get('status') or '').lower()
                    if status in ('closed','filled'):
                        exit_price = ord_tp.get('average') or ord_tp.get('price') or (ord_tp.get('info') or {}).get('avgPrice')
                        try:
                            exit_price = float(exit_price) if exit_price is not None else None
                        except:
                            exit_price = None
                        handle_detected_close('TP', exit_price)
                        found_fill = True
                except Exception:
                    pass
            if found_fill:
                continue

            if sl_id:
                try:
                    ord_sl = exchange.fetch_order(sl_id, SYMBOL)
                    status = (ord_sl.get('status') or '').lower()
                    if status in ('closed','filled'):
                        exit_price = ord_sl.get('average') or ord_sl.get('price') or (ord_sl.get('info') or {}).get('avgPrice')
                        try:
                            exit_price = float(exit_price) if exit_price is not None else None
                        except:
                            exit_price = None
                        handle_detected_close('SL', exit_price)
                        found_fill = True
                except Exception:
                    pass
            if found_fill:
                continue

            try:
                pos_list = None
                try:
                    # ccxt camelCase method for position risk
                    pos_list = exchange.fapiPrivateGetPositionRisk()
                except Exception:
                    pos_list = None
                if not pos_list:
                    try:
                        pos_list = exchange.fetch_positions([SYMBOL])
                    except Exception:
                        pos_list = None
                if pos_list:
                    found = None
                    for p in pos_list:
                        sym = p.get('symbol')
                        if sym:
                            if sym == SYMBOL.replace('/','') or sym == SYMBOL:
                                found = p
                                break
                    if found:
                        pos_amt = None
                        if 'positionAmt' in found:
                            try:
                                pos_amt = float(found.get('positionAmt', 0))
                            except:
                                pos_amt = None
                        elif isinstance(found.get('contracts', None), (int,float,str)):
                            try:
                                pos_amt = float(found.get('contracts', 0))
                            except:
                                pos_amt = None
                        elif isinstance(found.get('amount', None), (int,float,str)):
                            try:
                                pos_amt = float(found.get('amount', 0))
                            except:
                                pos_amt = None
                        if pos_amt is not None and abs(pos_amt) < 1e-8:
                            handle_detected_close('MANUAL', exit_price=None)
                            continue
            except Exception as e:
                logging.debug(f"monitor pos fallback error: {e}")
        except Exception as e:
            logging.debug(f"monitor_positions_poller error: {e}")
            time.sleep(1)

# =================== STARTUP ===================
print(f"[{now_str()}] 🚀 [RSI LIVE BOT] {SYMBOL} | Binance Perpetual | Smart LIMIT Entry Mode", flush=True)
log.info("Starting full websocket RSI bot with smart limit entry + monitor")

with balance_lock:
    current_balance = fetch_usdt_balance()
if current_balance is not None:
    print(f"[{now_str()}] Starting account USDT balance: {current_balance}", flush=True)
    log.info(f"Starting account USDT balance: {current_balance}")
else:
    print(f"[{now_str()}] Warning: could not fetch starting USDT balance.", flush=True)
    log.warning("Could not fetch starting USDT balance.")

t_user = threading.Thread(target=start_user_ws, daemon=True)
t_user.start()
t_kline = threading.Thread(target=start_kline_ws, daemon=True)
t_kline.start()
t_monitor = threading.Thread(target=monitor_positions_poller, kwargs={'poll_interval':5}, daemon=True)
t_monitor.start()

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
