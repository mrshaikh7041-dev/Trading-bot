# live_ema_binance_futures.py
"""
Robust live EMA futures bot using ccxt (Binance USDT-M futures).
Replace API_KEY / API_SECRET and test in a demo account / testnet first.
"""

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

# =================== CONFIG ===================
API_KEY = 'czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ'        # <-- put your API key
API_SECRET = 'cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO'  # <-- put your secret
SYMBOL = 'BNB/USDT'
TIMEFRAME = '1m'
LOT_SIZE = 0.01
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 75
POLL_INTERVAL_SECONDS = 5
CSV_FN = f'{SYMBOL.replace("/", "-")}_live_trades.csv'
LOG_FILE = 'bot_live.log'

# ✅ Added cooldown config
COOLDOWN_MINUTES = 30  # cooldown only after SL hit

# =================== LOGGING SETUP ===================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# =================== EXCHANGE (FUTURES) ===================
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

try:
    exchange.load_markets()
except Exception as e:
    print(f"[WARN] load_markets failed: {e}", flush=True)
    log.warning(f"load_markets failed: {e}")

# =================== STATE ===================
last_processed_candle_time = None
cooldown_until = None  # ✅ added global cooldown tracker

# =================== TIME ===================
KOLKATA = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)
def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== HELPERS ===================
def fetch_latest_candles(symbol, timeframe, limit=200):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 10:
            return None
        df = pd.DataFrame(bars, columns=['time','open','high','low','close','volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
        return df
    except Exception as e:
        log.error(f'Fetch candles failed: {e}')
        print(f"[{now_str()}] Fetch candles failed: {e}", flush=True)
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

def append_trade_csv(record):
    header = ['time','dir','entry','exit','outcome','pnl','balance','entry_order_id','tp_order_id','sl_order_id']
    file_exists = os.path.isfile(CSV_FN)
    try:
        with open(CSV_FN, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=header)
            if not file_exists:
                writer.writeheader()
            writer.writerow(record)
    except Exception as e:
        log.error(f'append_trade_csv failed: {e}')

def get_usdt_balance():
    try:
        bal = exchange.fetch_balance()
        totals = bal.get('total', {})
        if isinstance(totals, dict):
            for k,v in totals.items():
                if str(k).upper() == 'USDT':
                    return float(v or 0.0)
        info = bal.get('info') or {}
        if isinstance(info, dict):
            if 'totalWalletBalance' in info:
                return float(info.get('totalWalletBalance') or 0.0)
            assets = info.get('assets') or info.get('positions') or []
            if isinstance(assets, list):
                for a in assets:
                    if a.get('asset') == 'USDT':
                        return float(a.get('walletBalance') or a.get('balance') or 0.0)
        return 0.0
    except Exception as e:
        log.error(f'fetch_balance error: {e}')
        print(f"[{now_str()}] fetch_balance error: {e}", flush=True)
        return 0.0

def set_leverage(symbol, leverage):
    try:
        symbol_no_slash = symbol.replace('/', '')
        if hasattr(exchange, 'fapiPrivate_post_leverage'):
            resp = exchange.fapiPrivate_post_leverage({'symbol': symbol_no_slash, 'leverage': int(leverage)})
            log.info(f'Leverage set response: {resp}')
            print(f"[{now_str()}] Leverage set to {leverage}", flush=True)
        else:
            print(f"[{now_str()}] Leverage set API not available; skipping.", flush=True)
    except Exception as e:
        log.error(f'Could not set leverage: {e}')
        print(f"[{now_str()}] Warning: Could not set leverage via API: {e}", flush=True)

def place_market_entry(side, amount):
    try:
        side_str = 'buy' if side == 'BUY' else 'sell'
        order = exchange.create_order(symbol=SYMBOL, type='market', side=side_str, amount=amount, params={})
        return order
    except Exception as e:
        log.error(f'Entry order failed: {e}')
        print(f"[{now_str()}] Entry order failed: {e}", flush=True)
        return None

def place_tp_sl_orders(side, amount, tp_price, sl_price):
    tp_order = None
    sl_order = None
    try:
        tp_side = 'sell' if side == 'BUY' else 'buy'
        sl_side = 'sell' if side == 'BUY' else 'buy'

        try:
            tp_order = exchange.create_order(
                symbol=SYMBOL,
                type='limit',
                side=tp_side,
                amount=amount,
                price=tp_price,
                params={'reduceOnly': True, 'timeInForce': 'GTC'}
            )
        except Exception as e:
            log.warning(f'TP order creation failed: {e}')

        try:
            sl_order = exchange.create_order(
                symbol=SYMBOL,
                type='STOP_MARKET',
                side=sl_side,
                amount=amount,
                params={'stopPrice': sl_price, 'reduceOnly': True}
            )
        except Exception as e:
            log.warning(f'SL order creation failed: {e}')

        return {'tp_order': tp_order, 'sl_order': sl_order}
    except Exception as e:
        log.error(f'place_tp_sl_orders error: {e}')
        return {'tp_order': tp_order, 'sl_order': sl_order}

def fetch_open_orders_for_symbol():
    try:
        return exchange.fetch_open_orders(symbol=SYMBOL)
    except Exception as e:
        log.error(f'fetch_open_orders failed: {e}')
        return []

def cancel_order_by_id(order_id):
    try:
        return exchange.cancel_order(order_id, SYMBOL)
    except Exception as e:
        log.error(f'cancel_order {order_id} failed: {e}')
        return None

def get_position_size_for_symbol():
    try:
        if hasattr(exchange, 'fetch_positions'):
            try:
                positions = exchange.fetch_positions([SYMBOL])
                for p in positions:
                    if p.get('symbol') == SYMBOL or p.get('info', {}).get('symbol') == SYMBOL.replace('/', ''):
                        size = float(p.get('contracts', 0) or p.get('size', 0) or p.get('amount', 0) or p.get('positionAmt', 0) or 0)
                        return size
            except Exception:
                pass
        sym = SYMBOL.replace('/','')
        resp = exchange.fapiPrivate_get_positionrisk({'symbol': sym})
        if isinstance(resp, list):
            for r in resp:
                if r.get('symbol') == sym:
                    return float(r.get('positionAmt', 0) or 0)
        return 0.0
    except Exception as e:
        log.error(f'get_position_size error: {e}')
        return 0.0

def parse_order_id(order):
    if not order: return None
    if isinstance(order, dict):
        if order.get('id'): return order.get('id')
        info = order.get('info') or {}
        return info.get('orderId') or info.get('order_id') or info.get('id')
    return None

# ---------------- Threaded monitor ----------------
def monitor_orders_thread(tp_id, sl_id, entry_id, signal, entry_price, done_event, timeout_sec=3600):
    """
    Monitor TP/SL by checking order statuses and trades.
    Sets done_event when finished, writes CSV and sets cooldown on SL.
    """
    global cooldown_until
    start = time.time()
    outcome = None
    exit_price = None

    while True:
        if time.time() - start > timeout_sec:
            # timeout — mark UNKNOWN and exit
            outcome = 'UNKNOWN'
            break

        time.sleep(POLL_INTERVAL_SECONDS)

        # attempt to get order statuses
        tp_status = None
        sl_status = None
        try:
            if tp_id:
                tp_info = exchange.fetch_order(tp_id, SYMBOL)
                tp_status = (tp_info.get('status') or '').lower()
        except Exception:
            tp_status = None

        try:
            if sl_id:
                sl_info = exchange.fetch_order(sl_id, SYMBOL)
                sl_status = (sl_info.get('status') or '').lower()
        except Exception:
            sl_status = None

        # If any order shows filled/closed status -> determine outcome
        if tp_status and tp_status in ('closed', 'filled', 'closed()','filled()'):
            outcome = 'TP'
            # try to get fill price from trades
            try:
                trades = exchange.fetch_my_trades(symbol=SYMBOL, since=None, limit=200)
                for t in reversed(trades):
                    oid = t.get('order') or t.get('orderId') or (t.get('info') or {}).get('orderId')
                    if oid and str(oid) == str(tp_id):
                        exit_price = float(t.get('price') or t.get('info', {}).get('price') or 0)
                        break
            except Exception:
                exit_price = None
            # ensure other order canceled
            try:
                if sl_id:
                    cancel_order_by_id(sl_id)
            except Exception:
                pass
            break

        if sl_status and sl_status in ('closed', 'filled', 'closed()','filled()'):
            outcome = 'SL'
            try:
                trades = exchange.fetch_my_trades(symbol=SYMBOL, since=None, limit=200)
                for t in reversed(trades):
                    oid = t.get('order') or t.get('orderId') or (t.get('info') or {}).get('orderId')
                    if oid and str(oid) == str(sl_id):
                        exit_price = float(t.get('price') or t.get('info', {}).get('price') or 0)
                        break
            except Exception:
                exit_price = None
            try:
                if tp_id:
                    cancel_order_by_id(tp_id)
            except Exception:
                pass
            break

        # If both orders missing from open orders, check position size & last price fallback
        try:
            open_orders = fetch_open_orders_for_symbol()
            if not open_orders:
                pos_size = get_position_size_for_symbol()
                # fetch last price
                ticker = exchange.fetch_ticker(SYMBOL)
                current_price = float(ticker.get('last') or ticker.get('close') or 0)
                if abs(pos_size) < 0.0001:
                    # position closed — infer outcome from current_price vs tp/sl
                    if signal == 'BUY':
                        if current_price <= (entry_price - SL_POINTS) + 0.5:
                            outcome = 'SL'
                        elif current_price >= (entry_price + TP_POINTS) - 0.5:
                            outcome = 'TP'
                    elif signal == 'SELL':
                        if current_price >= (entry_price + SL_POINTS) - 0.5:
                            outcome = 'SL'
                        elif current_price <= (entry_price - TP_POINTS) + 0.5:
                            outcome = 'TP'
                    exit_price = current_price
                    if outcome:
                        # cancel remaining orders if any
                        try:
                            if tp_id: cancel_order_by_id(tp_id)
                            if sl_id: cancel_order_by_id(sl_id)
                        except Exception:
                            pass
                        break
        except Exception:
            pass

    # If no exit_price yet, try to fill from last trades as best-effort
    if exit_price is None:
        try:
            trades = exchange.fetch_my_trades(symbol=SYMBOL, since=None, limit=200)
            for t in reversed(trades):
                oid = t.get('order') or t.get('orderId') or (t.get('info') or {}).get('orderId')
                if oid and (str(oid) == str(tp_id) or str(oid) == str(sl_id)):
                    exit_price = float(t.get('price') or t.get('info', {}).get('price') or 0)
                    break
        except Exception:
            exit_price = None

    # compute pnl (approx)
    pnl = None
    try:
        if exit_price is not None:
            if signal == 'BUY':
                pnl = (exit_price - float(entry_price)) * LOT_SIZE
            else:
                pnl = (float(entry_price) - exit_price) * LOT_SIZE
        else:
            # fallback to points-based pnl if still unknown
            if outcome == 'TP':
                pnl = TP_POINTS * LOT_SIZE
            elif outcome == 'SL':
                pnl = -SL_POINTS * LOT_SIZE
            else:
                pnl = 0.0
    except Exception:
        pnl = 0.0

    rec = {
        'time': now_ist().isoformat(),
        'dir': signal,
        'entry': float(entry_price),
        'exit': float(exit_price) if exit_price is not None else None,
        'outcome': outcome,
        'pnl': round(pnl, 8) if pnl is not None else None,
        'balance': get_usdt_balance(),
        'entry_order_id': entry_id,
        'tp_order_id': tp_id,
        'sl_order_id': sl_id
    }
    append_trade_csv(rec)
    print(f"[{now_str()}] Monitor finished → outcome: {outcome} exit: {exit_price} pnl: {pnl}", flush=True)
    log.info(f"Monitor result: {rec}")

    if outcome == 'SL':
        cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
        print(f"[{now_str()}] Cooldown activated for {COOLDOWN_MINUTES} minutes after SL.", flush=True)

    # signal completion to main loop
    try:
        done_event.set()
    except Exception:
        pass

# =================== MAIN LOOP ===================
def main_loop():
    global last_processed_candle_time, cooldown_until

    print(f"[{now_str()}] Starting LIVE EMA Futures Bot ({SYMBOL}) | Leverage={LEVERAGE}", flush=True)
    set_leverage(SYMBOL, LEVERAGE)

    try:
        while True:
            try:
                # ✅ check cooldown
                if cooldown_until and now_ist() < cooldown_until:
                    remaining = (cooldown_until - now_ist()).total_seconds() / 60
                    print(f"[{now_str()}] In cooldown for {remaining:.1f} min after SL. Skipping entries.", flush=True)
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
                if df is None or len(df) < 12:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                df = compute_emas(df)

                pos_size = get_position_size_for_symbol()
                open_orders = fetch_open_orders_for_symbol()
                if abs(pos_size) > 0 or len(open_orders) > 0:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                last_closed = df.iloc[-2]
                live_candle = df.iloc[-1]
                next_open = float(live_candle['open'])
                last_closed_time_iso = str(last_closed['time'].isoformat())

                signal = check_signal(last_closed)
                if not signal:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                if last_processed_candle_time == last_closed_time_iso:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                usdt_bal = get_usdt_balance()
                entry_order = place_market_entry(signal, LOT_SIZE)
                if not entry_order:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                entry_price = float(entry_order.get('average') or entry_order.get('price') or next_open)
                tp_price = entry_price + TP_POINTS if signal == "BUY" else entry_price - TP_POINTS
                sl_price = entry_price - SL_POINTS if signal == "BUY" else entry_price + SL_POINTS

                placement = place_tp_sl_orders(signal, LOT_SIZE, tp_price, sl_price)
                tp_order, sl_order = placement.get('tp_order'), placement.get('sl_order')
                tp_id, sl_id, entry_id = parse_order_id(tp_order), parse_order_id(sl_order), parse_order_id(entry_order)

                last_processed_candle_time = last_closed_time_iso
                print(f"[{now_str()}] Entry placed @ {entry_price} | TP={tp_price} SL={sl_price}", flush=True)

                # Start threaded monitor and wait for its completion (or timeout)
                monitor_done = threading.Event()
                monitor_thread = threading.Thread(
                    target=monitor_orders_thread,
                    args=(tp_id, sl_id, entry_id, signal, entry_price, monitor_done),
                    daemon=True
                )
                monitor_thread.start()

                # Wait until monitor signals done or timeout
                MONITOR_TIMEOUT = 60 * 60
                monitor_done.wait(timeout=MONITOR_TIMEOUT)

                # if still not finished, we just continue to next loop (monitor thread may still log later)
                if not monitor_done.is_set():
                    print(f"[{now_str()}] Monitor did not finish within timeout; continuing main loop.", flush=True)

            except Exception as e:
                print(f"[{now_str()}] Error: {e}", flush=True)
                log.error(traceback.format_exc())
                time.sleep(5)
    except KeyboardInterrupt:
        print("Bot stopped by user.", flush=True)

if __name__ == '__main__':
    main_loop()
