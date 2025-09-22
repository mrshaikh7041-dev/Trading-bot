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
POLL_SLEEP = 1  # seconds

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
    ohlc = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","volume"])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df

def get_open_position(symbol):
    """Return current open position info if any"""
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

# -------- LIVE BOT --------
def run_live_bot():
    cooldown_until = None
    last_trend = None

    log(f"🚀 Starting LIVE BOT | Lot: {LOT_SIZE} | Leverage: {LEVERAGE}x")

    while True:
        try:
            df = fetch_candles(SYMBOL, TIMEFRAME, limit=50)
            df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

            last_candle = df.iloc[-2]
            o,h,l,c = last_candle[['open','high','low','close']]
            emaF, emaS = last_candle['ema_fast'], last_candle['ema_slow']

            if cooldown_until and datetime.utcnow() < cooldown_until:
                time.sleep(POLL_SLEEP)
                continue

            # EMA crossover detection
            emaF_prev, emaS_prev = df.iloc[-3]['ema_fast'], df.iloc[-3]['ema_slow']
            bullish_cross = (emaF_prev <= emaS_prev) and (emaF > emaS)
            bearish_cross = (emaF_prev >= emaS_prev) and (emaF < emaS)

            emaF_past = df.iloc[-1-SLOPE_WINDOW]['ema_fast']
            slope_deg = abs(math.degrees(math.atan((emaF - emaF_past)/SLOPE_WINDOW)))
            slope_ok = slope_deg >= SLOPE_DEG

            if bullish_cross and slope_ok:
                last_trend = "BUY"
            elif bearish_cross and slope_ok:
                last_trend = "SELL"
            else:
                last_trend = None

            # --- Skip if already in position ---
            pos = get_open_position(SYMBOL)
            if pos:
                time.sleep(POLL_SLEEP)
                continue

            if last_trend=="BUY" and not is_strong_bullish(o,h,l,c):
                time.sleep(POLL_SLEEP)
                continue
            if last_trend=="SELL" and not is_strong_bearish(o,h,l,c):
                time.sleep(POLL_SLEEP)
                continue

            # Entry
            entry_price = df.iloc[-1]['open']
            direction = last_trend

            balance = exchange.fetch_balance()['USDT']['free']
            required_margin = entry_price * LOT_SIZE / LEVERAGE
            if balance < required_margin:
                log(f"❌ Insufficient balance for {direction} at {entry_price}")
                time.sleep(POLL_SLEEP)
                continue

            if direction=="BUY":
                exchange.create_market_buy_order(SYMBOL, LOT_SIZE)
            else:
                exchange.create_market_sell_order(SYMBOL, LOT_SIZE)

            tp_price = entry_price + TP_POINTS if direction=="BUY" else entry_price - TP_POINTS
            sl_price = entry_price - SL_POINTS if direction=="BUY" else entry_price + SL_POINTS

            log(f"[ENTRY {direction}] @ {round(entry_price,6)} | TP: {tp_price} | SL: {sl_price}")

            # Monitor position
            while True:
                df_new = fetch_candles(SYMBOL, TIMEFRAME, limit=2)
                o2,h2,l2,c2 = df_new.iloc[-1][['open','high','low','close']]

                outcome = None
                if direction=="BUY":
                    if h2 >= tp_price:
                        outcome = "TP"; exit_price = tp_price
                    elif l2 <= sl_price:
                        outcome = "SL"; exit_price = sl_price
                else:
                    if l2 <= tp_price:
                        outcome = "TP"; exit_price = tp_price
                    elif h2 >= sl_price:
                        outcome = "SL"; exit_price = sl_price

                if outcome:
                    # Close with actual position size
                    pos = get_open_position(SYMBOL)
                    if pos:
                        size = float(pos['contracts'])
                        if direction=="BUY":
                            exchange.create_market_sell_order(SYMBOL, size)
                        else:
                            exchange.create_market_buy_order(SYMBOL, size)

                    log(f"[EXIT {outcome}] @ {round(exit_price,6)} | Direction: {direction}")

                    if outcome=="SL":
                        cooldown_until = datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)
                        last_trend = None
                    break

                time.sleep(POLL_SLEEP)

        except KeyboardInterrupt:
            log("Bot stopped by user.")
            break
        except Exception as e:
            log("ERROR:", repr(e))
            time.sleep(2)
            continue

if __name__ == "__main__":
    while True:  # Auto-restart loop
        try:
            run_live_bot()
        except Exception as e:
            log("BOT CRASHED:", repr(e))
            log("Restarting in 5 seconds...")
            time.sleep(5)
