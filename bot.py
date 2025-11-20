#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# =============== USER CONFIG ===============
SYMBOL = "XRP/USDT"
TIMEFRAME = "1m"
LOT_SIZE = 5.0
RSI_PERIOD = 14
RSI_LOW = 10
RSI_HIGH = 37

TP_POINTS = 0.032
SL_POINTS = 0.016
POLL_INTERVAL = 2
COOLDOWN_MINUTES = 15

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

def now_ist(): return datetime.now(timezone.utc).astimezone(IST)
def now_str(): return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")

exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.load_markets()

try:
    exchange.set_leverage(75, SYMBOL)
except: pass

# =============== STATE ===============
in_position = False
current_position = None
last_entry_candle_time = None
cooldown_until_utc = None

# =============== HELPERS ===============
def append_csv(row):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists: w.writeheader()
        w.writerow(row)

def rsi_wilder(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def fetch_position_size():
    try: positions = exchange.fetch_positions([SYMBOL])
    except: return 0.0
    for p in positions:
        if p.get("symbol") == SYMBOL:
            amt = p.get("contracts") or p.get("info", {}).get("positionAmt", 0)
            return float(amt)
    return 0.0

def create_tp_sl(side, entry):
    close_side = "sell" if side == "BUY" else "buy"
    tp = entry + TP_POINTS if side == "BUY" else entry - TP_POINTS
    sl = entry - SL_POINTS if side == "BUY" else entry + SL_POINTS

    tp_o = exchange.create_order(
        SYMBOL, "limit", close_side, LOT_SIZE, tp,
        {"reduceOnly": True}
    )
    sl_o = exchange.create_order(
        SYMBOL, "STOP_MARKET", close_side, LOT_SIZE, None,
        {"stopPrice": sl, "reduceOnly": True}
    )
    return tp_o["id"], sl_o["id"], tp, sl

def check_exit():
    global in_position, current_position, cooldown_until_utc
    size = fetch_position_size()
    if abs(size) < 1e-8 and in_position and current_position:
        ticker = exchange.fetch_ticker(SYMBOL)
        exit_p = float(ticker["last"])
        entry = current_position["entry"]
        side = current_position["side"]
        tp = current_position["tp"]
        sl = current_position["sl"]

        pnl = (exit_p - entry)*LOT_SIZE if side=="BUY" else (entry-exit_p)*LOT_SIZE
        hit_sl = abs(exit_p - sl) < abs(exit_p - tp)

        if hit_sl:
            cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)

        append_csv({
            "time": current_position["time"],
            "side": side,
            "entry": entry,
            "exit": exit_p,
            "pnl": round(pnl,6)
        })

        print(f"{now_str()} EXIT {side} @ {exit_p:.6f} | PNL={pnl:.6f}")
        in_position = False
        current_position = None


# =============== MAIN LOOP ===============
print(f"{now_str()} 🚀 Bot started {SYMBOL}")
while True:
    try:
        size = fetch_position_size()
        if abs(size)>1e-8 and not in_position: in_position=True
        if abs(size)<1e-8 and in_position and current_position is None: in_position=False

        if in_position: check_exit()

        if cooldown_until_utc and datetime.now(timezone.utc) < cooldown_until_utc:
            time.sleep(POLL_INTERVAL); continue

        df = pd.DataFrame(exchange.fetch_ohlcv(SYMBOL,TIMEFRAME,limit=RSI_PERIOD+5),
            columns=["time","open","high","low","close","volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        prev = df.iloc[-3]; last = df.iloc[-2]
        prev_r, curr_r = prev["rsi"], last["rsi"]
        time_candle = last["time"]
        close = float(last["close"])

        if not in_position and not pd.isna(prev_r) and not pd.isna(curr_r):
            signal=None
            if prev_r < RSI_LOW and curr_r > RSI_LOW: signal="BUY"
            if prev_r > RSI_HIGH and curr_r < RSI_HIGH: signal="SELL"

            if signal and last_entry_candle_time!=time_candle:
                if abs(fetch_position_size())>1e-8:
                    in_position=True
                else:
                    last_entry_candle_time=time_candle
                    in_position=True

                    order = exchange.create_order(SYMBOL,"market",signal.lower(),LOT_SIZE)
                    entry=float(order["average"])
                    tp_id, sl_id, tp, sl = create_tp_sl(signal, entry)

                    current_position={
                        "side":signal,"entry":entry,
                        "time":now_ist().isoformat(),
                        "tp":tp,"sl":sl
                    }

                    print(f"{now_str()} ENTER {signal} @ {entry:.6f} | TP={tp:.6f} SL={sl:.6f}")

        time.sleep(POLL_INTERVAL)

    except Exception as e:
        print(f"⚠️ {now_str()} Loop Error: {e}")
        time.sleep(3)
