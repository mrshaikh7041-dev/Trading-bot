#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# ========== USER CONFIG ==========
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

EMA_FAST = 50
EMA_SLOW = 200

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

# ========== TIME HELPERS ==========
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")

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
    log.warning("set_leverage failed: %s", e)

# ========== STATE ==========
in_position = False
current_position = None
cooldown_until_utc = None
wait_for_zone_exit = False

last_balance_check = None
balance_check_interval = 30

initial_balance = None
current_balance = None
total_profit = 0.0

# ========== HELPERS ==========

def fetch_balance_usdt():
    try:
        bal = exchange.fetch_balance()
        usdt = bal["USDT"]
        return {
            "free": float(usdt["free"]),
            "used": float(usdt["used"]),
            "total": float(usdt["total"])
        }
    except:
        return None

def show_balance():
    global initial_balance, current_balance, total_profit
    b = fetch_balance_usdt()
    if not b:
        return
    current_balance = b["total"]
    if initial_balance is None:
        initial_balance = current_balance
    total_profit = current_balance - initial_balance
    pct = (total_profit / initial_balance) * 100
    print(f"[{now_str()}] 💰 BAL: ${current_balance:.2f} | PNL: ${total_profit:+.2f} ({pct:+.2f}%)", flush=True)

def append_csv(row: dict):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)

def rsi_wilder(series, period=14):
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100 / (1 + (up / down)))

def fetch_position_size():
    try:
        bal = exchange.fetch_balance()
        for p in bal["info"]["positions"]:
            if p["symbol"] == FUT_SYMBOL:
                return float(p["positionAmt"])
    except:
        return 0.0
    return 0.0

# EMA CALC
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# ========== TRADING HELPERS ==========

def can_enter_trade():
    global in_position, cooldown_until_utc, current_position
    now_utc = datetime.now(timezone.utc)

    if cooldown_until_utc and now_utc < cooldown_until_utc:
        return False

    ex_size = fetch_position_size()
    if abs(ex_size) > 0:
        in_position = True
        return False

    if in_position and abs(ex_size) == 0:
        in_position = False
        current_position = None

    return not in_position

def place_entry_with_tp_sl(side, approx_price):
    close_side = "sell" if side == "BUY" else "buy"
    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    entry_price = float(order.get("average") or approx_price)

    tp = entry_price + TP_POINTS if side == "BUY" else entry_price - TP_POINTS
    sl = entry_price - SL_POINTS if side == "BUY" else entry_price + SL_POINTS

    exchange.create_order(SYMBOL, "limit", close_side, LOT_SIZE, tp, {"reduceOnly": True})
    exchange.create_order(SYMBOL, "stop_market", close_side, LOT_SIZE, None, {"stopPrice": sl, "reduceOnly": True})

    return entry_price, tp, sl

def safe_enter_trade(signal, next_open_price):
    global in_position, current_position
    if not can_enter_trade():
        return
    entry, tp, sl = place_entry_with_tp_sl(signal, next_open_price)
    in_position = True
    current_position = {"side": signal, "entry": entry}

def on_position_closed(exit_price):
    global in_position, current_position, wait_for_zone_exit, cooldown_until_utc
    if current_position is None:
        return
    side = current_position["side"]
    entry = current_position["entry"]
    pnl = (exit_price - entry)*LOT_SIZE if side=="BUY" else (entry - exit_price)*LOT_SIZE

    append_csv({
        "time": now_str(),
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "pnl": pnl
    })

    wait_for_zone_exit = True
    in_position = False
    current_position = None

# ========== STARTUP ==========
print(f"[{now_str()}] 🚀 BOT STARTING WITH EMA FILTER ADDED")
show_balance()

# ========== MAIN LOOP ==========
while True:
    try:
        now_utc = datetime.now(timezone.utc)
        if last_balance_check is None or (now_utc - last_balance_check).total_seconds() >= balance_check_interval:
            show_balance()
            last_balance_check = now_utc

        if in_position:
            if abs(fetch_position_size()) == 0:
                ticker = exchange.fetch_ticker(SYMBOL)
                exit_price = float(ticker.get("last"))
                on_position_closed(exit_price)
            time.sleep(POLL_INTERVAL)
            continue

        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=300)
        df = pd.DataFrame(ohlc, columns=["t","o","h","l","c","v"])

        df["rsi"] = rsi_wilder(df["c"], RSI_PERIOD)
        df["ema_fast"] = ema(df["c"], EMA_FAST)
        df["ema_slow"] = ema(df["c"], EMA_SLOW)

        prev = df.iloc[-3]
        last = df.iloc[-2]
        next_open = df.iloc[-1]["o"]

        prev_rsi = float(prev["rsi"])
        last_rsi = float(last["rsi"])
        ema_fast = float(last["ema_fast"])
        ema_slow = float(last["ema_slow"])

        # WAIT after TP
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False

        signal = None

        # === BUY CONDITION ===
        if (
            prev_rsi < RSI_LOW and 
            RSI_LOW < last_rsi < RSI_HIGH and 
            ema_fast > ema_slow
        ):
            signal = "BUY"

        # === SELL CONDITION ===
        elif (
            prev_rsi > RSI_HIGH and 
            RSI_LOW < last_rsi < RSI_HIGH and 
            ema_fast < ema_slow
        ):
            signal = "SELL"

        if signal:
            safe_enter_trade(signal, next_open)

        time.sleep(POLL_INTERVAL)

    except Exception as e:
        print(f"⚠️ ERROR: {e}")
        time.sleep(2)
