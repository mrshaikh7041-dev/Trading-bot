import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import os
import csv
import logging

# ---------------- CONFIG ----------------
SYMBOL = "BNB/USDT"
TIMEFRAME = "1m"
LOT_SIZE = 0.10
TP_POINTS = 6
SL_POINTS = 3
BALANCE = 5.0
LEVERAGE = 100
COOLDOWN_MINUTES = 30
CSV_FN = f"{SYMBOL.replace('/','_')}_paper_trades_sync.csv"
LOG_FILE = "bot.log"
FEE_RATE = 0.0006  # 0.06% roundtrip fee

# EMA sets
EMA_SETS = {
    "Set3": [10, 20, 50, 100],
}

# ---------------- LOGGING ----------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ---------------- EXCHANGE ----------------
exchange = ccxt.binance({'enableRateLimit': True})

# ---------------- UTILS ----------------
def now_ist():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))

def fetch_latest_candles(symbol, timeframe, limit=200):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=["time","open","high","low","close","volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms") + pd.Timedelta(hours=5, minutes=30)
        return df
    except:
        return None

def apply_ema_strategy(df, ema_set):
    df = df.copy()
    for span in ema_set:
        df[f"ema{span}"] = df["close"].ewm(span=span).mean()

    signals = []
    for i in range(1, len(df)):
        c = df.loc[i, "close"]
        h = df.loc[i, "high"]
        l = df.loc[i, "low"]
        emas = [df.loc[i, f"ema{span}"] for span in ema_set]

        signal = None
        if all(c > e for e in emas):
            signal = "BUY"
        elif all(c < e for e in emas):
            signal = "SELL"
        elif l <= emas[len(emas)//2] <= h:
            if c > emas[len(emas)//2]:
                signal = "BUY"
            elif c < emas[len(emas)//2]:
                signal = "SELL"
        signals.append(signal)
    df["signal"] = [None] + signals
    return df

def order_book_filter(symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        top_bid = ob['bids'][0][0] if ob['bids'] else 0
        top_ask = ob['asks'][0][0] if ob['asks'] else 0
        spread = top_ask - top_bid
        return spread <= 0.15
    except:
        return True

def append_trade_csv(record):
    header = ["time","dir","entry","exit","outcome","pnl","balance"]
    file_exists = os.path.isfile(CSV_FN)
    with open(CSV_FN, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)

# ---------------- STARTUP ----------------
STARTUP_MSG = f"🚀 Starting Paper Bot with EMA Backtest Strategy | Balance: {BALANCE}"
print(f"[{now_ist()}] {STARTUP_MSG}")
logging.info(STARTUP_MSG)

# ---------------- MAIN LOOP ----------------
balance = BALANCE
in_position = False
position = None
cooldown_until = None

while True:
    try:
        df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
        if df is None or len(df) < 10:
            time.sleep(1)
            continue

        df = apply_ema_strategy(df, EMA_SETS["Set3"])
        df = df.iloc[:-1]  # last candle is running

        for idx, row in df.iterrows():
            t = row["time"]
            signal = row["signal"]

            if cooldown_until and t < cooldown_until:
                continue
            if in_position or not signal or idx+1 >= len(df):
                continue
            if not order_book_filter(SYMBOL):
                continue

            next_open = df.loc[idx+1, "open"]
            entry_price = next_open
            tp_price = entry_price + TP_POINTS if signal=="BUY" else entry_price - TP_POINTS
            sl_price = entry_price - SL_POINTS if signal=="BUY" else entry_price + SL_POINTS

            required_margin = entry_price * LOT_SIZE / LEVERAGE
            if balance < required_margin:
                break

            in_position = True
            result = None

            # 1s tick simulation instead of intrabar
            for j in range(idx+1, len(df)):
                o2,h2,l2,c2 = df.loc[j, ["open","high","low","close"]]
                # simulate tick
                price_ticks = [o2, h2, l2, c2]
                for tick_price in price_ticks:
                    if signal=="BUY":
                        if tick_price >= tp_price:
                            pnl = (tp_price - entry_price)*LOT_SIZE
                            result = ("TP", tp_price, pnl, df.loc[j,"time"])
                            break
                        elif tick_price <= sl_price:
                            pnl = -(entry_price - sl_price)*LOT_SIZE
                            result = ("SL", sl_price, pnl, df.loc[j,"time"])
                            break
                    else:
                        if tick_price <= tp_price:
                            pnl = (entry_price - tp_price)*LOT_SIZE
                            result = ("TP", tp_price, pnl, df.loc[j,"time"])
                            break
                        elif tick_price >= sl_price:
                            pnl = -(sl_price - entry_price)*LOT_SIZE
                            result = ("SL", sl_price, pnl, df.loc[j,"time"])
                            break
                if result:
                    break

            if result:
                outcome, exit_price, pnl, exit_time = result
                fee = entry_price * LOT_SIZE * FEE_RATE
                pnl -= fee
                balance += pnl
                rec = {
                    "time": df.loc[idx+1,"time"],
                    "dir": signal,
                    "entry": entry_price,
                    "exit": exit_price,
                    "outcome": outcome,
                    "pnl": round(pnl,6),
                    "balance": round(balance,6)
                }
                append_trade_csv(rec)
                print(f"[{now_ist()}] {outcome} {signal} closed. PnL: {round(pnl,4)} | Bal: {round(balance,4)}")
                logging.info(f"{outcome} {signal} closed. PnL: {round(pnl,4)} | Bal: {round(balance,4)}")
                in_position = False
                position = None
                if outcome=="SL":
                    cooldown_until = exit_time + timedelta(minutes=COOLDOWN_MINUTES)

        time.sleep(1)

    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        logging.error(f"[FATAL ERROR] {e}")
        time.sleep(3)
