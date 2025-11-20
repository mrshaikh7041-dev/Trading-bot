#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# =============== USER CONFIG ===============
SYMBOL = "BNB/USDT"
TIMEFRAME = "1m"
LOT_SIZE = 0.03
RSI_PERIOD = 14
RSI_LOW = 10
RSI_HIGH = 37

TP_POINTS = 8.0
SL_POINTS = 4.0
POLL_INTERVAL = 2
COOLDOWN_MINUTES = 15

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# Logging
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
def now_str(): return datetime.now(timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S %Z")

# Exchange Setup
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.load_markets()
try: exchange.set_leverage(75, SYMBOL)
except: pass

# State
cooldown_until = None
pending_signal = None
pending_signal_candle = None
last_entry_candle = None

# Helpers
def append_csv(row):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists: w.writeheader()
        w.writerow(row)

def rsi_wilder(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    return 100 - (100 / (1 + gain / loss))

#  🔥 MONITOR: Always returns FLAT if uncertain
def get_position_status():
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for p in positions:
            if p.get("symbol") == SYMBOL:
                size = float(p.get("contracts") or 0)
                if size > 0: return "OPEN_BUY"
                if size < 0: return "OPEN_SELL"
                return "FLAT"
        return "FLAT"
    except:
        return "FLAT"

def place_entry(side, price):
    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    return float(order.get("average") or price)

def place_tp_sl(side, entry):
    close = "sell" if side == "BUY" else "buy"
    tp = entry + TP_POINTS if side == "BUY" else entry - TP_POINTS
    sl = entry - SL_POINTS if side == "BUY" else entry + SL_POINTS
    exchange.create_order(SYMBOL, "limit", close, LOT_SIZE, tp, {"reduceOnly": True})
    exchange.create_order(SYMBOL, "stop_market", close, LOT_SIZE, None,
                         {"stopPrice": sl, "reduceOnly": True})
    return tp, sl

def log_exit(side, entry, exit_price, tp, sl):
    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE
    append_csv({"time": now_str(), "side": side, "entry": entry,
                "exit": exit_price, "pnl": round(pnl, 6)})
    print(f"[{now_str()}] EXIT {side} @ {exit_price:.6f} | PNL={pnl:.6f}")
    hit_sl = abs(exit_price - sl) < abs(exit_price - tp)
    return hit_sl

# MAIN LOOP
print(f"[{now_str()}] 🚀 BOT STARTED | FUTURES")

while True:
    try:
        pos = get_position_status()

        if pos in ("OPEN_BUY", "OPEN_SELL"):
            pending_signal = None
            time.sleep(POLL_INTERVAL)
            continue

        if cooldown_until and datetime.now(timezone.utc) < cooldown_until:
            time.sleep(POLL_INTERVAL)
            continue

        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        if len(df) < 3: time.sleep(POLL_INTERVAL); continue

        forming = df.iloc[-1]
        closed = df.iloc[-2]
        prev = df.iloc[-3]
        forming_time = int(forming.time)
        closed_time = int(closed.time)

        r_prev, r_curr = prev.rsi, closed.rsi
        price = float(closed.close)

        # 🔥 Execute pending signal at NEXT candle open
        if pending_signal:
            if forming_time > pending_signal_candle:
                entry = place_entry(pending_signal, float(forming.open))
                tp, sl = place_tp_sl(pending_signal, entry)
                last_entry_candle = pending_signal_candle
                pending_signal = None

                print(f"[{now_str()}] ENTER {entry:.6f} | TP={tp:.6f} SL={sl:.6f}")

                while True:
                    if get_position_status() == "FLAT":
                        ticker = exchange.fetch_ticker(SYMBOL)
                        exit_price = float(ticker.get("last"))
                        if log_exit("BUY" if entry < exit_price else "SELL", entry, exit_price, tp, sl):
                            cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
                        break
                    time.sleep(1)
            time.sleep(POLL_INTERVAL)
            continue

        # ---------------- SIGNAL LOGIC ----------------
        if not pd.isna(r_prev) and not pd.isna(r_curr):
            sig = None
            if r_prev < RSI_LOW and r_curr > RSI_LOW:
                sig = "BUY"
            elif r_prev > RSI_HIGH and r_curr < RSI_HIGH:
                sig = "SELL"

            if sig and last_entry_candle != closed_time:
                pending_signal = sig
                pending_signal_candle = closed_time
                print(f"[{now_str()}] SIGNAL QUEUED {sig} | Close={price:.6f}")

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("Bot stopped manually")
        break
    except Exception as e:
        print(f"⚠ Error: {e}")
        time.sleep(3)
