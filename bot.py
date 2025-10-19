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

# =================== CONFIG ===================
API_KEY = 'czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ'        # <-- put your API key
API_SECRET = 'cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO'  # <-- put your secret
SYMBOL = 'BNB/USDT'
TIMEFRAME = '1m'
LOT_SIZE = 0.01              # quantity in BNB (adjust as needed)
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 75                # requested leverage (best-effort)
POLL_INTERVAL_SECONDS = 5
CSV_FN = f'{SYMBOL.replace("/", "-")}_live_trades.csv'
LOG_FILE = 'bot_live.log'

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
    'options': {
        'defaultType': 'future'   # ensure futures endpoints
    }
})

# try to load markets once
try:
    exchange.load_markets()
except Exception as e:
    print(f"[WARN] load_markets failed: {e}", flush=True)
    log.warning(f"load_markets failed: {e}")

# =================== STATE ===================
last_processed_candle_time = None

# =================== TIME ===================
KOLKATA = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)
def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== HELPERS ===================
def fetch_latest_candles(symbol, timeframe, limit=200):
    try:
        # ccxt: fetch_ohlcv(symbol, timeframe, since=None, limit=None)
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 10:
            return None
        df = pd.DataFrame(bars, columns=['time','open','high','low','close','volume'])
        # bars time in ms UTC
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
    # candle is a pandas Series with ema columns
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

    # simple rules same as original with explicit ordering
    if c >= ema5 and c >= ema9 and c >= ema15 and c > ema21:
        return 'BUY'
    if c <= ema5 and c <= ema9 and c <= ema15 and c < ema21:
        return 'SELL'
    # price touched ema15 during candle and closed above/below ema5 to decide
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
    """
    Fetch futures USDT wallet balance (best-effort).
    """
    try:
        bal = exchange.fetch_balance()
        # try common shapes
        totals = bal.get('total', {})
        if isinstance(totals, dict):
            for k,v in totals.items():
                if str(k).upper() == 'USDT':
                    return float(v or 0.0)
        # fallback to info
        info = bal.get('info') or {}
        # Binance futures often returns 'totalWalletBalance' or assets
        if isinstance(info, dict):
            if 'totalWalletBalance' in info:
                return float(info.get('totalWalletBalance') or 0.0)
            # look for assets array
            assets = info.get('assets') or info.get('positions') or []
            if isinstance(assets, list):
                for a in assets:
                    if a.get('asset') == 'USDT':
                        # may have 'walletBalance' or 'balance'
                        return float(a.get('walletBalance') or a.get('balance') or 0.0)
        # last resort: zero
        return 0.0
    except Exception as e:
        log.error(f'fetch_balance error: {e}')
        print(f"[{now_str()}] fetch_balance error: {e}", flush=True)
        return 0.0

def set_leverage(symbol, leverage):
    """
    Try to set leverage via Binance futures endpoint (best-effort).
    """
    try:
        symbol_no_slash = symbol.replace('/', '')
        # many ccxt versions expose fapiPrivate_post_leverage
        if hasattr(exchange, 'fapiPrivate_post_leverage'):
            resp = exchange.fapiPrivate_post_leverage({'symbol': symbol_no_slash, 'leverage': int(leverage)})
            log.info(f'Leverage set response: {resp}')
            print(f"[{now_str()}] Leverage set to {leverage}", flush=True)
        else:
            # fallback: use exchange.private_post... or simply skip
            print(f"[{now_str()}] Leverage set API not available in this ccxt build; skipping.", flush=True)
    except Exception as e:
        log.error(f'Could not set leverage: {e}')
        print(f"[{now_str()}] Warning: Could not set leverage via API: {e}", flush=True)

def place_market_entry(side, amount):
    """
    Places a market order for entry and returns order info.
    side: 'BUY' or 'SELL'
    """
    try:
        side_str = 'buy' if side == 'BUY' else 'sell'
        # For futures, ensure params reduceOnly is not set for entry
        order = exchange.create_order(symbol=SYMBOL, type='market', side=side_str, amount=amount, params={})
        return order
    except Exception as e:
        log.error(f'Entry order failed: {e}')
        print(f"[{now_str()}] Entry order failed: {e}", flush=True)
        return None

def place_tp_sl_orders(side, amount, tp_price, sl_price):
    """
    Place TP (LIMIT reduceOnly) and SL (STOP_MARKET reduceOnly) for Binance Futures.
    Returns dict with tp_order and sl_order (or None).
    Notes: ccxt + Binance naming varies by version — this function tries common variations.
    """
    tp_order = None
    sl_order = None
    try:
        if side == 'BUY':
            tp_side = 'sell'
            sl_side = 'sell'
        else:
            tp_side = 'buy'
            sl_side = 'buy'

        # Try to place TP limit reduceOnly
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
            log.warning(f'TP order creation primary method failed: {e}')
            # some ccxt versions require 'positionSide' or 'reduceOnly' in different shapes — skip or attempt alternative
            try:
                tp_order = exchange.create_order(
                    symbol=SYMBOL,
                    type='limit',
                    side=tp_side,
                    amount=amount,
                    price=tp_price,
                    params={'reduceOnly': 'true', 'timeInForce': 'GTC'}
                )
            except Exception as e2:
                log.error(f'TP creation fallback failed: {e2}')
                tp_order = None

        # Place stop-loss stop-market reduceOnly — different ccxt wrappers expect different params names:
        try:
            # common approach: type 'STOP_MARKET' with 'stopPrice' param
            sl_order = exchange.create_order(
                symbol=SYMBOL,
                type='STOP_MARKET',
                side=sl_side,
                amount=amount,
                params={'stopPrice': sl_price, 'reduceOnly': True}
            )
        except Exception as e:
            log.warning(f'SL order creation primary method failed: {e}')
            # fallback attempt: use 'stop' param or 'stopPrice' capitalized
            try:
                sl_order = exchange.create_order(
                    symbol=SYMBOL,
                    type='stop_market',
                    side=sl_side,
                    amount=amount,
                    params={'stopPrice': sl_price, 'reduceOnly': True}
                )
            except Exception as e2:
                log.error(f'SL creation fallback failed: {e2}')
                sl_order = None

        return {'tp_order': tp_order, 'sl_order': sl_order}
    except Exception as e:
        log.error(f'place_tp_sl_orders error: {e}')
        print(f"[{now_str()}] place_tp_sl_orders error: {e}", flush=True)
        return {'tp_order': tp_order, 'sl_order': sl_order}

def fetch_open_orders_for_symbol():
    try:
        orders = exchange.fetch_open_orders(symbol=SYMBOL)
        return orders
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
    """
    Try to return current position amount for the symbol on futures (positive long, negative short, or 0).
    Uses multiple ccxt endpoints if available.
    """
    try:
        # Try fetch_positions (some ccxt versions)
        if hasattr(exchange, 'fetch_positions'):
            try:
                positions = exchange.fetch_positions([SYMBOL])
                for p in positions:
                    # different shapes: 'contracts', 'size', 'amount', 'positionAmt'
                    if p.get('symbol') == SYMBOL or p.get('info', {}).get('symbol') == SYMBOL.replace('/', ''):
                        size = float(p.get('contracts', 0) or p.get('size', 0) or p.get('amount', 0) or p.get('positionAmt', 0) or 0)
                        return size
            except Exception:
                pass

        # fallback: Binance position risk endpoint
        try:
            sym = SYMBOL.replace('/','')
            resp = exchange.fapiPrivate_get_positionrisk({'symbol': sym})
            if isinstance(resp, list):
                for r in resp:
                    if r.get('symbol') == sym:
                        amt = float(r.get('positionAmt', 0) or 0)
                        return amt
        except Exception:
            pass

        return 0.0
    except Exception as e:
        log.error(f'get_position_size error: {e}')
        return 0.0

def parse_order_id(order):
    # Some ccxt shapes: order['id'] or order.get('info', {}).get('orderId')
    if not order:
        return None
    if isinstance(order, dict):
        if order.get('id'):
            return order.get('id')
        info = order.get('info') or {}
        return info.get('orderId') or info.get('order_id') or info.get('id')
    return None

# =================== STARTUP ===================
def main_loop():
    global last_processed_candle_time

    print(f"[{now_str()}] Starting LIVE EMA Futures Bot ({SYMBOL}) | Leverage={LEVERAGE}", flush=True)
    log.info("Starting live bot")

    # Try to set leverage (best-effort)
    set_leverage(SYMBOL, LEVERAGE)

    try:
        while True:
            try:
                df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
                if df is None or len(df) < 12:
                    print(f"[{now_str()}] Not enough candles yet ➡ sleeping...", flush=True)
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                df = compute_emas(df)

                # detect existing position or open orders
                pos_size = get_position_size_for_symbol()
                open_orders = fetch_open_orders_for_symbol()
                any_open_orders = len(open_orders) > 0
                currently_in_position = (abs(float(pos_size)) > 0) or any_open_orders

                # use last fully closed candle for signal
                last_closed = df.iloc[-2]
                live_candle = df.iloc[-1]
                next_open = float(live_candle['open'])
                last_closed_time_iso = str(last_closed['time'].isoformat())

                if currently_in_position:
                    print(f"[{now_str()}] Detected existing position/orders on exchange; skipping new entry.", flush=True)
                    log.info("Existing position/orders detected; skipping entry.")
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                signal = check_signal(last_closed)

                if signal:
                    if last_processed_candle_time == last_closed_time_iso:
                        print(f"[{now_str()}] Skipping entry: last_closed {last_closed_time_iso} already processed.", flush=True)
                        time.sleep(POLL_INTERVAL_SECONDS)
                        continue

                    usdt_bal = get_usdt_balance()
                    print(f"[{now_str()}] Account USDT balance (futures): {usdt_bal}", flush=True)

                    # Place market entry
                    entry_order = place_market_entry(signal, LOT_SIZE)
                    if entry_order is None:
                        print(f"[{now_str()}] Entry order failed, skipping.", flush=True)
                        time.sleep(POLL_INTERVAL_SECONDS)
                        continue

                    # parse entry price
                    entry_price = None
                    try:
                        if isinstance(entry_order, dict):
                            if entry_order.get('average'):
                                entry_price = float(entry_order['average'])
                            elif entry_order.get('price'):
                                entry_price = float(entry_order['price'])
                            else:
                                # sometimes info contains fills
                                info = entry_order.get('info') or {}
                                fills = info.get('fills') or info.get('fillQty') or []
                                if isinstance(fills, list) and len(fills) > 0:
                                    # try to pick avg price from fills
                                    prices = [float(f.get('price') or f.get('fillPrice') or 0) for f in fills]
                                    entry_price = sum(prices)/len(prices) if prices else None
                    except Exception:
                        entry_price = None

                    if entry_price is None:
                        entry_price = float(next_open)

                    # compute absolute TP/SL
                    if signal == "BUY":
                        tp_price = entry_price + TP_POINTS
                        sl_price = entry_price - SL_POINTS
                    else:
                        tp_price = entry_price - TP_POINTS
                        sl_price = entry_price + SL_POINTS

                    placement = place_tp_sl_orders(signal, LOT_SIZE, tp_price, sl_price)
                    tp_order = placement.get('tp_order')
                    sl_order = placement.get('sl_order')

                    last_processed_candle_time = last_closed_time_iso
                    print(f"[{now_str()}] Entry placed @ {entry_price} | TP={tp_price} SL={sl_price}", flush=True)
                    log.info(f'Entry placed {signal}@{entry_price} TP={tp_price} SL={sl_price}')

                    # track order ids
                    tp_id = parse_order_id(tp_order) if tp_order else None
                    sl_id = parse_order_id(sl_order) if sl_order else None
                    entry_id = parse_order_id(entry_order)

                    # Monitor until TP or SL
                    tp_filled = False
                    sl_filled = False

                    # safety timeout for monitoring (seconds) to avoid infinite loop — adjust as needed
                    monitor_start = time.time()
                    MONITOR_TIMEOUT = 60 * 60  # 1 hour default

                    while True:
                        time.sleep(POLL_INTERVAL_SECONDS)
                        # break on timeout
                        if time.time() - monitor_start > MONITOR_TIMEOUT:
                            print(f"[{now_str()}] Monitor timeout reached. Breaking monitor loop.", flush=True)
                            log.info("Monitor timeout reached for trade.")
                            break

                        # refresh open orders & position
                        open_orders = fetch_open_orders_for_symbol()
                        open_ids = set()
                        for o in open_orders:
                            oid = o.get('id') or (o.get('info') or {}).get('orderId')
                            if oid:
                                open_ids.add(str(oid))

                        # if tp_id gone from open_ids -> possibly filled
                        try:
                            if tp_id and str(tp_id) not in open_ids:
                                # check closed orders or trades to confirm
                                try:
                                    closed = exchange.fetch_closed_orders(symbol=SYMBOL, since=None, limit=100)
                                except Exception:
                                    closed = []
                                for co in closed:
                                    coid = co.get('id') or (co.get('info') or {}).get('orderId')
                                    if coid and str(coid) == str(tp_id):
                                        st = co.get('status') or (co.get('info') or {}).get('status')
                                        if str(st).lower() in ('closed', 'filled', 'filled()'):
                                            tp_filled = True
                                            break
                        except Exception:
                            pass

                        try:
                            if sl_id and str(sl_id) not in open_ids:
                                try:
                                    closed = exchange.fetch_closed_orders(symbol=SYMBOL, since=None, limit=100)
                                except Exception:
                                    closed = []
                                for co in closed:
                                    coid = co.get('id') or (co.get('info') or {}).get('orderId')
                                    if coid and str(coid) == str(sl_id):
                                        st = co.get('status') or (co.get('info') or {}).get('status')
                                        if str(st).lower() in ('closed', 'filled', 'filled()'):
                                            sl_filled = True
                                            break
                        except Exception:
                            pass

                        # Alternative position check: if position size zero => closed
                        try:
                            current_pos = get_position_size_for_symbol()
                            if abs(float(current_pos)) == 0:
                                # position closed
                                # check which order is still present
                                remaining = fetch_open_orders_for_symbol()
                                remaining_ids = [o.get('id') or (o.get('info') or {}).get('orderId') for o in remaining]
                                if tp_id and str(tp_id) not in [str(x) for x in remaining_ids]:
                                    tp_filled = True
                                if sl_id and str(sl_id) not in [str(x) for x in remaining_ids]:
                                    sl_filled = True
                        except Exception:
                            pass

                        if tp_filled or sl_filled:
                            outcome = 'TP' if tp_filled else 'SL'
                            # cancel the other one if still open
                            try:
                                if tp_filled and sl_id:
                                    cancel_order_by_id(sl_id)
                                if sl_filled and tp_id:
                                    cancel_order_by_id(tp_id)
                            except Exception as e:
                                log.error(f'Error cancelling other order: {e}')

                            # attempt to get exit price from trades
                            exit_price = None
                            try:
                                trades = exchange.fetch_my_trades(symbol=SYMBOL, since=None, limit=200)
                                for t in reversed(trades):
                                    oid = t.get('order') or t.get('orderId') or (t.get('info') or {}).get('orderId') or (t.get('info') or {}).get('orderId')
                                    if oid and ((tp_filled and str(oid) == str(tp_id)) or (sl_filled and str(oid) == str(sl_id))):
                                        p = t.get('price') or (t.get('info') or {}).get('price') or None
                                        if p:
                                            exit_price = float(p)
                                            break
                            except Exception:
                                pass

                            if exit_price is None:
                                # fallback to latest closed candle
                                try:
                                    latest = fetch_latest_candles(SYMBOL, TIMEFRAME, 2)
                                    if latest is not None and len(latest) >= 2:
                                        exit_price = float(latest.iloc[-2]['close'])
                                except Exception:
                                    exit_price = None

                            pnl = None
                            if exit_price is not None:
                                if signal == 'BUY':
                                    pnl = (exit_price - entry_price) * LOT_SIZE
                                else:
                                    pnl = (entry_price - exit_price) * LOT_SIZE

                            rec = {
                                'time': now_ist().isoformat(),
                                'dir': signal,
                                'entry': round(entry_price, 6),
                                'exit': round(exit_price, 6) if exit_price else None,
                                'outcome': outcome,
                                'pnl': round(pnl, 6) if pnl is not None else None,
                                'balance': get_usdt_balance(),
                                'entry_order_id': entry_id,
                                'tp_order_id': tp_id,
                                'sl_order_id': sl_id
                            }
                            append_trade_csv(rec)
                            print(f"[{now_str()}] {outcome} hit. Entry {entry_price} Exit {exit_price} PnL {pnl}", flush=True)
                            log.info(f"Trade closed: {rec}")
                            break

                        # otherwise continue monitoring
                    # end monitor loop

                else:
                    print(f"[{now_str()}] No valid signal for last closed candle.", flush=True)

                time.sleep(POLL_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                print("\n[INFO] KeyboardInterrupt received. Exiting gracefully...", flush=True)
                log.info("KeyboardInterrupt received. Bot stopped by user.")
                sys.exit(0)
            except Exception as e:
                print(f"[{now_str()}] Error in main loop: {e}", flush=True)
                log.error(f"Error in main loop: {e}\n{traceback.format_exc()}")
                time.sleep(5)
    except Exception as e:
        log.error(f'Fatal: {e}')
        print(f"[{now_str()}] Fatal error: {e}", flush=True)

if __name__ == '__main__':
    main_loop()
