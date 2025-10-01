import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import traceback
import os
import csv
import logging

# ---------------- CONFIG ----------------
SYMBOL = "BNB/USDT"
TIMEFRAME = "1m"
LOT_SIZE = 0.08
TP_POINTS = 6
SL_POINTS = 3
BALANCE = 2.0
COOLDOWN_MINUTES = 30
CSV_FN = f"{SYMBOL.replace('/','_')}_paper_trades_sync.csv"
LOG_FILE = "bot.log"
FEE_RATE = 0.0005   # 0.05% one-time fee
CALC_WINDOW_SEC = 3  # last 3 sec window for calculation

# ---------------- LOGGING ----------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ---------------- EXCHANGE ----------------
exchange = ccxt.binance({'enableRateLimit': True})

# ---------------- STATE ----------------
balance = BALANCE
in_position = False
cooldown_until = None
position = None
wait_for_next_signal = False

# ---------------- UTILS ----------------
def now_ist():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))

def fetch_latest_candles(symbol, timeframe, limit=200):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 10:
            return None
        df = pd.DataFrame(bars, columns=["time","open","high","low","close","volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata")
        return df
    except Exception as e:
        print(f"[ERROR] Fetch candles failed: {e}", flush=True)
        logging.error(f"Fetch candles failed: {e}")
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

    if c > ema5 and c > ema9 and c > ema15 and c > ema21:
        return "BUY"
    if c < ema5 and c < ema9 and c < ema15 and c < ema21:
        return "SELL"
    if l <= ema15 <= h and c > ema15:
        return "BUY"
    if l <= ema15 <= h and c < ema15:
        return "SELL"
    return None

def append_trade_csv(record):
    header = ["time","dir","entry","exit","outcome","pnl","balance"]
    file_exists = os.path.isfile(CSV_FN)
    with open(CSV_FN, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)

# ---------------- STARTUP MESSAGE ----------------
STARTUP_MSG = f"🚀 Starting EMA Bot ({SYMBOL}, Paper Trading Synced) | Starting Balance: {BALANCE} USDT"
print(f"[{now_ist()}] {STARTUP_MSG}", flush=True)
logging.info(STARTUP_MSG)

# ---------------- MAIN LOOP ----------------
while True:
    try:
        df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
        if df is None:
            time.sleep(1)
            continue

        df = compute_emas(df)
        now = now_ist()

        # ---------------- COOLDOWN CHECK ----------------
        if cooldown_until and now < cooldown_until:
            time.sleep(1)
            continue

        last_closed_candle = df.iloc[-2]   # completed candle
        recent_candle = df.iloc[-1]        # running candle
        next_open = recent_candle["open"]

        # ---------------- IN POSITION ----------------
        if in_position and position:
            dir = position["dir"]
            entry_price = position["entry"]
            tp_price = position["tp_price"]
            sl_price = position["sl_price"]

            outcome = None
            if dir == "BUY":
                if recent_candle["low"] <= sl_price and recent_candle["high"] >= tp_price:
                    outcome = "TP" if abs(tp_price-entry_price)<abs(sl_price-entry_price) else "SL"
                elif recent_candle["high"] >= tp_price:
                    outcome = "TP"
                elif recent_candle["low"] <= sl_price:
                    outcome = "SL"
            else:
                if recent_candle["low"] <= tp_price and recent_candle["high"] >= sl_price:
                    outcome = "TP" if abs(tp_price-entry_price)<abs(sl_price-entry_price) else "SL"
                elif recent_candle["low"] <= tp_price:
                    outcome = "TP"
                elif recent_candle["high"] >= sl_price:
                    outcome = "SL"

            if outcome:
                pnl = (tp_price - entry_price) * LOT_SIZE if dir=="BUY" else (entry_price - tp_price) * LOT_SIZE
                if outcome=="SL":
                    pnl = (sl_price - entry_price)*LOT_SIZE if dir=="BUY" else (entry_price - sl_price)*LOT_SIZE
                fee = entry_price * LOT_SIZE * FEE_RATE
                pnl -= fee
                balance += pnl
                rec = {
                    "time": position["entry_time"].isoformat(),
                    "dir": dir,
                    "entry": entry_price,
                    "exit": tp_price if outcome=="TP" else sl_price,
                    "outcome": outcome,
                    "pnl": round(pnl,6),
                    "balance": round(balance,6)
                }
                append_trade_csv(rec)
                msg = f"{outcome} {dir} trade closed. PnL: {round(pnl,4)} | Balance: {round(balance,4)}"
                print(f"[{now}] {msg}", flush=True)
                logging.info(msg)
                in_position = False
                position = None
                if outcome=="SL":
                    cooldown_until = now + timedelta(minutes=COOLDOWN_MINUTES)
                else:
                    wait_for_next_signal = True
            time.sleep(1)
            continue

        # ---------------- NOT IN POSITION ----------------
        # last 3 sec window logic
        candle_close_time = last_closed_candle["time"]
        seconds_to_close = (pd.Timestamp.now(tz=last_closed_candle["time"].tz) - candle_close_time).total_seconds()
        if seconds_to_close >= (60 - CALC_WINDOW_SEC):
            signal = check_signal(last_closed_candle)
            if not in_position and not wait_for_next_signal and signal:
                entry_price = next_open
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
                msg = f"Opening new trade {signal} @ {entry_price}"
                print(f"[{now}] {msg}", flush=True)
                logging.info(msg)
            else:
                if wait_for_next_signal and signal is None:
                    wait_for_next_signal = False
                msg = "No valid signal or waiting/cooldown → waiting..."
                print(f"[{now}] {msg}", flush=True)
                logging.info(msg)
        else:
            # short sleep to sync with candle close
            time.sleep(0.5)
            continue

        time.sleep(0.5)

    except Exception as e:
        msg = f"[FATAL ERROR] {e}"
        print(msg, flush=True)
        logging.error(msg)
        traceback.print_exc()
        time.sleep(3)
