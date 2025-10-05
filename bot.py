import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import traceback
import os
import csv
import logging

# ================= CONFIG =================
SYMBOL = "BNB/USDT"
TIMEFRAME = "1m"
LOT_SIZE = 0.10
TP_POINTS = 6
SL_POINTS = 3
BALANCE = 5.0
LEVERAGE = 100
COOLDOWN_MINUTES = 30
ORDERBOOK_SPREAD_THRESHOLD = 0.15
INTRABAR_STEPS = 10
EMA_SPANS = [10, 20, 50, 100]
CSV_FN = f"{SYMBOL.replace('/','_')}_paper_trades.csv"
LOG_FILE = "live_paper_bot.log"
FEE_RATE = 0.0005        # same as backtest

# ================= LOGGING =================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

exchange = ccxt.binance({'enableRateLimit': True})

# ================= STATE =================
balance = BALANCE
in_position = False
position = None
cooldown_until = None
wait_for_next_signal = False

# ================= HELPERS =================
def now_ist():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))

def fetch_latest_candles(symbol, timeframe, limit=200):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < max(EMA_SPANS) + 5:
            return None
        df = pd.DataFrame(bars, columns=["time","open","high","low","close","volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata")
        return df
    except Exception as e:
        logging.error(f"fetch_latest_candles failed: {e}")
        return None

def compute_emas(df):
    for span in EMA_SPANS:
        df[f"ema{span}"] = df["close"].ewm(span=span).mean()
    return df

def check_signal(candle):
    c = candle["close"]
    h = candle["high"]
    l = candle["low"]
    emas = [candle[f"ema{span}"] for span in EMA_SPANS]
    mid_ema = emas[len(emas)//2]

    if all(c > e for e in emas):
        return "BUY"
    elif all(c < e for e in emas):
        return "SELL"
    elif l <= mid_ema <= h:
        if c > mid_ema:
            return "BUY"
        elif c < mid_ema:
            return "SELL"
    return None

def order_book_allows(symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        top_bid = ob["bids"][0][0] if ob["bids"] else 0
        top_ask = ob["asks"][0][0] if ob["asks"] else 0
        spread = top_ask - top_bid
        return spread <= ORDERBOOK_SPREAD_THRESHOLD
    except Exception as e:
        logging.warning(f"order_book_allows failed, fallback True: {e}")
        return True

def append_trade_csv(record):
    header = ["time","dir","entry","exit","outcome","pnl","balance"]
    file_exists = os.path.isfile(CSV_FN)
    with open(CSV_FN, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)

# ================= STARTUP =================
msg = f"🚀 Starting Live Paper Trader ({SYMBOL}) | Bal={BALANCE} | TF={TIMEFRAME}"
print(f"[{now_ist()}] {msg}", flush=True)
logging.info(msg)

# ================= MAIN LOOP =================
while True:
    try:
        df = fetch_latest_candles(SYMBOL, TIMEFRAME)
        if df is None:
            time.sleep(1)
            continue

        # use closed candles only for EMAs/signals
        df_closed = df.iloc[:-1].copy()
        df_closed = compute_emas(df_closed)

        if len(df_closed) < max(EMA_SPANS) + 2:
            time.sleep(1)
            continue

        last_closed = df_closed.iloc[-1]
        running = df.iloc[-1]
        next_open = running["open"]
        now = now_ist()

        # cooldown
        if cooldown_until and now < cooldown_until:
            time.sleep(1)
            continue

        # ====== check running position ======
        if in_position and position:
            o = running["open"]
            h = running["high"]
            l = running["low"]
            dir_ = position["dir"]
            entry = position["entry"]
            tp = position["tp"]
            sl = position["sl"]

            outcome = None
            exit_price = None

            for k in range(1, INTRABAR_STEPS + 1):
                price_up = o + (h - o) * k / INTRABAR_STEPS
                price_down = o + (l - o) * k / INTRABAR_STEPS
                if dir_ == "BUY":
                    if price_up >= tp:
                        outcome, exit_price = "TP", tp; break
                    if price_down <= sl:
                        outcome, exit_price = "SL", sl; break
                else:
                    if price_down <= tp:
                        outcome, exit_price = "TP", tp; break
                    if price_up >= sl:
                        outcome, exit_price = "SL", sl; break

            if not outcome:
                if dir_ == "BUY":
                    if running["high"] >= tp:
                        outcome, exit_price = "TP", tp
                    elif running["low"] <= sl:
                        outcome, exit_price = "SL", sl
                else:
                    if running["low"] <= tp:
                        outcome, exit_price = "TP", tp
                    elif running["high"] >= sl:
                        outcome, exit_price = "SL", sl

            if outcome:
                if outcome == "TP":
                    pnl = (tp - entry) * LOT_SIZE if dir_ == "BUY" else (entry - tp) * LOT_SIZE
                else:
                    pnl = -(entry - sl) * LOT_SIZE if dir_ == "BUY" else -(sl - entry) * LOT_SIZE

                fee = entry * LOT_SIZE * FEE_RATE * 2
                pnl -= fee
                balance += pnl

                rec = {
                    "time": position["entry_time"].isoformat(),
                    "dir": dir_,
                    "entry": round(entry, 6),
                    "exit": round(exit_price, 6),
                    "outcome": outcome,
                    "pnl": round(pnl, 6),
                    "balance": round(balance, 6)
                }
                append_trade_csv(rec)
                msg = f"{outcome} {dir_} | PnL: {round(pnl,6)} | Bal: {round(balance,6)}"
                print(f"[{now}] {msg}", flush=True)
                logging.info(msg)

                in_position = False
                position = None
                if outcome == "SL":
                    cooldown_until = now + timedelta(minutes=COOLDOWN_MINUTES)
                else:
                    wait_for_next_signal = True

            time.sleep(1)
            continue

        # ====== new signal evaluation ======
        signal = check_signal(last_closed)

        if (not in_position) and (not wait_for_next_signal) and signal:
            if not order_book_allows(SYMBOL):
                print(f"[{now}] Spread too wide. Skip {signal}.", flush=True)
                logging.info(f"Spread too wide. Skip {signal}.")
            else:
                entry = next_open
                tp = entry + TP_POINTS if signal == "BUY" else entry - TP_POINTS
                sl = entry - SL_POINTS if signal == "BUY" else entry + SL_POINTS

                required_margin = entry * LOT_SIZE / LEVERAGE
                if balance < required_margin:
                    print(f"[{now}] Insufficient margin. Skipping.", flush=True)
                    continue

                in_position = True
                position = {
                    "dir": signal,
                    "entry": entry,
                    "tp": tp,
                    "sl": sl,
                    "entry_time": now
                }

                msg = f"Opened {signal} @ {round(entry,6)} | TP: {round(tp,6)} | SL: {round(sl,6)}"
                print(f"[{now}] {msg}", flush=True)
                logging.info(msg)

        elif wait_for_next_signal and signal is None:
            wait_for_next_signal = False

        time.sleep(1)

    except KeyboardInterrupt:
        print("Stopped manually.", flush=True)
        logging.info("Stopped manually.")
        break
    except Exception as e:
        logging.error(f"Main loop error: {e}")
        traceback.print_exc()
        time.sleep(2)
        continue
