import ccxt
import pandas as pd
import time
from datetime import datetime
import os

# ---------------- CONFIG ----------------
SYMBOL = "ETH/USDT"
TIMEFRAME = "1m"
EMA_FAST = 13
EMA_SLOW = 55
LOT_SIZE = 0.01
TP_POINTS = 30
SL_POINTS = 40
FEE_RATE = 0.0006
START_BALANCE = 4.0
LEVERAGE = 100
CANDLE_LIMIT = 50
SLIPPAGE_RATE = 0.0002
COOLDOWN_AFTER_1_SL = 30
POLL_SLEEP = 1

# ---------------- EXCHANGE ----------------
exchange = ccxt.binance({'enableRateLimit': True})

# ---------------- HELPERS ----------------
def nowstr():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(*args):
    print(nowstr(), *args, flush=True)

def fetch_candles(symbol, tf=TIMEFRAME, limit=CANDLE_LIMIT):
    ohlc = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlc, columns=["ts","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("time", inplace=True)
    return df

# ---------------- MAIN BOT ----------------
def run_paper_bot():
    balance = START_BALANCE
    cooldown = 0
    in_position = False
    entry_side = None
    entry_price = None
    last_trade_candle = None

    log(f"🚀 Starting PAPER BOT | Balance: {balance} USDT | Lot: {LOT_SIZE} | Leverage: {LEVERAGE}x")

    while True:
        try:
            df = fetch_candles(SYMBOL)
            df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
            df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
            df["ema_fast_window"] = df["ema_fast"].rolling(window=CANDLE_LIMIT).mean()
            df["ema_slow_window"] = df["ema_slow"].rolling(window=CANDLE_LIMIT).mean()

            current_candle = df.index[-1]
            ef_win = df["ema_fast_window"].iloc[-1]
            es_win = df["ema_slow_window"].iloc[-1]
            price = df["close"].iloc[-1]

            # cooldown decrement
            if cooldown > 0:
                cooldown -= 1
                log(f"Cooldown active: {cooldown} candles remaining")
                time.sleep(POLL_SLEEP)
                continue

            # ---------- ENTRY ----------
            if not in_position and current_candle != last_trade_candle:
                side = None
                if ef_win > es_win:
                    side = "buy"
                elif ef_win < es_win:
                    side = "sell"

                if side:
                    last_trade_candle = current_candle
                    in_position = True
                    entry_side = side
                    entry_price = price * (1 + SLIPPAGE_RATE if side=="buy" else 1 - SLIPPAGE_RATE)

                    # TP/SL calculation
                    tp_price = entry_price + TP_POINTS if side=="buy" else entry_price - TP_POINTS
                    sl_price = entry_price - SL_POINTS if side=="buy" else entry_price + SL_POINTS

                    log(f"[ENTRY {side.upper()}] @ {round(entry_price,6)} | TP: {round(tp_price,6)} | SL: {round(sl_price,6)}")

            # ---------- EXIT LOGIC ----------
            elif in_position:
                # simulate TP/SL hit by candle high/low
                high = df["high"].iloc[-1]
                low = df["low"].iloc[-1]
                outcome = None
                exit_price = None

                if entry_side=="buy":
                    if high >= tp_price:
                        outcome = "TP"
                        exit_price = tp_price
                    elif low <= sl_price:
                        outcome = "SL"
                        exit_price = sl_price
                else:
                    if low <= tp_price:
                        outcome = "TP"
                        exit_price = tp_price
                    elif high >= sl_price:
                        outcome = "SL"
                        exit_price = sl_price

                if outcome:
                    pnl = (exit_price - entry_price) * LOT_SIZE if entry_side=="buy" else (entry_price - exit_price) * LOT_SIZE
                    fee = LOT_SIZE * entry_price * FEE_RATE * 2
                    pnl -= fee
                    balance += pnl

                    log(f"[EXIT {outcome}] @ {round(exit_price,6)} | PnL: {round(pnl,6)} | Balance: {round(balance,6)}")

                    if outcome=="SL":
                        cooldown = COOLDOWN_AFTER_1_SL
                        log(f"SL hit -> cooldown {COOLDOWN_AFTER_1_SL} candles")
                    
                    in_position = False
                    entry_side = None
                    entry_price = None

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
