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
LOT_SIZE = 0.01
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 75
COOLDOWN_MINUTES = 30
CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'

# API KEYS - FILL LOCALLY
API_KEY = 'czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ'
API_SECRET = 'cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO'

# =================== LOGGING ===================
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# =================== TIMEZONE/HELPERS ===================
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
last_processed_candle_time = None  # will store integer ms timestamp of last processed closed candle
balance_lock = threading.Lock()
current_balance = None

# For kline history (we'll keep last 300 candles)
kline_deque = deque(maxlen=300)  # store [open,high,low,close,volume,time_ms]

# websocket objects
ws_kline = None
stop_all = False

# =================== UTILITIES ===================
def fetch_usdt_balance():
    try:
        bal = exchange.fetch_balance({'type': 'future'})
        # try various shapes
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

def _round_amount(symbol, amount):
    try:
        market = exchange.markets.get(symbol)
        precision = market.get('precision', {}).get('amount')
        if precision is not None:
            return float(round(amount, precision))
    except Exception:
        pass
    return amount

# =================== ORDER HELPERS ===================
def set_leverage(symbol, leverage):
    try:
        sym = symbol.replace('/', '')
        # try multiple ccxt method names for different versions
        try:
            exchange.fapiPrivate_post_leverage({'symbol': sym, 'leverage': int(leverage)})
        except Exception:
            exchange.request('fapi/v1/leverage','POST',{'symbol':sym,'leverage':int(leverage)})
        logging.info(f"Leverage {leverage} set for {symbol}")
    except Exception as e:
        logging.warning(f"Set leverage failed: {e}")

def create_market_entry(symbol, side, amount):
    amount_rounded = _round_amount(symbol, amount)
    order = exchange.create_order(symbol, 'market', side.lower(), amount_rounded, None, {'reduceOnly': False})
    return order

def place_tp_sl(symbol, side, amount, tp_price, sl_price):
    close_side = 'sell' if side == 'BUY' else 'buy'
    amount_rounded = _round_amount(symbol, amount)
    tp_order = None; sl_order = None
    try:
        tp_order = exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', close_side, amount_rounded, None,
                                         {'stopPrice': float(tp_price), 'reduceOnly': True})
    except Exception as e:
        logging.warning(f"TP placement failed: {e}")
    try:
        sl_order = exchange.create_order(symbol, 'STOP_MARKET', close_side, amount_rounded, None,
                                         {'stopPrice': float(sl_price), 'reduceOnly': True})
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
    # expects df with ema columns and last row is latest closed candle
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
    """
    Handle public kline stream messages.
    We append closed kline to kline_deque when 'k'['x'] == True
    """
    global last_processed_candle_time, in_position, position, cooldown_until
    try:
        data = json.loads(message)
    except Exception as e:
        print(f"[kline] invalid json: {e}", flush=True)
        return

    # stream wrapper if using /stream?streams=... returns {"stream":..., "data":{...}}
    if 'data' in data and isinstance(data['data'], dict):
        payload = data['data']
    else:
        payload = data

    # kline payload
    k = payload.get('k') or {}
    is_closed = k.get('x')
    if is_closed:
        # append: open, high, low, close, volume, time_ms
        try:
            o = float(k.get('o')); h = float(k.get('h')); l = float(k.get('l')); c = float(k.get('c')); v = float(k.get('v'))
            t = int(k.get('t'))  # start time in ms
            kline_deque.append([o,h,l,c,v,t])
            # compute EMAs & check signal
            df = compute_emas_from_deque()
            if df is None:
                return

            # debug EMA / close values
            last_row = df.iloc[-1]
            try:
                print(f"[DEBUG EMA] close={last_row['close']:.6f} ema5={last_row['ema5']:.6f} ema9={last_row['ema9']:.6f} ema15={last_row['ema15']:.6f} ema21={last_row['ema21']:.6f}", flush=True)
            except Exception:
                pass

            signal = check_signal_from_df(df)
            # protect last_processed_candle_time to avoid double entries
            # use integer timestamp (ms) to avoid timezone/string issues
            candle_ts = int(t)
            # if cooldown active, skip
            if cooldown_until is not None and now_ist() < cooldown_until:
                print(f"[{now_str()}] In cooldown until {cooldown_until} -> skipping signal", flush=True)
                return
            if signal and not in_position:
                # ensure we didn't already process this candle
                if last_processed_candle_time == candle_ts:
                    return
                # compute entry (use close as approximate entry; market order will execute current price)
                entry_price = float(c)
                if signal == 'BUY':
                    tp_price = entry_price + TP_POINTS
                    sl_price = entry_price - SL_POINTS
                else:
                    tp_price = entry_price - TP_POINTS
                    sl_price = entry_price + SL_POINTS

                try:
                    print(f"[{now_str()}] Signal {signal} detected from kline close (ts={candle_ts}). Placing market entry...", flush=True)
                    log.info(f"Signal {signal} -> entry at market, tp={tp_price}, sl={sl_price}")
                    # set leverage
                    try:
                        set_leverage(SYMBOL, LEVERAGE)
                    except Exception:
                        pass
                    side_ccxt = 'buy' if signal == 'BUY' else 'sell'
                    entry_order = create_market_entry(SYMBOL, side_ccxt, LOT_SIZE)
                    print(f"[DEBUG ORDER] entry_order response: {entry_order}", flush=True)

                    # derive entry price and qty
                    entry_price_actual = None
                    qty_actual = None
                    try:
                        # ccxt shapes differ; try common fields
                        entry_price_actual = entry_order.get('average') or entry_order.get('price') or (entry_order.get('info') or {}).get('avgPrice')
                        qty_actual = (entry_order.get('filled') or entry_order.get('filledQty') or entry_order.get('amount') or (entry_order.get('info') or {}).get('executedQty') or None)
                    except Exception:
                        entry_price_actual = None
                        qty_actual = None
                    try:
                        entry_price_actual = float(entry_price_actual) if entry_price_actual else float(entry_price)
                    except:
                        entry_price_actual = float(entry_price)
                    try:
                        qty_actual = float(qty_actual) if qty_actual else float(LOT_SIZE)
                    except:
                        qty_actual = float(LOT_SIZE)

                    # place TP/SL (two orders)
                    tp_order, sl_order = place_tp_sl(SYMBOL, signal, qty_actual, tp_price, sl_price)
                    print(f"[DEBUG ORDER] tp_order: {tp_order}; sl_order: {sl_order}", flush=True)

                    position = {
                        'dir': signal,
                        'entry': float(entry_price_actual),
                        'tp_price': float(tp_price),
                        'sl_price': float(sl_price),
                        'entry_time': now_ist(),
                        'entry_id': str(entry_order.get('id')) if entry_order and entry_order.get('id') is not None else None,
                        'tp_id': str(tp_order.get('id')) if tp_order and tp_order.get('id') is not None else None,
                        'sl_id': str(sl_order.get('id')) if sl_order and sl_order.get('id') is not None else None,
                        'qty': float(qty_actual)
                    }
                    in_position = True
                    last_processed_candle_time = candle_ts
                    print(f"[{now_str()}] OPENED {signal} entry={position['entry']} qty={position['qty']} tp_id={position['tp_id']} sl_id={position['sl_id']}", flush=True)
                    log.info(f"Opened live {signal}: {position}")
                except Exception as e:
                    print(f"[{now_str()}] Error placing live trade: {e}", flush=True)
                    log.error(f"Error placing live trade: {e}")
                    in_position = False
                    position = None
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
    """
    Connects to public kline stream for SYMBOL at 1m.
    """
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

# =================== NEW: MONITOR (poller) to detect TP/SL / manual close ===================
def handle_detected_close(outcome, exit_price=None, qty=None):
    """
    outcome: 'TP','SL','MANUAL'
    exit_price: price or None
    qty: closed qty (contracts) or None
    """
    global in_position, position, cooldown_until, current_balance
    try:
        if not position:
            return
        entry_price = float(position.get('entry'))
        dir_side = position.get('dir')

        # qty: prefer provided qty, then position['qty'], then LOT_SIZE
        qty_used = None
        try:
            if qty is not None:
                qty_used = float(qty)
            elif position.get('qty') is not None:
                qty_used = float(position.get('qty'))
            else:
                qty_used = float(LOT_SIZE)
        except:
            qty_used = float(LOT_SIZE)

        # If exit_price not provided, try to approximate by current ticker
        if exit_price is None:
            try:
                ticker = exchange.fetch_ticker(SYMBOL)
                exit_price = float(ticker.get('last') or ticker.get('close'))
            except:
                exit_price = entry_price

        if dir_side == 'BUY':
            pnl = (exit_price - entry_price) * qty_used
        else:
            pnl = (entry_price - exit_price) * qty_used

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
        print(f"[{now_str()}] {outcome} detected by monitor. qty={qty_used} PnL: {round(pnl,6)} | Account USDT: {rec['balance']}", flush=True)
        log.info(f"{outcome} detected by monitor. {rec}")

        # cleanup and apply cooldown if SL
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
    """
    Polls exchange to confirm if current position closed (TP/SL or manual).
    Strategy:
      1) If in_position True and position has tp_id or sl_id, try fetching those orders to see if filled.
      2) If not found or not filled, fallback to position risk endpoint to see positionAmt for SYMBOL.
      3) If positionAmt == 0 -> treat as closed (MANUAL if no order found).
    """
    global in_position, position, stop_all
    while not stop_all:
        try:
            time.sleep(poll_interval)
            if not in_position or not position:
                continue

            tp_id = position.get('tp_id')
            sl_id = position.get('sl_id')

            # 1) try to check TP/SL orders by id (best effort)
            found_fill = False
            # check TP order
            if tp_id:
                try:
                    ord_tp = exchange.fetch_order(tp_id, SYMBOL)
                    status = (ord_tp.get('status') or '').lower()
                    # statuses may vary; check common filled/closed indicators
                    if status in ('closed','filled','done'):
                        exit_price = ord_tp.get('average') or ord_tp.get('price') or (ord_tp.get('info') or {}).get('avgPrice')
                        # try extract qty from order
                        qty = None
                        try:
                            qty = ord_tp.get('filled') or ord_tp.get('filledQty') or ord_tp.get('amount') or (ord_tp.get('info') or {}).get('executedQty')
                        except:
                            qty = None
                        try:
                            qty = float(qty) if qty is not None else None
                        except:
                            qty = None
                        try:
                            exit_price = float(exit_price) if exit_price is not None else None
                        except:
                            exit_price = None
                        handle_detected_close('TP', exit_price, qty)
                        found_fill = True
                except Exception:
                    pass
            if found_fill:
                continue

            # check SL order
            if sl_id:
                try:
                    ord_sl = exchange.fetch_order(sl_id, SYMBOL)
                    status = (ord_sl.get('status') or '').lower()
                    if status in ('closed','filled','done'):
                        exit_price = ord_sl.get('average') or ord_sl.get('price') or (ord_sl.get('info') or {}).get('avgPrice')
                        qty = None
                        try:
                            qty = ord_sl.get('filled') or ord_sl.get('filledQty') or ord_sl.get('amount') or (ord_sl.get('info') or {}).get('executedQty')
                        except:
                            qty = None
                        try:
                            qty = float(qty) if qty is not None else None
                        except:
                            qty = None
                        try:
                            exit_price = float(exit_price) if exit_price is not None else None
                        except:
                            exit_price = None
                        handle_detected_close('SL', exit_price, qty)
                        found_fill = True
                except Exception:
                    pass
            if found_fill:
                continue

            # 2) fallback to position risk to detect if position size is zero
            try:
                pos_list = None
                try:
                    pos_list = exchange.fapiPrivate_get_positionrisk()
                except Exception:
                    pos_list = None
                if not pos_list:
                    # try ccxt fetch_positions
                    try:
                        pos_list = exchange.fetch_positions([SYMBOL])
                    except Exception:
                        pos_list = None
                if pos_list:
                    found = None
                    for p in pos_list:
                        # positionRisk returns 'symbol' like 'BNBUSDT'
                        sym = p.get('symbol') or p.get('symbol', None)
                        if sym:
                            if isinstance(sym, str) and (sym.replace('USDT','/USDT') == SYMBOL.replace('/','') or sym == SYMBOL):
                                found = p
                                break
                    if found:
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
                        elif isinstance(found.get('amount', None), (int, float, str)):
                            try:
                                pos_amt = float(found.get('amount', 0))
                            except:
                                pos_amt = None
                        # if closed (zero)
                        if pos_amt is not None and abs(pos_amt) < 1e-8:
                            # No order fill info available -> treat as MANUAL close
                            handle_detected_close('MANUAL', exit_price=None, qty=None)
                            continue
            except Exception as e:
                logging.debug(f"monitor pos fallback error: {e}")
                # continue and next poll

        except Exception as e:
            logging.debug(f"monitor_positions_poller error: {e}")
            time.sleep(1)
    # end while

# =================== WATCHDOG (optional safety) ===================
def watchdog_checker():
    """
    Resets stale last_processed_candle_time or stuck in_position if obviously stale.
    """
    global last_processed_candle_time, in_position, position, stop_all
    while not stop_all:
        try:
            time.sleep(60)  # check every minute
            # reset last_processed_candle_time if older than 2 hours
            try:
                if last_processed_candle_time:
                    age_ms = int(time.time() * 1000) - int(last_processed_candle_time)
                    if age_ms > 1000 * 60 * 60 * 2:
                        print("[WATCHDOG] last_processed_candle_time stale -> resetting", flush=True)
                        last_processed_candle_time = None
            except Exception:
                last_processed_candle_time = None
            # if in_position stuck very long (>6 hours), log and reset (rare)
            try:
                if in_position and position and position.get('entry_time'):
                    et = position.get('entry_time')
                    # convert to naive UTC for comparison
                    try:
                        delta = datetime.utcnow() - et.replace(tzinfo=None)
                        if delta.total_seconds() > 60 * 60 * 6:
                            print("[WATCHDOG] position stuck >6h, resetting state", flush=True)
                            log.warning("Watchdog resetting stuck position")
                            in_position = False
                            position = None
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            time.sleep(5)

# =================== STARTUP ===================
print(f"[{now_str()}] 🚀 [EMA LIVE BOT] {SYMBOL} | Binance Perpetual | Kline WS + Monitor", flush=True)
log.info("Starting websocket EMA bot with monitor")

with balance_lock:
    current_balance = fetch_usdt_balance()
if current_balance is not None:
    print(f"[{now_str()}] Starting account USDT balance: {current_balance}", flush=True)
    log.info(f"Starting account USDT balance: {current_balance}")
else:
    print(f"[{now_str()}] Warning: could not fetch starting USDT balance.", flush=True)
    log.warning("Could not fetch starting USDT balance.")

# start kline ws thread (daemon)
t_kline = threading.Thread(target=start_kline_ws, daemon=True)
t_kline.start()
# start monitor thread (daemon)
t_monitor = threading.Thread(target=monitor_positions_poller, kwargs={'poll_interval':5}, daemon=True)
t_monitor.start()
# start watchdog thread (daemon)
t_watchdog = threading.Thread(target=watchdog_checker, daemon=True)
t_watchdog.start()

# set leverage once at startup (best-effort)
try:
    set_leverage(SYMBOL, LEVERAGE)
except Exception as e:
    logging.warning(f"Leverage set failed at startup: {e}")

# =================== MAIN LOOP (minimal) ===================
try:
    while True:
        # main thread does little: just monitor flags, keep alive, print status
        time.sleep(1)
        if stop_all:
            break
except KeyboardInterrupt:
    print("\n[INFO] KeyboardInterrupt received. Shutting down...", flush=True)
    stop_all = True
    try:
        if ws_kline:
            ws_kline.close()
    except Exception:
        pass
    sys.exit(0)
