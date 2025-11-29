#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# ================= USER CONFIG =================
SYMBOL = "XRP/USDT"
TIMEFRAME = "1m"

LOT_SIZE = 10
RSI_PERIOD = 14
RSI_LOW = 10
RSI_HIGH = 37

TP_POINTS = 0.032
SL_POINTS = 0.016
POLL_INTERVAL = 5
COOLDOWN_MINUTES = 15

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# ================= LOGGING =================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ================= TIME HELPERS =================
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")

# ================= EXCHANGE SETUP =================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.load_markets()
FUT_SYMBOL = SYMBOL.replace("/", "")

try:
    exchange.set_leverage(75, SYMBOL)
except:
    pass

# ================= STATE =================
in_position = False
current_position = None
cooldown_until_utc = None
entry_in_progress = False
last_position_check = None
wait_for_zone_exit = False

# ================= HELPERS =================
def fetch_position_size() -> float:
    try:
        balance = exchange.fetch_balance()
        positions = balance["info"]["positions"]
        for p in positions:
            if p["symbol"] == FUT_SYMBOL:
                return float(p["positionAmt"])
        return 0.0
    except Exception as e:
        log.error(f"fetch_position_size error: {e}")
        return 0.0


def append_csv(row: dict):
    exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def rsi_wilder(series: pd.Series, period=14):
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100 / (1 + (up / down)))


# ================= SIGNAL & ENTRY =================
def can_enter_trade():
    global in_position, cooldown_until_utc

    now = datetime.now(timezone.utc)

    if cooldown_until_utc and now < cooldown_until_utc:
        return False
    if in_position:
        return False

    size = fetch_position_size()
    if abs(size) > 0:
        in_position = True
        return False

    return True


def place_entry(side, price):
    close_side = "sell" if side == "BUY" else "buy"

    order = exchange.create_order(
        SYMBOL, "market", side.lower(), LOT_SIZE
    )
    entry_price = float(order["average"])

    if side == "BUY":
        tp = entry_price + TP_POINTS
        sl = entry_price - SL_POINTS
    else:
        tp = entry_price - TP_POINTS
        sl = entry_price + SL_POINTS

    exchange.create_order(SYMBOL, "limit", close_side, LOT_SIZE, tp, {"reduceOnly": True})
    exchange.create_order(SYMBOL, "stop_market", close_side, LOT_SIZE, None,
                          {"stopPrice": sl, "reduceOnly": True})

    return entry_price, tp, sl


# ================= POSITION MONITOR =================
def close_position(reason):
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit

    try:
        price = float(exchange.fetch_ticker(SYMBOL)["last"])
    except:
        price = current_position["entry"]

    entry = current_position["entry"]
    side = current_position["side"]
    qty = LOT_SIZE

    pnl = (price - entry) * qty if side == "BUY" else (entry - price) * qty

    append_csv({
        "time": current_position["time"],
        "side": side,
        "entry": entry,
        "exit": price,
        "pnl": round(pnl, 4),
        "reason": reason,
    })

    if reason != "SL":
        wait_for_zone_exit = True
    else:
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)

    for o in exchange.fetch_open_orders(SYMBOL):
        try:
            exchange.cancel_order(o["id"], SYMBOL)
        except:
            pass

    in_position = False
    return


# ================= MAIN =================
print(f"[{now_str()}] 🚀 Bot started: RSI({RSI_LOW}-{RSI_HIGH})")
log.info("Bot started")

while True:
    try:
        if in_position:
            size = fetch_position_size()
            if abs(size) == 0:
                close_position("AUTO")
            time.sleep(POLL_INTERVAL)
            continue

        candles = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD+5)
        df = pd.DataFrame(candles, columns=["t","o","h","l","c","v"])
        df["rsi"] = rsi_wilder(df["c"], RSI_PERIOD)

        prev = df.iloc[-3]
        last = df.iloc[-2]
        next_open = df.iloc[-1]["o"]
        prev_rsi = float(prev["rsi"])
        last_rsi = float(last["rsi"])

        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            time.sleep(POLL_INTERVAL)
            continue

        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False

        signal = None
        if prev_rsi < RSI_LOW and RSI_LOW < last_rsi < RSI_HIGH:
            signal = "BUY"
        elif prev_rsi > RSI_HIGH and RSI_LOW < last_rsi < RSI_HIGH:
            signal = "SELL"

        if signal and can_enter_trade():
            entry_price, tp, sl = place_entry(signal, next_open)
            in_position = True
            current_position = {
                "side": signal,
                "entry": entry_price,
                "time": now_ist().isoformat(),
            }
            print(f"[{now_str()}] 🚀 {signal} at {entry_price:.4f} | TP {tp:.4f} SL {sl:.4f}")
            log.info("Trade Entered")

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("Bot stopped manually.")
        break
    except Exception as e:
        print(f"⚠️ Error: {e}")
        time.sleep(2)
