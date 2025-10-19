# live_ema_binance_futures.py
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
LOT_SIZE = 0.01              # quantity in BNB (keep same as before)
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 75                # requested leverage
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
# Using Binance USDT-M futures via ccxt. options.defaultType='future' makes fetch_balance and order endpoints use futures.
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'
    }
})

# =================== STATE ===================
in_position = False
position = None
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
    with open(CSV_FN, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)

def get_usdt_balance():
    """
    Fetches futures USDT balance (total or available).
    """
    try:
        bal = exchange.fetch_balance()
        # depending on ccxt version/shape, futures balance often in 'total' under 'USDT'
        if 'USDT' in bal.get('total', {}):
            return float(bal['total']['USDT'])
        # fallback: try 'info' raw response
        info = bal.get('info', {})
        # try multiple common places - this might vary by ccxt version
        if isinstance(info, dict) and 'totalWalletBalance' in info:
            return float(info['totalWalletBalance'])
        # fallback: sum totals
        totals = bal.get('total', {})
        for k,v in totals.items():
            if k.upper() == 'USDT':
                return float(v)
        # if nothing found, return 0
        return 0.0
    except Exception as e:
        log.error(f'fetch_balance error: {e}')
        print(f"[{now_str()}] fetch_balance error: {e}", flush=True)
        return 0.0

def set_leverage(symbol, leverage):
    """
    Set leverage for the symbol (Futures). Using CCXT's generic POST to Binance futures leverage endpoint.
    """
    try:
        symbol_no_slash = symbol.replace('/', '')
        resp = exchange.fapiPrivate_post_leverage({'symbol': symbol_no_slash, 'leverage': int(leverage)})
        log.info(f'Leverage set response: {resp}')
        print(f"[{now_str()}] Leverage set to {leverage}", flush=True)
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
        order = exchange.create_order(symbol=SYMBOL, type='market', side=side_str, amount=amount)
        return order
    except Exception as e:
        log.error(f'Entry order failed: {e}')
        print(f"[{now_str()}] Entry order failed: {e}", flush=True)
        return None

def place_tp_sl_orders(side, amount, tp_price, sl_price):
    """
    Place TP (LIMIT reduceOnly) and SL (STOP_MARKET reduceOnly) for Binance Futures.
    Returns dict with tp_order and sl_order objects (or None).
    """
    try:
        tp_order = None
        sl_order = None
        # For BUY entry: TP should be SELL limit at tp_price (take profit). SL should be SELL stop-market at sl_price.
        # For SELL entry: reverse sides.
        if side == 'BUY':
            tp_side = 'sell'
            sl_side = 'sell'
        else:
            tp_side = 'buy'
            sl_side = 'buy'

        # Place take-profit limit (reduceOnly)
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
            log.error(f'TP order creation failed: {e}')
            print(f"[{now_str()}] TP order creation failed: {e}", flush=True)

        # Place stop-loss stop-market (reduceOnly)
        try:
            # Many ccxt wrappers use type='STOP_MARKET' with stopPrice param; adjust if your ccxt requires different naming.
            sl_order = exchange.create_order(
                symbol=SYMBOL,
                type='STOP_MARKET',
                side=sl_side,
                amount=amount,
                params={'stopPrice': sl_price, 'reduceOnly': True}
            )
        except Exception as e:
            log.error(f'SL order creation failed: {e}')
            print(f"[{now_str()}] SL order creation failed: {e}", flush=True)

        return {'tp_order': tp_order, 'sl_order': sl_order}
    except Exception as e:
        log.error(f'place_tp_sl_orders error: {e}')
        print(f"[{now_str()}] place_tp_sl_orders error: {e}", flush=True)
        return {'tp_order': None, 'sl_order': None}

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
    Attempt to infer current position size (positive for long, negative for short).
    Uses CCXT's fetch_positions if available, else inspects balance/info.
    """
    try:
        if hasattr(exchange, 'fetch_positions'):
            positions = exchange.fetch_positions([SYMBOL])
            for p in positions:
                if p.get('symbol') == SYMBOL:
                    size = float(p.get('contracts', 0) or p.get('size', 0) or 0)
                    return size
        # Fallback: try fetching positions risk endpoint
        try:
            sym = SYMBOL.replace('/','')
            resp = exchange.fapiPrivate_get_positionrisk({'symbol': sym})
            if isinstance(resp, list):
                for r in resp:
                    if r.get('symbol') == sym:
                        amt = float(r.get('positionAmt', 0))
                        return amt
        except Exception:
            pass
        return 0.0
    except Exception as e:
        log.error(f'get_position_size error: {e}')
        return 0.0

# =================== STARTUP ===================
print(f"[{now_str()}] Starting LIVE EMA Futures Bot ({SYMBOL}) | Leverage={LEVERAGE}", flush=True)
log.info("Starting live bot")

# Set leverage once at start (best-effort)
set_leverage(SYMBOL, LEVERAGE)

# =================== MAIN LOOP ===================
try:
    while True:
        try:
            df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
            if df is None or len(df) < 12:
                print(f"[{now_str()}] Not enough candles yet ➡ sleeping...", flush=True)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            df = compute_emas(df)

            # Quick check: ensure exactly one trade at a time
            # Check existing open positions (best-effort)
            pos_size = get_position_size_for_symbol()
            open_orders = fetch_open_orders_for_symbol()
            any_open_orders = len(open_orders) > 0
            currently_in_position = (abs(pos_size) > 0) or any_open_orders

            # Use last fully closed candle to generate signal
            last_closed = df.iloc[-2]
            live_candle = df.iloc[-1]
            next_open = float(live_candle['open'])
            last_closed_time_iso = str(last_closed['time'].isoformat())

            # If there's a live position detected by exchange, reflect it in state
            if currently_in_position:
                print(f"[{now_str()}] Detected existing position/orders on exchange; skipping new entry.", flush=True)
                log.info("Existing position/orders detected; skipping entry.")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Generate signal as before
            signal = check_signal(last_closed)

            if signal:
                # Ensure we don't open multiple entries for same candle
                if last_processed_candle_time == last_closed_time_iso:
                    print(f"[{now_str()}] Skipping entry: last_closed {last_closed_time_iso} already processed.", flush=True)
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # Use real account balance to decide (we keep same LOT_SIZE but you can implement sizing)
                usdt_bal = get_usdt_balance()
                print(f"[{now_str()}] Account USDT balance (futures): {usdt_bal}", flush=True)

                # Place market entry order
                entry_price = None
                entry_order = place_market_entry(signal, LOT_SIZE)
                if entry_order is None:
                    print(f"[{now_str()}] Entry order failed, skipping.", flush=True)
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # Parse fill price if available
                try:
                    if 'average' in entry_order and entry_order['average']:
                        entry_price = float(entry_order['average'])
                    elif 'price' in entry_order and entry_order['price']:
                        entry_price = float(entry_order['price'])
                    else:
                        # fallback: use next_open from candles as approximate entry
                        entry_price = float(next_open)
                except Exception:
                    entry_price = float(next_open)

                # Compute TP/SL absolute prices (points)
                if signal == "BUY":
                    tp_price = entry_price + TP_POINTS
                    sl_price = entry_price - SL_POINTS
                else:
                    tp_price = entry_price - TP_POINTS
                    sl_price = entry_price + SL_POINTS

                # Place TP and SL orders (reduceOnly)
                placement = place_tp_sl_orders(signal, LOT_SIZE, tp_price, sl_price)
                tp_order = placement.get('tp_order')
                sl_order = placement.get('sl_order')

                # Save state and mark processed candle
                last_processed_candle_time = last_closed_time_iso
                print(f"[{now_str()}] Entry placed @ {entry_price} | TP={tp_price} SL={sl_price}", flush=True)
                log.info(f'Entry placed {signal}@{entry_price} TP={tp_price} SL={sl_price}')

                # Monitor until either TP or SL fills
                entry_filled = True  # already executed by market
                tp_filled = False
                sl_filled = False
                tp_id = tp_order.get('id') if tp_order else None
                sl_id = sl_order.get('id') if sl_order else None
                entry_id = entry_order.get('id') if entry_order else None

                # Polling to detect fills
                while True:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    # Check order statuses
                    orders = fetch_open_orders_for_symbol()
                    order_ids = {o.get('id'): o for o in orders}
                    # If TP id not in open orders => it might be filled or canceled
                    try:
                        if tp_id:
                            if tp_id not in order_ids:
                                # verify if it's filled by checking user trades / closed orders
                                # fetch closed orders to inspect
                                try:
                                    closed = exchange.fetch_closed_orders(symbol=SYMBOL, since=None, limit=50)
                                except Exception:
                                    closed = []
                                for co in closed:
                                    if co.get('id') == tp_id and co.get('status') in ('closed','canceled','filled'):
                                        # Depending on ccxt, status or info needs checking
                                        status = co.get('status')
                                        if status == 'closed' or status == 'filled':
                                            tp_filled = True
                                            break
                        if sl_id:
                            if sl_id not in order_ids:
                                try:
                                    closed = exchange.fetch_closed_orders(symbol=SYMBOL, since=None, limit=50)
                                except Exception:
                                    closed = []
                                for co in closed:
                                    if co.get('id') == sl_id and co.get('status') in ('closed','canceled','filled'):
                                        status = co.get('status')
                                        if status == 'closed' or status == 'filled':
                                            sl_filled = True
                                            break
                    except Exception:
                        pass

                    # Alternative quick check: fetch position size to see if position is gone
                    current_pos = get_position_size_for_symbol()
                    if abs(current_pos) == 0:
                        # position closed → one of TP/SL likely filled
                        # determine which filled by checking which order still exists
                        remaining_orders = fetch_open_orders_for_symbol()
                        remaining_ids = [o.get('id') for o in remaining_orders]
                        if tp_id and tp_id not in remaining_ids:
                            tp_filled = True
                        if sl_id and sl_id not in remaining_ids:
                            sl_filled = True

                    if tp_filled or sl_filled:
                        outcome = 'TP' if tp_filled else 'SL'
                        # Cancel the other order if still open
                        try:
                            if tp_filled and sl_id:
                                cancel_order_by_id(sl_id)
                            if sl_filled and tp_id:
                                cancel_order_by_id(tp_id)
                        except Exception as e:
                            log.error(f'Cancel other order error: {e}')
                        # Get exit price (best-effort)
                        exit_price = None
                        # Try to read fill price from closed trades
                        try:
                            trades = exchange.fetch_my_trades(symbol=SYMBOL, since=None, limit=50)
                            for t in reversed(trades):
                                # find trade that corresponds to TP/SL by matching order id
                                if tp_filled and t.get('order') == tp_id:
                                    exit_price = float(t.get('price') or t.get('cost') / max(1e-9, float(t.get('amount',1))))
                                    break
                                if sl_filled and t.get('order') == sl_id:
                                    exit_price = float(t.get('price') or t.get('cost') / max(1e-9, float(t.get('amount',1))))
                                    break
                        except Exception:
                            pass
                        # fallback: fetch last candle close as exit approximation
                        if exit_price is None:
                            try:
                                latest = fetch_latest_candles(SYMBOL, TIMEFRAME, 2)
                                if latest is not None and len(latest) >= 2:
                                    exit_price = float(latest.iloc[-2]['close'])
                            except Exception:
                                exit_price = None

                        # PnL calculation
                        pnl = None
                        if exit_price is not None:
                            if signal == 'BUY':
                                pnl = (exit_price - entry_price) * LOT_SIZE
                            else:
                                pnl = (entry_price - exit_price) * LOT_SIZE

                        # Update CSV and logs
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
                       
