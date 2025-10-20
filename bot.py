# live_binance_futures_bot_ready.py
import ccxt
import pandas as pd
import time, traceback, logging
from datetime import datetime, timedelta

# ------------- CONFIG -------------
API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"
SYMBOL = "BNB/USDT"        # unified pair format
TIMEFRAME = "1m"
EMA_SET = [10,20,50,100]
LOT_SIZE = 0.01
TP_POINTS = 6.0
SL_POINTS = 3.0
LEVERAGE = 75
COOLDOWN_MINUTES = 30
POLL_INTERVAL = 5
# ----------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
    'timeout': 30000,
})

def now(): return datetime.utcnow() + timedelta(hours=5, minutes=30)

def safe_sleep(s): 
    try: time.sleep(s)
    except KeyboardInterrupt: raise

# ----- helpers for leverage (robust) -----
def set_leverage(symbol, leverage):
    try:
        # try unified helper first (may exist)
        if hasattr(exchange, 'set_leverage'):
            exchange.set_leverage(leverage, symbol)
        else:
            # fallback to raw fapi endpoint (Binance USDM)
            exchange.fapiPrivate_post_leverage({'symbol': symbol.replace('/',''), 'leverage': int(leverage)})
        logging.info("Leverage set %sx for %s", leverage, symbol)
    except Exception as e:
        logging.warning("set_leverage failed: %s", e)

# ----- fetch candles & EMA -----
def fetch_ohlcv_df(symbol, timeframe, limit=200):
    bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(bars, columns=["time","open","high","low","close","volume"])
    df['time'] = pd.to_datetime(df['time'], unit='ms') + timedelta(hours=5, minutes=30)
    return df

def get_signal(df):
    for s in EMA_SET:
        df[f"ema{s}"] = df['close'].ewm(span=s, adjust=False).mean()
    if len(df) < max(EMA_SET)+2: 
        return None
    r = df.iloc[-1]
    c,h,l = r['close'], r['high'], r['low']
    emas = [r[f"ema{e}"] for e in EMA_SET]
    if all(c > e for e in emas): return "BUY"
    if all(c < e for e in emas): return "SELL"
    if l <= emas[len(emas)//2] <= h:
        return "BUY" if c > emas[len(emas)//2] else "SELL" if c < emas[len(emas)//2] else None
    return None

# ----- place market entry (unified) -----
def place_market(symbol, side, qty):
    # ccxt signature: create_order(symbol, type, side, amount, price=None, params={})
    try:
        return exchange.create_order(symbol, 'MARKET', side, qty)
    except Exception as e:
        logging.error("Market order failed: %s", e)
        raise

# ----- place TP/SL (reduceOnly) with fallback -----
def place_tp_sl(symbol, side, qty, entry_price, tp_points, sl_points):
    tp_price = entry_price + tp_points if side == 'BUY' else entry_price - tp_points
    sl_price = entry_price - sl_points if side == 'BUY' else entry_price + sl_points
    tp_side = 'SELL' if side=='BUY' else 'BUY'
    sl_side = tp_side
    # try via ccxt create_order with type=TAKE_PROFIT_MARKET / STOP_MARKET
    params_tp = {'stopPrice': float(tp_price), 'reduceOnly': True}
    params_sl = {'stopPrice': float(sl_price), 'reduceOnly': True}
    try:
        o1 = exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', tp_side, qty, None, params_tp)
        o2 = exchange.create_order(symbol, 'STOP_MARKET', sl_side, qty, None, params_sl)
        logging.info("Placed TP and SL via unified create_order")
        return o1,o2
    except Exception as e:
        logging.warning("Unified TP/SL failed: %s - trying raw endpoints", e)
        # Raw POST to fapi/v1/order
        try:
            s = symbol.replace('/','')
            # TAKE_PROFIT_MARKET
            tp_payload = {'symbol': s, 'side': tp_side, 'type': 'TAKE_PROFIT_MARKET', 'quantity': qty, 'stopPrice': tp_price, 'reduceOnly': 'true', 'timestamp': exchange.milliseconds()}
            sl_payload = {'symbol': s, 'side': sl_side, 'type': 'STOP_MARKET', 'quantity': qty, 'stopPrice': sl_price, 'reduceOnly': 'true', 'timestamp': exchange.milliseconds()}
            o1 = exchange.fapiPrivate_post_order(tp_payload)
            o2 = exchange.fapiPrivate_post_order(sl_payload)
            logging.info("Placed TP/SL via raw fapi endpoints")
            return o1,o2
        except Exception as e2:
            logging.error("Raw TP/SL placement failed: %s", e2)
            raise

# ----- robust position check (multi-shape) -----
def is_position_open(symbol):
    # try fetch_positions unified
    try:
        if hasattr(exchange, 'fetch_positions'):
            positions = exchange.fetch_positions([symbol])
            for p in positions:
                # p may be dict with 'symbol' or 'info' sub-dict
                s = p.get('symbol') or (p.get('info') or {}).get('symbol')
                contracts = p.get('contracts') or float((p.get('info') or {}).get('positionAmt') or 0)
                if s and s.replace('/','') == symbol.replace('/','') and abs(float(contracts)) > 0:
                    return True
        # fallback: fetch_balance info positions
        bal = exchange.fetch_balance(params={})
        info = bal.get('info', {})
        for pos in info.get('positions', []):
            if pos.get('symbol') == symbol.replace('/','') and float(pos.get('positionAmt',0)) != 0:
                return True
    except Exception as e:
        logging.debug("position check error: %s", e)
    return False

# ----- main loop -----
def run():
    set_leverage(SYMBOL, LEVERAGE)
    logging.info("Bot started")
    in_position = False
    cooldown_until = None

    while True:
        try:
            if cooldown_until and datetime.utcnow() + timedelta(hours=5,minutes=30) < cooldown_until:
                logging.info("Cooldown active until %s", cooldown_until); safe_sleep(POLL_INTERVAL); continue

            # ensure no open position already (external changes)
            if is_position_open(SYMBOL):
                logging.info("Detected existing live position -> waiting until closed")
                safe_sleep(10); continue

            df = fetch_ohlcv_df(SYMBOL, TIMEFRAME)
            if df.empty:
                safe_sleep(POLL_INTERVAL); continue
            signal = get_signal(df)
            if not signal:
                safe_sleep(POLL_INTERVAL); continue

            ticker = exchange.fetch_ticker(SYMBOL)
            price = float(ticker.get('last') or ticker.get('close'))
            logging.info("Signal %s @ price %s", signal, price)

            # market entry
            place_market(SYMBOL, signal, LOT_SIZE)
            # place TP/SL
            try:
                place_tp_sl(SYMBOL, signal, LOT_SIZE, price, TP_POINTS, SL_POINTS)
            except Exception:
                # If TP/SL placement failed, try to close immediately to avoid unprotected position
                logging.error("TP/SL placement failed, trying to close position immediately")
                close_side = 'SELL' if signal=='BUY' else 'BUY'
                try:
                    exchange.create_order(SYMBOL, 'MARKET', close_side, LOT_SIZE)
                except Exception as e:
                    logging.error("Forced close failed: %s", e)
            # wait until closed
            while is_position_open(SYMBOL):
                safe_sleep(5)
            # set cooldown (apply always; you can refine to check whether it was SL specifically)
            cooldown_until = datetime.utcnow() + timedelta(hours=5,minutes=30) + timedelta(minutes=COOLDOWN_MINUTES)
            logging.info("Position closed; cooldown until %s", cooldown_until)

        except KeyboardInterrupt:
            logging.info("Stopped by user"); break
        except Exception as e:
            logging.error("Loop error: %s\n%s", e, traceback.format_exc()); safe_sleep(5)

if __name__ == "__main__":
    run()
