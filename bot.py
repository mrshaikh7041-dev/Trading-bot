import ccxt
import pandas as pd
import time
import math
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
POLL_SLEEP = 1  # seconds between fetching new candles

exchange = ccxt.binance({'enableRateLimit': True})

# -------- Candle pattern --------
def is_strong_bullish(o,h,l,c):
    body = c - o
    rng = h - l if h-l!=0 else 1e-9
    upper_wick = h - c
    lower_wick = o - l
    if body>0 and (body/rng)>=0.55: return True
    if body>0 and (lower_wick >= 2*abs(body)) and (upper_wick <= abs(body)*0.5): return True
    if body>0 and ((c-l)/rng)>=0.75: return True
    return False

def is_strong_bearish(o,h,l,c):
    body = c - o
    rng = h - l if h-l!=0 else 1e-9
    upper_wick = h - o
    lower_wick = c - l
    if body<0 and (abs(body)/rng)>=0.55: return True
    if body<0 and (upper_wick >= 2*abs(body)) and (lower_wick <= abs(body)*0.5): return True
    if body<0 and ((h-c)/rng)>=0.75: return True
    return False

# -------- Helpers --------
def nowstr():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(*args):
    print(nowstr(), *args, flush=True)

def fetch_candles(symbol, tf=TIMEFRAME, limit=100):
    ohlc = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","volume"])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df

# -------- Bot --------
def run_paper_bot():
    balance = 3.0
    in_position = False
    cooldown_until = None
    last_trend, crossover_idx, last_tp_candle_close = None, None, None

    log(f"🚀 Starting LIVE PAPER BOT | Balance: {balance} USDT | Lot: {LOT_SIZE} | Leverage: {LEVERAGE}x")

    while True:
        try:
            df = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
            df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

            for i in range(max(EMA_SLOW, SLOPE_WINDOW), len(df)):
                time_now = df.loc[i,'time']
                o,h,l,c = df.loc[i,['open','high','low','close']]
                emaF, emaS = df.loc[i,'ema_fast'], df.loc[i,'ema_slow']

                if cooldown_until and datetime.utcnow() < cooldown_until:
                    continue

                emaF_prev, emaS_prev = df.loc[i-1,'ema_fast'], df.loc[i-1,'ema_slow']
                bullish_cross = (emaF_prev <= emaS_prev) and (emaF > emaS)
                bearish_cross = (emaF_prev >= emaS_prev) and (emaF < emaS)

                # slope
                emaF_past = df.loc[i-SLOPE_WINDOW,'ema_fast']
                slope_deg = abs(math.degrees(math.atan((emaF - emaF_past)/SLOPE_WINDOW)))
                slope_ok = slope_deg >= SLOPE_DEG

                if bullish_cross and slope_ok:
                    last_trend, crossover_idx = "BUY", i
                elif bearish_cross and slope_ok:
                    last_trend, crossover_idx = "SELL", i

                if crossover_idx is not None and crossover_idx < i:
                    if last_trend=="BUY" and not (emaF>emaS):
                        last_trend, crossover_idx = None, None
                    elif last_trend=="SELL" and not (emaF<emaS):
                        last_trend, crossover_idx = None, None

                if in_position:
                    continue

                if last_tp_candle_close is not None:
                    if last_trend=="BUY" and not (last_tp_candle_close>emaF and last_tp_candle_close>emaS):
                        last_tp_candle_close, last_trend, crossover_idx = None, None, None
                        continue
                    if last_trend=="SELL" and not (last_tp_candle_close<emaF and last_tp_candle_close<emaS):
                        last_tp_candle_close, last_trend, crossover_idx = None, None, None
                        continue
                    last_tp_candle_close = None

                if crossover_idx is not None and last_trend in ("BUY","SELL"):
                    touches_emaF = (l <= emaF <= h)
                    if not touches_emaF:
                        continue

                    if last_trend=="BUY": is_alert = is_strong_bullish(o,h,l,c)
                    else: is_alert = is_strong_bearish(o,h,l,c)
                    if not is_alert:
                        continue

                    # ENTRY at next candle open
                    entry_idx = i+1 if i+1<len(df) else i
                    entry_price = df.loc[entry_idx,'open']
                    direction = last_trend

                    # margin check
                    notional = entry_price * LOT_SIZE
                    required_margin = notional / LEVERAGE
                    if balance < required_margin:
                        log(f"❌ Insufficient balance for {direction} at {entry_price}")
                        break

                    tp_price = entry_price + TP_POINTS if direction=="BUY" else entry_price - TP_POINTS
                    sl_price = entry_price - SL_POINTS if direction=="BUY" else entry_price + SL_POINTS
                    in_position = True

                    # Live candle TP/SL check
                    while in_position:
                        df_new = fetch_candles(SYMBOL, TIMEFRAME, limit=2)
                        o2,h2,l2,c2 = df_new.iloc[-1][['open','high','low','close']]
                        if direction=="BUY":
                            if h2 >= tp_price:
                                pnl = (tp_price - entry_price) * LOT_SIZE
                                outcome = "TP"
                                in_position = False
                            elif l2 <= sl_price:
                                pnl = (sl_price - entry_price) * LOT_SIZE * -1
                                outcome = "SL"
                                in_position = False
                        else:
                            if l2 <= tp_price:
                                pnl = (entry_price - tp_price) * LOT_SIZE
                                outcome = "TP"
                                in_position = False
                            elif h2 >= sl_price:
                                pnl = (entry_price - sl_price) * LOT_SIZE * -1
                                outcome = "SL"
                                in_position = False

                        if not in_position:
                            balance += pnl
                            log(f"[{direction} ENTRY @ {entry_price}] -> EXIT {outcome} @ {tp_price if outcome=='TP' else sl_price} | PnL: {pnl} | Balance: {balance}")
                            if outcome=="SL":
                                cooldown_until = datetime.utcnow() + timedelta(minutes=COOLDOWN_MINUTES)
                                last_trend, crossover_idx = None, None
                            else:
                                last_tp_candle_close = c2
                        time.sleep(POLL_SLEEP)

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
