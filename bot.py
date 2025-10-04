import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import traceback
import os
import csv
import logging
import random

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
FEE_RATE = 0.0005
ORDERBOOK_SPREAD_THRESHOLD = 0.15   # <- same absolute threshold as backtest
HIST_LIMIT = 200
MIN_SLEEP = 0.3

EMA_SPANS = [10, 20, 50, 100]
INTRABAR_STEPS = 10  # match backtest

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
        # use default adjust (same as backtest's ewm(...).mean())
        df[f"ema{span}"] = df["close"].ewm(span=span).mean()
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
        # EXACT same absolute spread check as backtest
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

        # --- EMA leak fix: compute EMAs only on closed candles (no look-ahead)
        if len(df) < max(EMA_SPANS) + 5:
            time.sleep(1)
            continue

        df_closed = df.iloc[:-1].copy()   # fully closed candles only
        df_running = df.iloc[-1].copy()   # current forming candle

        # compute EMAs on closed candles (matches backtest)
        df_closed = compute_emas(df_closed)

        if len(df_closed) < max(EMA_SPANS) + 2:
            time.sleep(1)
            continue

        last_closed = df_closed.iloc[-1]  # fully closed candle with EMAs computed without lookahead
        running = df_running
        next_open = running["open"]

        now = now_ist()

        # cooldown
        if cooldown_until and now < cooldown_until:
            time.sleep(1)
            continue

        # If currently in a position -> check running candle for OCO outcome (intrabar simulation)
        if in_position and position:
            dir_ = position["dir"]
            entry_price = position["entry"]
            tp_price = position["tp_price"]
            sl_price = position["sl_price"]

            outcome = None

            # Intrabar simulation (match backtest intrabar steps)
            o = running["open"]
            h = running["high"]
            l = running["low"]

            for k in range(1, INTRABAR_STEPS + 1):
                price_up = o + (h - o) * k / INTRABAR_STEPS
                price_down = o + (l - o) * k / INTRABAR_STEPS

                if dir_ == "BUY":
                    if price_up >= tp_price:
                        outcome = "TP"
                        exit_price = tp_price
                        break
                    if price_down <= sl_price:
                        outcome = "SL"
                        exit_price = sl_price
                        break
                else:  # SELL
                    if price_down <= tp_price:
                        outcome = "TP"
                        exit_price = tp_price
                        break
                    if price_up >= sl_price:
                        outcome = "SL"
                        exit_price = sl_price
                        break

            # fallback (rare) to simple high/low
            if not outcome:
                if dir_ == "BUY":
                    if running["high"] >= tp_price:
                        outcome = "TP"; exit_price = tp_price
                    elif running["low"] <= sl_price:
                        outcome = "SL"; exit_price = sl_price
                else:
                    if running["low"] <= tp_price:
                        outcome = "TP"; exit_price = tp_price
                    elif running["high"] >= sl_price:
                        outcome = "SL"; exit_price = sl_price

            if outcome:
                # calculate pnl and apply fee (kept same scheme)
                if outcome == "TP":
                    pnl = (tp_price - entry_price) * LOT_SIZE if dir_ == "BUY" else (entry_price - tp_price) * LOT_SIZE
                else:
                    pnl = (sl_price - entry_price) * LOT_SIZE if dir_ == "BUY" else (entry_price - sl_price) * LOT_SIZE

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
            # Orderbook check before opening (same absolute threshold as backtest)
            if not order_book_allows(SYMBOL):
                logging.info(f"[{now}] Orderbook blocked entry (spread too wide). Signal was {signal}.")
                print(f"[{now}] Orderbook blocked entry. Skipping.", flush=True)
            else:
                # optional small latency to better match backtest realism (commented out to keep behavior same)
                # time.sleep(random.uniform(0.3, 1.0))

                entry_price = next_open
                tp_price = entry_price + TP_POINTS if signal == "BUY" else entry_price - TP_POINTS
                sl_price = entry_price - SL_POINTS if signal == "BUY" else entry_price + SL_POINTS

                required_margin = entry_price * LOT_SIZE / LEVERAGE
                if balance < required_margin:
                    logging.info(f"[{now}] Insufficient margin for trade. Required: {required_margin} | Bal: {balance}")
                    print(f"[{now}] Insufficient margin. Skipping.", flush=True)
                else:
                    # Open simulated position
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
