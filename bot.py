# live_binance_perp_ws_bot_complete_refined.py

import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import traceback
import os
import csv
import logging
import sys
import threading
import websocket
import json

# =================== CONFIG ===================
SYMBOL = 'BNB/USDT'
TIMEFRAME = '1m'
LOT_SIZE = 0.10
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 100
COOLDOWN_MINUTES = 30
POLL_INTERVAL_SECONDS = 5
CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'

# --- Binance API keys placeholders ---
API_KEY = 'czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ'
API_SECRET = 'cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO'

# =================== LOGGING ===================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# =================== TIMEZONE ===================
KOLKATA = timezone(timedelta(hours=5, minutes=30))
def now_ist(): return datetime.now(timezone.utc).astimezone(KOLKATA)
def now_str(): return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== EXCHANGE ===================
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
cooldown_until = None
position = None
last_processed_candle_time = None
ws_stop = False
current_balance = None
balance_lock = threading.Lock()

listen_key = None
listen_key_last_refresh = None
LISTENKEY_REFRESH_INTERVAL = 25 * 60  # refresh every 25 minutes

# =================== UTILS ===================
def fetch_usdt_balance():
    try:
        bal = exchange.fetch_balance({'type': 'future'})
        if isinstance(bal, dict):
            if 'USDT' in bal and isinstance(bal['USDT'], dict):
                if bal['USDT'].get('total') is not None:
                    return float(bal['USDT']['total'])
                if bal['USDT'].get('free') is not None:
                    return float(bal['USDT']['free'])
            total = bal.get('total')
            if isinstance(total, dict) and 'USDT' in total:
                return float(total['USDT'])
            if isinstance(bal.get('total'), dict) and 'USDT' in bal.get('total'):
                return float(bal.get('total')['USDT'])
        return None
    except Exception as e:
        logging.warning(f"fetch_usdt_balance failed: {e}")
        return None

def append_trade_csv(record):
    header = ['time', 'dir', 'entry', 'exit', 'outcome', 'pnl', 'balance']
    file_exists = os.path.isfile(CSV_FN)
    with open(CSV_FN, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
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

# =================== EMA STRATEGY ===================
def fetch_latest_candles(symbol, timeframe, limit=200):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 10: return None
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
        return df
    except Exception as e:
        print(f"[ERROR] Fetch candles failed: {e}", flush=True)
        log.error(f"Fetch candles failed: {e}")
        return None

def compute_emas(df):
    df = df.copy()
    df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema15'] = df['close'].ewm(span=15, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    return df

def check_signal(candle):
    try:
        c = float(candle['close'])
        l = float(candle['low'])
        h = float(candle['high'])
        ema5 = float(candle['ema5'])
        ema9 = float(candle['ema9'])
        ema15 = float(candle['ema15'])
        ema21 = float(candle['ema21'])
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

# =================== ORDER HELPERS ===================
def set_leverage(symbol, leverage):
    try:
        sym = symbol.replace('/', '')
        exchange.fapiPrivate_post_leverage({'symbol': sym, 'leverage': int(leverage)})
        logging.info(f"Leverage {leverage} set for {symbol}")
    except Exception as e:
        logging.warning(f"Set leverage failed: {e}")

def create_market_entry(symbol, side, amount):
    try:
        amount_rounded = _round_amount(symbol, amount)
        logging.info(f"Placing market entry: {side} {amount_rounded} {symbol}")
        order = exchange.create_order(symbol, 'market', side.lower(), amount_rounded, None, {'reduceOnly': False})
        logging.info(f"Entry order placed: id={order.get('id')}")
        return order
    except Exception as e:
        logging.error(f"Market entry failed: {e}")
        raise

def place_tp_sl(symbol, side, amount, tp_price, sl_price):
    close_side = 'sell' if side == 'BUY' else 'buy'
    amount_rounded = _round_amount(symbol, amount)
    tp_order = sl_order = None
    try:
        tp_order = exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', close_side, amount_rounded, None, {'stopPrice': float(tp_price), 'reduceOnly': True})
        logging.info(f"TP order placed id={tp_order.get('id')}")
    except Exception as e:
        logging.warning(f"TP placement failed: {e}")
    try:
        sl_order = exchange.create_order(symbol, 'STOP_MARKET', close_side, amount_rounded, None, {'stopPrice': float(sl_price), 'reduceOnly': True})
        logging.info(f"SL order placed id={sl_order.get('id')}")
    except Exception as e:
        logging.warning(f"SL placement failed: {e}")
    return tp_order, sl_order

# =================== LISTENKEY HANDLING ===================
def create_listen_key():
    global listen_key, listen_key_last_refresh
    try:
        res = exchange.fapiPrivate_post_listenKey()
        listen_key = res.get('listenKey') if isinstance(res, dict) else res
        if not listen_key:
            print(f"[WS] ListenKey creation failed, response={res}", flush=True)
            log.error(f"ListenKey creation failed, response={res}")
            return False
        listen_key_last_refresh = time.time()
        print(f"[WS] ListenKey created", flush=True)
        log.info("ListenKey created")
        return True
    except Exception as e:
        print(f"[WS] ListenKey creation exception: {e}", flush=True)
        log.error(f"ListenKey creation exception: {e}")
        return False

def refresh_listen_key_if_needed():
    global listen_key, listen_key_last_refresh
    if not listen_key:
        return create_listen_key()
    if time.time() - listen_key_last_refresh > LISTENKEY_REFRESH_INTERVAL:
        try:
            exchange.fapiPrivate_put_listenKey({'listenKey': listen_key})
            listen_key_last_refresh = time.time()
            print(f"[WS] ListenKey refreshed", flush=True)
            log.info("ListenKey refreshed")
        except Exception as e:
            print(f"[WS] ListenKey refresh failed: {e}", flush=True)
            log.error(f"ListenKey refresh failed: {e}")

# =================== WEBSOCKET HANDLER ===================
def on_message(ws, msg):
    global in_position, position, current_balance
    try:
        data = json.loads(msg)
    except Exception as e:
        print(f"[WS] invalid json: {e}", flush=True)
        return

    evt_type = data.get('e')
    if evt_type != 'ORDER_TRADE_UPDATE':
        return

    o = data.get('o') or {}
    status = o.get('X')
    order_id_raw = o.get('i')
    order_id = str(order_id_raw) if order_id_raw is not None else None
    side = (o.get('S') or '').upper()
    avg_price_str = o.get('ap')
    try:
        avg_price = float(avg_price_str) if avg_price_str not in (None, '', '0') else None
    except Exception:
        avg_price = None

    if not in_position or not position:
        return

    tp_id = str(position.get('tp_id')) if position.get('tp_id') else None
    sl_id = str(position.get('sl_id')) if position.get('sl_id') else None

    if order_id in (tp_id, sl_id) and status == 'FILLED':
        outcome = 'TP' if order_id == tp_id else 'SL'
        exit_price = avg_price or (position.get('tp_price') if outcome == 'TP' else position.get('sl_price'))
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
            'pnl': round(pnl, 6),
            'balance': round(bal, 6) if bal else None
        }
        append_trade_csv(rec)
        print(f"[{now_str()}] {outcome} closed via WS. PnL: {round(pnl,6)} | Account USDT: {rec['balance']}", flush=True)
        log.info(f"{outcome} closed via WS. PnL: {round(pnl,6)} | Account USDT: {rec['balance']}")

        # cleanup state
        in_position = False
        position = None

        global cooldown_until
        if outcome == 'SL':
            cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
            print(f"[{now_str()}] SL hit → cooldown until {cooldown_until}", flush=True)
            log.info(f"SL cooldown until {cooldown_until}")
        else:
            print(f"[{now_str()}] TP occurred → re-entry blocked until next candle.", flush=True)
            log.info("Re-entry blocked on same candle after TP (exchange).")

def on_error(ws, err):
    print(f"[WS ERROR] {err}", flush=True)
    log.error(f"WS ERROR: {err}")

def on_close(ws, close_status, close_msg):
    print("[WS] closed", flush=True)
    log.info("WS closed")

def on_open(ws):
    print("[WS] connected", flush=True)
    log.info("WS connected")

def start_ws():
    global ws_stop
    if not create_listen_key():
        print("[WS] Cannot start WS without listenKey.", flush=True)
        return

    while not ws_stop:
        try:
            refresh_listen_key_if_needed()
            ws_url = f"wss://fstream.binance.com/ws/{listen_key}"
            ws = websocket.WebSocketApp(ws_url, on_message=on_message, on_error=on_error, on_close=on_close, on_open=on_open)
            ws.run_forever()
        except Exception as e:
            print(f"[WS] run_forever error: {e}", flush=True)
            log.error(f"WS run_forever error: {e}")
        time.sleep(2)  # small delay before reconnect

# =================== STARTUP ===================
STARTUP_MSG = "🚀 [EMA LIVE BOT] BNB/USDT | Binance Perpetual | OCO Mode Enabled"
print(f"[{now_str()}] {STARTUP_MSG}", flush=True)
log.info(STARTUP_MSG)

with balance_lock:
    current_balance = fetch_usdt_balance()
    if current_balance is not None:
        print(f"[{now_str()}] Starting account USDT balance: {current_balance}", flush=True)
        log.info(f"Starting account USDT balance: {current_balance}")
    else:
        print(f"[{now_str()}] Warning: could not fetch starting USDT balance.", flush=True)
        log.warning("Could not fetch starting USDT balance.")

# Start websocket listener thread
ws_thread = threading.Thread(target=start_ws, daemon=True)
ws_thread.start()

# =================== MAIN LOOP ===================
try:
    set_leverage(SYMBOL, LEVERAGE)
except Exception as e:
    logging.warning(f"Leverage set failed at startup: {e}")

while True:
    try:
        df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
        if df is None or len(df) < 12:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        df = compute_emas(df)

        if cooldown_until and now_ist() < cooldown_until:
            print(f"[{now_str()}] In cooldown until {cooldown_until} ➡ sleeping...", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        last_closed = df.iloc[-2]
        live_candle = df.iloc[-1]
        next_open = float(live_candle['open'])
        last_closed_time_iso = str(last_closed['time'].isoformat())

        if in_position:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        signal = check_signal(last_closed)
        if signal:
            if last_processed_candle_time == last_closed_time_iso:
                print(f'[{now_str()}] Skipping entry: last_closed {last_closed_time_iso} already processed.', flush=True)
                log.info("Skipping entry because last_closed was already processed.")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            entry_price = float(next_open)
            if signal == "BUY":
                tp_price = entry_price + TP_POINTS
                sl_price = entry_price - SL_POINTS
            else:
                tp_price = entry_price - TP_POINTS
                sl_price = entry_price + SL_POINTS

            try:
                print(f"[{now_str()}] Placing live trade: {signal} amount={LOT_SIZE} TP={tp_price} SL={sl_price} Lev={LEVERAGE}", flush=True)
                log.info(f"Placing live trade: {signal} amount={LOT_SIZE} TP={tp_price} SL={sl_price} Lev={LEVERAGE}")

                side_ccxt = 'buy' if signal == 'BUY' else 'sell'
                entry_order = create_market_entry(SYMBOL, side_ccxt, LOT_SIZE)

                entry_price_actual = entry_order.get('average') or entry_order.get('price') or (entry_order.get('info') or {}).get('avgPrice')
                if not entry_price_actual:
                    try:
                        entry_price_actual = exchange.fetch_ticker(SYMBOL).get('last')
                    except Exception:
                        entry_price_actual = entry_price

                tp_order, sl_order = place_tp_sl(SYMBOL, signal, LOT_SIZE, tp_price, sl_price)

                position = {
                    'dir': signal,
                    'entry': float(entry_price_actual),
                    'tp_price': float(tp_price),
                    'sl_price': float(sl_price),
                    'entry_time': now_ist(),
                    'entry_id': str(entry_order.get('id')) if entry_order and entry_order.get('id') else None,
                    'tp_id': str(tp_order.get('id')) if tp_order and tp_order.get('id') else None,
                    'sl_id': str(sl_order.get('id')) if sl_order and sl_order.get('id') else None
                }
                in_position = True
                last_processed_candle_time = last_closed_time_iso

                print(f'[{now_str()}] OPENED LIVE {signal} entry={position["entry"]} entry_id={position["entry_id"]} tp_id={position["tp_id"]} sl_id={position["sl_id"]}', flush=True)
                log.info(f"Opened live {signal}: {position}")

            except Exception as e:
                print(f"[{now_str()}] Live execution error placing trade: {e}", flush=True)
                log.error(f"Live execution error placing trade: {e}")
                in_position = False
                position = None

        else:
            print(f'[{now_str()}] No valid signal or cooldown active.', flush=True)

        time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt received. Exiting gracefully...", flush=True)
        log.info("KeyboardInterrupt received. Stopping bot.")
        ws_stop = True
        time.sleep(1)
        sys.exit(0)
    except Exception as e:
        msg = f"[FATAL ERROR] {e}"
        print(f'[{now_str()}] {msg}', flush=True)
        log.error(msg)
        traceback.print_exc()
        time.sleep(5)
