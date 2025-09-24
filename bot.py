import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta
import traceback
import os
import csv

# ---------------- CONFIG ----------------
SYMBOL = "BNB/USDT"
TIMEFRAME = "1m"
LOT_SIZE = 0.06
TP_POINTS = 6
SL_POINTS = 3
LEVERAGE = 100
BALANCE = 2.0
COOLDOWN_MINUTES = 30
SLIPPAGE_PERCENT = 0.0005
FEE_RATE = 0.0006
POLL_INTERVAL_SECONDS = 10  # data fetch interval

CSV_FN = f"{SYMBOL.replace('/','_')}_paper_trades.csv"

# ---------------- EXCHANGE ----------------
exchange = ccxt.binance({'enableRateLimit': True})

# ---------------- STATE ----------------
balance = BALANCE
in_position = False
cooldown_until = None
position = None

# ---------------- UTILS ----------------
def now_ist():
    return datetime.utcnow() + pd.Timedelta(hours=5, minutes=30)

def fetch_latest_candles(symbol, timeframe, limit=200):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 10:
            return None
        df = pd.DataFrame(bars, columns=["time","open","high","low","close","volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms") + pd.Timedelta(hours=5, minutes=30)
        return df
    except Exception as e:
        print(f"[ERROR] Fetch candles failed: {e}")
        return None

def compute_emas(df):
    df = df.copy()
    df["ema5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema15"] = df["close"].ewm(span=15, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    return df

def check_signal(candle):
    c = candle["close"]
    l = candle["low"]
    h = candle["high"]
    ema5 = candle["ema5"]
    ema9 = candle["ema9"]
    ema15 = candle["ema15"]
    ema21 = candle["ema21"]

    # All EMAs same side
    if c > ema5 and c > ema9 and c > ema15 and c > ema21:
        return "BUY"
    if c < ema5 and c < ema9 and c < ema15 and c < ema21:
        return "SELL"

    # 15 EMA support/resistance touch
    if l <= ema15 <= h and c > ema15:
        return "BUY"
    if l <= ema15 <= h and c < ema15:
        return "SELL"

    return None

def append_trade_csv(record):
    header = ["time","dir","entry","exit","outcome","pnl","balance","fee"]
    file_exists = os.path.isfile(CSV_FN)
    with open(CSV_FN, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)

# ---------------- MAIN LOOP ----------------
print(f"[{now_ist()}] 🚀 Starting EMA Bot (BNB/USDT, Paper Trading)...")

while True:
    try:
        df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
        if df is None:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        df = compute_emas(df)

        now = now_ist()

        # cooldown check
        if cooldown_until and now < cooldown_until:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        last_closed_candle = df.iloc[-2]
        next_candle_open = df.iloc[-1]["open"]

        # ---------------- IN POSITION ----------------
        if in_position and position:
            # intrabar check TP/SL
            recent = df.iloc[-1]
            dir = position["dir"]
            entry_price = position["entry"]
            tp_price = position["tp_price"]
            sl_price = position["sl_price"]

            outcome = None
            if dir == "BUY":
                if recent["high"] >= tp_price:
                    outcome = "TP"
                elif recent["low"] <= sl_price:
                    outcome = "SL"
            else:  # SELL
                if recent["low"] <= tp_price:
                    outcome = "TP"
                elif recent["high"] >= sl_price:
                    outcome = "SL"

            if outcome:
                pnl = (tp_price - entry_price) * LOT_SIZE if dir=="BUY" else (entry_price - tp_price) * LOT_SIZE
                if outcome == "SL":
                    pnl = -abs(pnl)
                fee = entry_price * LOT_SIZE * FEE_RATE * 2
                pnl_after_fee = pnl - fee
                balance += pnl_after_fee

                rec = {
                    "time": position["entry_time"].isoformat(),
                    "dir": dir,
                    "entry": entry_price,
                    "exit": tp_price if outcome=="TP" else sl_price,
                    "outcome": outcome,
                    "pnl": round(pnl_after_fee,6),
                    "balance": round(balance,6),
                    "fee": round(fee,8)
                }
                append_trade_csv(rec)
                print(f"[{now}] {outcome} {dir} trade closed. PnL: {round(pnl_after_fee,4)} | Balance: {round(balance,4)}")

                in_position = False
                position = None

                if outcome == "SL":
                    cooldown_until = now + timedelta(minutes=COOLDOWN_MINUTES)
                    continue

                # TP hit, now check if strategy condition still satisfied
                signal_after_tp = check_signal(last_closed_candle)
                if signal_after_tp:
                    # open new trade next candle
                    entry_price = next_candle_open * (1 + SLIPPAGE_PERCENT if signal_after_tp=="BUY" else 1 - SLIPPAGE_PERCENT)
                    tp_price = entry_price + TP_POINTS if signal_after_tp=="BUY" else entry_price - TP_POINTS
                    sl_price = entry_price - SL_POINTS if signal_after_tp=="BUY" else entry_price + SL_POINTS
                    position = {
                        "dir": signal_after_tp,
                        "entry": entry_price,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "entry_time": now
                    }
                    in_position = True
                    print(f"[{now}] TP condition satisfied → Opening new {signal_after_tp} @ {entry_price}")
                else:
                    print(f"[{now}] TP hit but condition not satisfied → Waiting for next valid setup")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # ---------------- NOT IN POSITION ----------------
        signal = check_signal(last_closed_candle)
        if not in_position and signal:
            entry_price = next_candle_open * (1 + SLIPPAGE_PERCENT if signal=="BUY" else 1 - SLIPPAGE_PERCENT)
            tp_price = entry_price + TP_POINTS if signal=="BUY" else entry_price - TP_POINTS
            sl_price = entry_price - SL_POINTS if signal=="BUY" else entry_price + SL_POINTS
            position = {
                "dir": signal,
                "entry": entry_price,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "entry_time": now
            }
            in_position = True
            print(f"[{now}] Opening new trade {signal} @ {entry_price}")
        else:
            print(f"[{now}] No valid signal or in cooldown → waiting...")

        time.sleep(POLL_INTERVAL_SECONDS)

    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        traceback.print_exc()
        time.sleep(10)
        continue
