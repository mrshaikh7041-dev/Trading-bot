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
LOT_SIZE = 0.10
TP_POINTS = 6
SL_POINTS = 3
BALANCE = 5.0
LEVERAGE = 100
COOLDOWN_MINUTES = 30
CSV_FN = f"{SYMBOL.replace('/','_')}_paper_trades.csv"
LOG_FILE = "live_paper_bot.log"
FEE_RATE = 0.0005   # same as earlier (both sides)
ORDERBOOK_SPREAD_THRESHOLD = 0.15
HIST_LIMIT = 200
MIN_SLEEP = 0.3

EMA_SPANS = [10, 20, 50, 100]

# ---------------- LOGGING SETUP ----------------
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

# ---------------- HELPERS ----------------
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
    df = df.copy()
    for span in EMA_SPANS:
        df[f"ema{span}"] = df["close"].ewm(span=span, adjust=False).mean()
    return df

def check_signal(candle):
    c = candle["close"]
    h = candle["high"]
    l = candle["low"]
    emas = [candle[f"ema{span}"] for span in EMA_SPANS]
    # All above -> BUY
    if all(c > e for e in emas):
        return "BUY"
    # All below -> SELL
    if all(c < e for e in emas):
        return "SELL"
    # Middle EMA touch logic (use middle index)
    mid_idx = len(emas)//2
    mid_ema = emas[mid_idx]
    if l <= mid_ema <= h:
        if c > mid_ema:
            return "BUY"
        elif c < mid_ema:
            return "SELL"
    return None

def order_book_allows(symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        top_bid = ob.get('bids')[0][0] if ob.get('bids') else 0
        top_ask = ob.get('asks')[0][0] if ob.get('asks') else 0
        spread = top_ask - top_bid
        return spread <= ORDERBOOK_SPREAD_THRESHOLD
    except Exception as e:
        logging.warning(f"order_book_allows failed, permissive fallback: {e}")
        return True  # fail-open to avoid stalling; change if you want stricter behavior

def append_trade_csv(record):
    header = ["time","dir","entry","exit","outcome","pnl","balance"]
    file_exists = os.path.isfile(CSV_FN)
    with open(CSV_FN, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)

# ---------------- STARTUP ----------------
STARTUP_MSG = f"🚀 Starting Live Paper Trader ({SYMBOL}) | Balance: {BALANCE} | Timeframe: {TIMEFRAME}"
print(f"[{now_ist()}] {STARTUP_MSG}", flush=True)
logging.info(STARTUP_MSG)

# ---------------- MAIN LOOP ----------------
while True:
    try:
        df = fetch_latest_candles(SYMBOL, TIMEFRAME, limit=HIST_LIMIT)
        if df is None:
            time.sleep(1)
            continue

        df = compute_emas(df)
        now = now_ist()

        # cooldown
        if cooldown_until and now < cooldown_until:
            time.sleep(1)
            continue

        last_closed = df.iloc[-2]   # fully closed candle
        running = df.iloc[-1]       # current forming candle
        next_open = running["open"]

        # If currently in a position -> check running candle for OCO outcome (no intrabar polling)
        if in_position and position:
            dir_ = position["dir"]
            entry_price = position["entry"]
            tp_price = position["tp_price"]
            sl_price = position["sl_price"]

            outcome = None
            # OCO logic: if both touched in same candle, assume the one closer to entry executed first
            if dir_ == "BUY":
                if running["low"] <= sl_price and running["high"] >= tp_price:
                    outcome = "SL" if abs(sl_price - entry_price) < abs(tp_price - entry_price) else "TP"
                elif running["high"] >= tp_price:
                    outcome = "TP"
                elif running["low"] <= sl_price:
                    outcome = "SL"
            else:  # SELL
                if running["low"] <= tp_price and running["high"] >= sl_price:
                    outcome = "SL" if abs(sl_price - entry_price) < abs(tp_price - entry_price) else "TP"
                elif running["low"] <= tp_price:
                    outcome = "TP"
                elif running["high"] >= sl_price:
                    outcome = "SL"

            if outcome:
                # calculate pnl and apply fee
                if outcome == "TP":
                    pnl = (tp_price - entry_price) * LOT_SIZE if dir_ == "BUY" else (entry_price - tp_price) * LOT_SIZE
                    exit_price = tp_price
                else:
                    pnl = (sl_price - entry_price) * LOT_SIZE if dir_ == "BUY" else (entry_price - sl_price) * LOT_SIZE
                    exit_price = sl_price

                fee = entry_price * LOT_SIZE * FEE_RATE
                pnl_after_fee = pnl - fee
                balance += pnl_after_fee

                rec = {
                    "time": position["entry_time"].isoformat(),
                    "dir": dir_,
                    "entry": round(entry_price, 6),
                    "exit": round(exit_price, 6),
                    "outcome": outcome,
                    "pnl": round(pnl_after_fee, 6),
                    "balance": round(balance, 6),
                }
                append_trade_csv(rec)
                msg = f"{outcome} {dir_} closed | PnL: {round(pnl_after_fee,6)} | Bal: {round(balance,6)}"
                print(f"[{now}] {msg}", flush=True)
                logging.info(msg)

                # reset position state
                in_position = False
                position = None

                if outcome == "SL":
                    cooldown_until = now + timedelta(minutes=COOLDOWN_MINUTES)
                else:
                    wait_for_next_signal = True

            time.sleep(1)
            continue

        # Not in position -> evaluate signal on last closed candle
        signal = check_signal(last_closed)
        if (not in_position) and (not wait_for_next_signal) and signal:
            # Orderbook check before opening
            if not order_book_allows(SYMBOL):
                logging.info(f"[{now}] Orderbook blocked entry (spread too wide). Signal was {signal}.")
                print(f"[{now}] Orderbook blocked entry. Skipping.", flush=True)
            else:
                entry_price = next_open
                tp_price = entry_price + TP_POINTS if signal == "BUY" else entry_price - TP_POINTS
                sl_price = entry_price - SL_POINTS if signal == "BUY" else entry_price + SL_POINTS

                required_margin = entry_price * LOT_SIZE / LEVERAGE
                if balance < required_margin:
                    logging.info(f"[{now}] Insufficient margin for trade. Required: {required_margin} | Bal: {balance}")
                    print(f"[{now}] Insufficient margin. Skipping.", flush=True)
                else:
                    # Open simulated position (no added latency)
                    in_position = True
                    position = {
                        "dir": signal,
                        "entry": entry_price,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "entry_time": now
                    }
                    msg = f"Opened {signal} @ {round(entry_price,6)} | TP: {round(tp_price,6)} | SL: {round(sl_price,6)}"
                    print(f"[{now}] {msg}", flush=True)
                    logging.info(msg)
        else:
            if wait_for_next_signal and signal is None:
                wait_for_next_signal = False
            # quiet message to reduce spam
            # print(f"[{now}] No signal / waiting...", flush=True)

        time.sleep(1)

    except KeyboardInterrupt:
        print("User stopped the bot. Exiting.", flush=True)
        logging.info("User stopped the bot.")
        break
    except Exception as e:
        logging.error(f"Main loop error: {e}")
        traceback.print_exc()
        time.sleep(2)
        continue
