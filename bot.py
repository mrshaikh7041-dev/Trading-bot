import ccxt
import pandas as pd
import math
import time
import os
from datetime import datetime, timedelta

# ---------------- CONFIG ----------------
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not API_KEY or not API_SECRET:
    raise SystemExit("❌ API_KEY / API_SECRET missing! Set them in environment variables.")

SYMBOL = "ETH/USDT"
TIMEFRAME = "1m"
LOT_SIZE = 0.02
TP_POINTS = 40
SL_POINTS = 20
LEVERAGE = 100
COOLDOWN_MINUTES = 30
EMA_FAST = 13
EMA_SLOW = 55
SLOPE_WINDOW = 3
SLOPE_DEG = 20
POLL_SLEEP = 1
MAX_RETRIES = 5

# ---------------- EXCHANGE ----------------
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "future",
        "adjustForTimeDifference": True
    }
})

# ---------------- HELPERS ----------------
def nowstr():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(*args):
    print(nowstr(), *args, flush=True)

def fetch_candles(symbol, tf=TIMEFRAME, limit=50):
    for attempt in range(MAX_RETRIES):
        try:
            ohlc = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","volume"])
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            return df
        except Exception as e:
            log(f"⚠️ Fetch candle error: {e}, retry {attempt+1}/{MAX_RETRIES}")
            time.sleep(2 ** attempt)
    raise Exception("Failed to fetch candles after retries")

def get_open_position(symbol):
    positions = exchange.fetch_positions([symbol])
    for p in positions:
        if float(p['contracts']) > 0:
            return p
    return None

def is_strong_bullish(o,h,l,c):
    body = c - o
    rng = h-l if h-l!=0 else 1e-9
    upper_wick = h - c
    lower_wick = o - l
    return (body>0 and (body/rng)>=0.55) or (body>0 and lower_wick>=2*abs(body) and upper_wick<=abs(body)*0.5) or (body>0 and (c-l)/rng>=0.75)

def is_strong_bearish(o,h,l,c):
    body = c - o
    rng = h-l if h-l!=0 else 1e-9
    upper_wick = h - o
    lower_wick = c - l
    return (body<0 and abs(body)/rng>=0.55) or (body<0 and upper_wick>=2*abs(body) and lower_wick<=abs(body)*0.5) or (body<0 and (h-c)/rng>=0.75)

def ema_slope(df, ema_col, window):
    if len(df) < window + 1:
        return 0
    delta_y = df[ema_col].iloc[-1] - df[ema_col].iloc[-window-1]
    slope_deg = abs(math.degrees(math.atan(delta_y / window)))
    return slope_deg

# -------- LIVE BOT --------
def run_live_bot():
    cooldown_until = None
    last_trade_candle = None

    log(f"🚀 Starting LIVE BOT | Lot: {LOT_SIZE} | Leverage: {LEVERAGE}x")

    while True:
        try:
            df = fetch_candles(SYMBOL, TIMEFRAME, limit=50)
            df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

            last_candle_time = df.index[-1]
            o,h,l,c = df.iloc[-1][['open','high','low','close']]

            # Skip if in cooldown
            if cooldown_until and datetime.utcnow() < cooldown_until:
                time.sleep(POLL_SLEEP)
                continue

            # Skip if already traded this candle
            if last_trade_candle == last_candle_time:
                time.sleep(POLL_SLEEP)
                continue
            last_trade_candle = last_candle_time

            # -------- ENTRY LOGIC (Same as previous code) --------
            emaF_prev, emaS_prev = df['ema_fast'].iloc[-2], df['ema_slow'].iloc[-2]
            emaF, emaS = df['ema_fast'].iloc[-1], df['ema_slow'].iloc[-1]

            bullish_cross = (emaF_prev <= emaS_prev) and (emaF > emaS)
            bearish_cross = (emaF_prev >= emaS_prev) and (emaF < emaS)

            slope_deg = ema_slope(df, 'ema_fast', SLOPE_WINDOW)
            if slope_deg < SLOPE_DEG:
                bullish_cross = bearish_cross = False

            side = None
            if bullish_cross and is_strong_bullish(o,h,l,c):
                side = "BUY"
            elif bearish_cross and is_strong_bearish(o,h,l,c):
                side = "SELL"

            if not side:
                time.sleep(POLL_SLEEP)
                continue

            # Check if already in position
            pos = get_open_position(SYMBOL)
            if pos:
                time.sleep(POLL_SLEEP)
                continue

            # Margin check
            balance = exchange.fetch_balance()['USDT']['free']
            required_margin = c * LOT_SIZE / LEVERAGE
            if balance < required_margin:
                log(f"❌ Insufficient balance {balance} USDT for {side} at {c}")
                time.sleep(POLL_SLEEP)
                continue

            # -------- PLACE MARKET ORDER + OCO --------
            if side=="BUY":
                exchange.create_market_buy_order(SYMBOL, LOT_SIZE)
                tp_side = 'SELL'
                tp_price = c + TP_POINTS
                sl_price = c - SL_POINTS
            else:
                exchange.create_market_sell_order(SYMBOL, LOT_SIZE)
                tp_side = 'BUY'
                tp_price = c - TP_POINTS
                sl_price = c + SL_POINTS

            symbol_str = SYMBOL.replace('/','')
            for attempt in range(MAX_RETRIES):
                try:
                    exchange.fapiPrivate_post_order_oco({
                        'symbol': symbol_str,
                        'side': tp_side,
                        'quantity': LOT_SIZE,
                        'price': round(tp_price,2),
                        'stopPrice': round(sl_price,2),
                        'stopLimitPrice': round(sl_price,2),
                        'stopLimitTimeInForce': 'GTC'
                    })
                    break
                except Exception as e:
                    log(f"⚠️ OCO order error: {e}, retry {attempt+1}/{MAX_RETRIES}")
                    time.sleep(2 ** attempt)
            else:
                log("❌ Failed to place OCO order after retries")

            log(f"[ENTRY {side}] @ {round(c,6)} | TP: {round(tp_price,6)} | SL: {round(sl_price,6)}")

            # -------- MONITOR SL / TP FOR COOLDOWN --------
            while True:
                pos = get_open_position(SYMBOL)
                if not pos:
                    break  # Position closed by OCO

                df_new = fetch_candles(SYMBOL, TIMEFRAME, limit=2)
                o2,h2,l2,c2 = df_new.iloc[-1][['open','high','low','close']]

                sl_hit = (side=="BUY" and l2 <= sl_price) or (side=="SELL" and h2 >= sl_price)
                tp_hit = (side=="BUY" and h2 >= tp_price) or (side=="SELL" and l2 <= tp_price)

                if sl_hit or tp_hit:
                    # Close any remaining position
                    pos = get_open_position(SYMBOL)
                    if pos:
                        size = float(pos['contracts'])
                        if side=="BUY":
                            exchange.create_market_sell_order(SYMBOL, size)
                        else:
                            exchange.create_market_buy_order(SYMBOL, size)

                    outcome = "SL" if sl_hit else "TP"
                    exit_price = sl_price if sl_hit else tp_price
                    log(f"[EXIT {outcome}] @ {round(exit_price,6)} | Direction: {side}")

                    if sl_hit:
                        cooldown_until = datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)
                    break

                time.sleep(POLL_SLEEP)

        except KeyboardInterrupt:
            log("Bot stopped by user.")
            break
        except Exception as e:
            log("ERROR in main loop:", repr(e))
            time.sleep(2)
            continue

if __name__ == "__main__":
    while True:
        try:
            run_live_bot()
        except Exception as e:
            log("BOT CRASHED:", repr(e))
            log("Restarting in 5 seconds...")
            time.sleep(5)
