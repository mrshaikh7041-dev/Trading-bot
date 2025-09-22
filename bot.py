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
POLL_SLEEP = 1  # seconds

# Starting balance for paper trading
START_BALANCE = 1.5
balance = START_BALANCE

# ---------------- EXCHANGE ----------------
exchange = ccxt.binance({
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

# -------- PAPER TRADING BOT --------
def run_paper_bot():
    global balance
    cooldown_until = None
    last_trend = None
    position = None  # Track current paper position
    entry_price = None

    log(f"🚀 Starting PAPER BOT | Lot: {LOT_SIZE} | Leverage: {LEVERAGE}x | Starting Balance: {balance}")

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

            # Skip if already in position
            if position:
                time.sleep(POLL_SLEEP)
                continue

            if last_trend=="BUY" and not is_strong_bullish(o,h,l,c):
                time.sleep(POLL_SLEEP)
                continue
            if last_trend=="SELL" and not is_strong_bearish(o,h,l,c):
                time.sleep(POLL_SLEEP)
                continue

            # Check balance for margin
            required_margin = (df.iloc[-1]['open'] * LOT_SIZE) / LEVERAGE
            if balance < required_margin:
                log(f"❌ Insufficient balance for {last_trend} at {df.iloc[-1]['open']}")
                time.sleep(POLL_SLEEP)
                continue

            # Paper entry
            entry_price = df.iloc[-1]['open']
            position = {
                "direction": last_trend,
                "lot": LOT_SIZE,
                "entry": entry_price,
                "tp": entry_price + TP_POINTS if last_trend=="BUY" else entry_price - TP_POINTS,
                "sl": entry_price - SL_POINTS if last_trend=="BUY" else entry_price + SL_POINTS
            }

            log(f"[PAPER ENTRY {last_trend}] @ {entry_price} | TP: {position['tp']} | SL: {position['sl']}")

            # Simulate OCO TP/SL
            while position:
                df_new = fetch_candles(SYMBOL, TIMEFRAME, limit=2)
                o2,h2,l2,c2 = df_new.iloc[-1][['open','high','low','close']]

                outcome = None
                exit_price = None

                if position['direction']=="BUY":
                    if h2 >= position['tp']:
                        outcome = "TP"; exit_price = position['tp']
                    elif l2 <= position['sl']:
                        outcome = "SL"; exit_price = position['sl']
                else:
                    if l2 <= position['tp']:
                        outcome = "TP"; exit_price = position['tp']
                    elif h2 >= position['sl']:
                        outcome = "SL"; exit_price = position['sl']

                if outcome:
                    pnl = (exit_price - position['entry']) * LOT_SIZE if position['direction']=="BUY" else (position['entry'] - exit_price) * LOT_SIZE
                    balance += pnl
                    log(f"[PAPER EXIT {outcome}] @ {exit_price} | PnL: {round(pnl,6)} | New Balance: {round(balance,6)}")

                    if outcome=="SL":
                        cooldown_until = datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)

                    position = None  # Reset position
                    break

                time.sleep(POLL_SLEEP)

        except KeyboardInterrupt:
            log("Paper Bot stopped by user.")
            break
        except Exception as e:
            log("ERROR:", repr(e))
            time.sleep(2)
            continue

if __name__ == "__main__":
    while True:
        try:
            run_paper_bot()
        except Exception as e:
            log("BOT CRASHED:", repr(e))
            log("Restarting in 5 seconds...")
            time.sleep(5)
