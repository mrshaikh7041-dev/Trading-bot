import ccxt
import pandas as pd
import time
import math
from datetime import datetime, timedelta

# ---------------- CONFIG ----------------
SYMBOL = "ETH/USDT"
TIMEFRAME = "1m"
EMA_FAST = 13
EMA_SLOW = 55
LOT_SIZE = 0.02
TP_POINTS = 40
SL_POINTS = 20
START_BALANCE = 3.0
LEVERAGE = 100               # only for margin check
COOLDOWN_AFTER_SL = 30       # minutes
POLL_SLEEP = 1               # seconds
SLOPE_WINDOW = 3
SLOPE_DEG = 20

# ---------------- EXCHANGE ----------------
exchange = ccxt.binance({'enableRateLimit': True})

# ---------------- HELPERS ----------------
def nowstr():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(*args):
    print(nowstr(), *args, flush=True)

def fetch_candles(symbol, tf=TIMEFRAME, limit=50):
    ohlc = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlc, columns=["ts","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("time", inplace=True)
    return df

# EMA slope calculation
def ema_slope(df, ema_col, window):
    if len(df) < window + 1:
        return 0
    delta_y = df[ema_col].iloc[-1] - df[ema_col].iloc[-window-1]
    slope_deg = abs(math.degrees(math.atan(delta_y / window)))
    return slope_deg

# Candle check
def is_bullish(o,h,l,c):
    body = c - o
    rng = h - l if h-l !=0 else 1e-9
    if body>0 and (body/rng)>=0.55:
        return True
    return False

def is_bearish(o,h,l,c):
    body = c - o
    rng = h - l if h-l !=0 else 1e-9
    if body<0 and (abs(body)/rng)>=0.55:
        return True
    return False

# ---------------- BOT ----------------
def run_paper_bot():
    balance = START_BALANCE
    cooldown_until = None
    in_position = False
    entry_side = None
    entry_price = None
    tp_price = None
    sl_price = None
    last_trade_candle = None

    daily_trades = 0
    daily_tp = 0
    daily_sl = 0
    current_day = None

    log(f"🚀 Starting PAPER BOT | Balance: {balance} USDT | Lot: {LOT_SIZE} | Leverage: {LEVERAGE}x")

    while True:
        try:
            df = fetch_candles(SYMBOL, limit=50)
            df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
            df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

            current_candle = df.index[-1]
            o,h,l,c = df.iloc[-1][['open','high','low','close']]

            # reset daily counters
            if current_day != df.index[-1].date():
                if current_day is not None:
                    log(f"📊 DAILY SUMMARY | Trades: {daily_trades} | TP: {daily_tp} | SL: {daily_sl} | Balance: {round(balance,6)} USDT")
                current_day = df.index[-1].date()
                daily_trades = 0
                daily_tp = 0
                daily_sl = 0

            # cooldown check
            if cooldown_until and datetime.utcnow() < cooldown_until:
                time.sleep(POLL_SLEEP)
                continue

            # Entry logic
            if not in_position and current_candle != last_trade_candle:
                last_trade_candle = current_candle
                bullish_cross = (df["ema_fast"].iloc[-2] <= df["ema_slow"].iloc[-2]) and (df["ema_fast"].iloc[-1] > df["ema_slow"].iloc[-1])
                bearish_cross = (df["ema_fast"].iloc[-2] >= df["ema_slow"].iloc[-2]) and (df["ema_fast"].iloc[-1] < df["ema_slow"].iloc[-1])

                slope = ema_slope(df,"ema_fast",SLOPE_WINDOW)
                if slope < SLOPE_DEG:
                    bullish_cross = bearish_cross = False

                side = None
                if bullish_cross and is_bullish(o,h,l,c):
                    side = "BUY"
                elif bearish_cross and is_bearish(o,h,l,c):
                    side = "SELL"

                if side:
                    # check margin
                    notional = c * LOT_SIZE
                    required_margin = notional / LEVERAGE
                    if balance < required_margin:
                        log(f"❌ Insufficient balance {balance} USDT for {side} at {c}, stopping bot.")
                        break

                    entry_side = side
                    entry_price = c
                    tp_price = entry_price + TP_POINTS if side=="BUY" else entry_price - TP_POINTS
                    sl_price = entry_price - SL_POINTS if side=="BUY" else entry_price + SL_POINTS
                    in_position = True
                    log(f"[ENTRY {side}] @ {round(entry_price,6)} | TP: {round(tp_price,6)} | SL: {round(sl_price,6)}")

            # Exit logic
            if in_position:
                outcome = None
                exit_price = None
                if entry_side=="BUY":
                    if h >= tp_price:
                        outcome = "TP"
                        exit_price = tp_price
                    elif l <= sl_price:
                        outcome = "SL"
                        exit_price = sl_price
                else:
                    if l <= tp_price:
                        outcome = "TP"
                        exit_price = tp_price
                    elif h >= sl_price:
                        outcome = "SL"
                        exit_price = sl_price

                if outcome:
                    pnl = (exit_price - entry_price) * LOT_SIZE if entry_side=="BUY" else (entry_price - exit_price) * LOT_SIZE
                    balance += pnl
                    daily_trades += 1
                    if outcome=="TP":
                        daily_tp +=1
                    else:
                        daily_sl +=1
                        cooldown_until = datetime.utcnow() + timedelta(minutes=COOLDOWN_AFTER_SL)

                    log(f"[EXIT {outcome}] @ {round(exit_price,6)} | PnL: {round(pnl,6)} | Balance: {round(balance,6)}")
                    in_position = False
                    entry_side = entry_price = tp_price = sl_price = None

            time.sleep(POLL_SLEEP)

        except KeyboardInterrupt:
            log("Bot stopped by user.")
            break
        except Exception as e:
            log("ERROR:", repr(e))
            time.sleep(2)
            continue

# ---------------- RUN ----------------
if __name__ == "__main__":
    run_paper_bot()
