#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# ========== USER CONFIG ==========
SYMBOL = "BNB/USDT"
TIMEFRAME = "1m"

LOT_SIZE = 0.06

RSI_PERIOD = 14
RSI_LOW = 10
RSI_HIGH = 37

EMA_FAST = 21
EMA_SLOW = 50

TP_POINTS = 6
SL_POINTS = 3

POLL_INTERVAL = 5
COOLDOWN_MINUTES = 15

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# ========== LOGGING ==========
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ========== TIME ==========
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")

# ========== EXCHANGE ==========
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.options["adjustForTimeDifference"] = True
exchange.load_markets()

FUT_SYMBOL = SYMBOL.replace("/", "")

try:
    exchange.set_leverage(75, SYMBOL)
except Exception as e:
    log.warning("Leverage set failed: %s", e)

# ========== STATE ==========
in_position = False
current_position = None
cooldown_until_utc = None
wait_for_zone_exit = False

last_balance_check = None
balance_check_interval = 30

initial_balance = None

# ========== HELPERS ==========
def rsi_wilder(series: pd.Series, period: int):
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100 / (1 + up / down))

def ema(series: pd.Series, period: int):
    return series.ewm(span=period, adjust=False).mean()

def fetch_position_size():
    try:
        bal = exchange.fetch_balance()
        for p in bal.get("info", {}).get("positions", []):
            if p["symbol"] == FUT_SYMBOL:
                return float(p["positionAmt"])
    except Exception:
        pass
    return 0.0

def place_entry_with_tp_sl(side, approx_price):
    close_side = "sell" if side == "BUY" else "buy"

    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    entry = float(order.get("average") or approx_price)

    if side == "BUY":
        tp = entry + TP_POINTS
        sl = entry - SL_POINTS
    else:
        tp = entry - TP_POINTS
        sl = entry + SL_POINTS

    exchange.create_order(
        SYMBOL, "limit", close_side, LOT_SIZE, tp,
        {"reduceOnly": True, "timeInForce": "GTC"}
    )

    exchange.create_order(
        SYMBOL, "stop_market", close_side, LOT_SIZE, None,
        {"stopPrice": sl, "reduceOnly": True}
    )

    return entry, tp, sl

def on_position_closed(exit_price):
    global in_position, current_position, wait_for_zone_exit

    print(f"[{now_str()}] 📊 EXIT @ {exit_price:.4f}", flush=True)
    wait_for_zone_exit = True
    in_position = False
    current_position = None

# ========== START ==========
print(f"[{now_str()}] 🚀 RSI + EMA BOT STARTED")

# ========== MAIN LOOP ==========
while True:
    try:
        now_utc = datetime.now(timezone.utc)

        # --- Monitor open position ---
        if in_position:
            size = fetch_position_size()
            if abs(size) < 1e-8:
                price = exchange.fetch_ticker(SYMBOL)["last"]
                on_position_closed(price)
            time.sleep(POLL_INTERVAL)
            continue

        if cooldown_until_utc and now_utc < cooldown_until_utc:
            time.sleep(POLL_INTERVAL)
            continue

        # --- Fetch data ---
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 60)
        df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","volume"])

        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)
        df["ema_fast"] = ema(df["close"], EMA_FAST)
        df["ema_slow"] = ema(df["close"], EMA_SLOW)

        prev = df.iloc[-3]
        last = df.iloc[-2]
        entry_candle = df.iloc[-1]

        prev_rsi = float(prev["rsi"])
        last_rsi = float(last["rsi"])

        ema_fast = float(last["ema_fast"])
        ema_slow = float(last["ema_slow"])

        # --- EMA Direction ---
        ema_direction = None
        if ema_fast > ema_slow:
            ema_direction = "BUY"
        elif ema_fast < ema_slow:
            ema_direction = "SELL"

        print(
            f"[{now_str()}] RSI {prev_rsi:.2f}->{last_rsi:.2f} | "
            f"EMA21={ema_fast:.4f} EMA50={ema_slow:.4f} DIR={ema_direction}",
            flush=True
        )

        # --- Zone wait after exit ---
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            time.sleep(POLL_INTERVAL)
            continue

        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False

        # --- SIGNAL (EMA + RSI) ---
        signal = None

        if ema_direction == "BUY":
            if prev_rsi < RSI_LOW and (RSI_LOW < last_rsi < RSI_HIGH):
                signal = "BUY"

        elif ema_direction == "SELL":
            if prev_rsi > RSI_HIGH and (RSI_LOW < last_rsi < RSI_HIGH):
                signal = "SELL"

        if signal:
            entry_price, tp, sl = place_entry_with_tp_sl(signal, entry_candle["open"])
            in_position = True
            current_position = {"side": signal, "entry": entry_price}
            print(
                f"[{now_str()}] 🚀 ENTER {signal} @ {entry_price:.4f} TP={tp:.4f} SL={sl:.4f}",
                flush=True
            )

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("⛔ Bot stopped")
        break
    except Exception as e:
        print("⚠️ Error:", e)
        time.sleep(3)
