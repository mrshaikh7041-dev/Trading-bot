import ccxt
import pandas as pd
import math
import time
from datetime import datetime, timedelta

# ---------------- CONFIG ----------------
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
exchange = ccxt.binance({'enableRateLimit': True})

# -------- Candle pattern --------
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

# -------- Helpers --------
def nowstr():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(*args):
    print(nowstr(), *args, flush=True)

def fetch_candles(symbol, tf=TIMEFRAME, limit=200):
    ohlc = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","volume"])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df

# -------- Paper Trading Bot --------
def run_paper_bot():
    in_position = False
    cooldown_until = None
    last_trend, crossover_idx, last_tp_candle_close = None, None, None

    log(f"🚀 Starting IMPROVED PAPER BOT | Lot: {LOT_SIZE} | Leverage: {LEVERAGE}x")

    while True:
        try:
            df = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
            df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

            # get last closed candle for polling sync
            last_candle = df.iloc[-2]
            o,h,l,c = last_candle[['open','high','low','close']]
            emaF, emaS = last_candle['ema_fast'], last_candle['ema_slow']

            if cooldown_until and datetime.utcnow() < cooldown_until:
                time.sleep(POLL_SLEEP)
                continue

            emaF_prev, emaS_prev = df.iloc[-3]['ema_fast'], df.iloc[-3]['ema_slow']
            bullish_cross = (emaF_prev <= emaS_prev) and (emaF > emaS)
            bearish_cross = (emaF_prev >= emaS_prev) and (emaF < emaS)

            # slope
            emaF_past = df.iloc[-1-SLOPE_WINDOW]['ema_fast']
            slope_deg = abs(math.degrees(math.atan((emaF - emaF_past)/SLOPE_WINDOW)))
            slope_ok = slope_deg >= SLOPE_DEG

            if bullish_cross and slope_ok:
                last_trend, crossover_idx = "BUY", df.index[-2]
            elif bearish_cross and slope_ok:
                last_trend, crossover_idx = "SELL", df.index[-2]

            if in_position or last_trend is None:
                time.sleep(POLL_SLEEP)
                continue

            # Check strong candle at last closed candle
            if last_trend=="BUY" and not is_strong_bullish(o,h,l,c):
                time.sleep(POLL_SLEEP)
                continue
            if last_trend=="SELL" and not is_strong_bearish(o,h,l,c):
                time.sleep(POLL_SLEEP)
                continue

            # Entry at current candle open
            entry_candle = df.iloc[-1]
            entry_price = entry_candle['open']
            direction = last_trend

            # Paper trade: check balance + log
            balance = 1000  # simulate starting balance
            notional = entry_price * LOT_SIZE
            required_margin = notional / LEVERAGE
            if balance < required_margin:
                log(f"❌ Insufficient balance for {direction} at {entry_price}")
                break

            log(f"[ENTRY {direction}] @ {round(entry_price,6)} | TP: {TP_POINTS} | SL: {SL_POINTS}")
            in_position = True

            # Monitor TP/SL
            while in_position:
                df_new = fetch_candles(SYMBOL, TIMEFRAME, limit=2)
                o2,h2,l2,c2 = df_new.iloc[-1][['open','high','low','close']]
                if direction=="BUY":
                    if h2 >= entry_price + TP_POINTS:
                        outcome = "TP"
                        in_position = False
                        exit_price = entry_price + TP_POINTS
                    elif l2 <= entry_price - SL_POINTS:
                        outcome = "SL"
                        in_position = False
                        exit_price = entry_price - SL_POINTS
                else:
                    if l2 <= entry_price - TP_POINTS:
                        outcome = "TP"
                        in_position = False
                        exit_price = entry_price - TP_POINTS
                    elif h2 >= entry_price + SL_POINTS:
                        outcome = "SL"
                        in_position = False
                        exit_price = entry_price + SL_POINTS

                if not in_position:
                    log(f"[EXIT {outcome}] @ {round(exit_price,6)} | Direction: {direction}")
                    if outcome=="SL":
                        cooldown_until = datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)
                        last_trend = None
                    else:
                        last_tp_candle_close = c2
                time.sleep(POLL_SLEEP)

        except KeyboardInterrupt:
            log("Bot stopped by user.")
            break
        except Exception as e:
            log("ERROR:", repr(e))
            time.sleep(2)
            continue

# -------- RUN --------
if __name__ == "__main__":
    run_paper_bot()
