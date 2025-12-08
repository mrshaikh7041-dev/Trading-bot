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

TP_POINTS = 0.025     # ✅ OPTIMIZED TP
SL_POINTS = 0.016

EMA_FAST = 50         # ✅ TREND FILTER
EMA_SLOW = 200

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
def now_ist(): return datetime.now(timezone.utc).astimezone(IST)
def now_str(): return now_ist().strftime("%Y-%m-%d %H:%M:%S")

# ========== EXCHANGE ==========
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.load_markets()
FUT_SYMBOL = SYMBOL.replace("/", "")
exchange.set_leverage(75, SYMBOL)

# ========== STATE ==========
in_position = False
current_position = None
cooldown_until_utc = None
wait_for_zone_exit = False

initial_balance = None
last_balance_check = None

# ========== INDICATORS ==========
def rsi(series, p=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/p, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# ========== BALANCE ==========
def fetch_balance():
    try:
        b = exchange.fetch_balance()['USDT']
        return float(b['free']) + float(b['used'])
    except:
        return None

def show_balance():
    global initial_balance, last_balance_check
    bal = fetch_balance()
    if bal is None: return
    if initial_balance is None:
        initial_balance = bal
    pnl = bal - initial_balance
    pct = (pnl / initial_balance) * 100
    print(f"[{now_str()}] 💰 BAL: ${bal:.2f} | PNL: {pnl:+.2f}$ ({pct:+.2f}%)", flush=True)

# ========== POSITION SIZE ==========
def fetch_position_size():
    try:
        bal = exchange.fetch_balance()
        for p in bal["info"]["positions"]:
            if p["symbol"] == FUT_SYMBOL:
                return float(p["positionAmt"])
    except:
        pass
    return 0.0

# ========== ENTRY CHECK ==========
def can_enter_trade():
    global in_position, cooldown_until_utc
    now = datetime.now(timezone.utc)

    if cooldown_until_utc and now < cooldown_until_utc:
        return False

    size = fetch_position_size()
    if abs(size) > 0:
        in_position = True
        return False

    return not in_position

# ========== ORDER ==========
def place_entry(side, price):
    close_side = "sell" if side=="BUY" else "buy"
    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    entry = float(order["average"])

    tp = entry + TP_POINTS if side=="BUY" else entry - TP_POINTS
    sl = entry - SL_POINTS if side=="BUY" else entry + SL_POINTS

    exchange.create_order(SYMBOL, "limit", close_side, LOT_SIZE, tp, {"reduceOnly": True})
    exchange.create_order(SYMBOL, "stop_market", close_side, LOT_SIZE, None,
                          {"stopPrice": sl, "reduceOnly": True})

    return entry, tp, sl

# ========== EXIT ==========
def on_position_closed(exit_price):
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit

    side = current_position["side"]
    entry = current_position["entry"]
    pnl = (exit_price-entry)*LOT_SIZE if side=="BUY" else (entry-exit_price)*LOT_SIZE

    if pnl < 0:
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
    else:
        wait_for_zone_exit = True

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f}", flush=True)

    for o in exchange.fetch_open_orders(SYMBOL):
        try: exchange.cancel_order(o["id"], SYMBOL)
        except: pass

    in_position = False
    current_position = None

# ========== START ==========
print(f"[{now_str()}] 🚀 OPTION-C BOT STARTED")
show_balance()

# ========== MAIN LOOP ==========
while True:
    try:
        show_balance()

        if in_position:
            if abs(fetch_position_size()) == 0:
                price = float(exchange.fetch_ticker(SYMBOL)['last'])
                on_position_closed(price)
            time.sleep(POLL_INTERVAL)
            continue

        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=250)
        df = pd.DataFrame(ohlc, columns=["t","o","h","l","c","v"])

        df["rsi"] = rsi(df["c"], RSI_PERIOD)
        df["ema_fast"] = ema(df["c"], EMA_FAST)
        df["ema_slow"] = ema(df["c"], EMA_SLOW)

        prev = df.iloc[-3]
        last = df.iloc[-2]
        next_open = df.iloc[-1]["o"]

        prev_rsi = float(prev["rsi"])
        last_rsi = float(last["rsi"])
        ema_fast = float(last["ema_fast"])
        ema_slow = float(last["ema_slow"])

        # ✅ WAIT AFTER TP
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False

        signal = None

        # ✅ RSI + EMA FILTER
        if prev_rsi < RSI_LOW and RSI_LOW < last_rsi < RSI_HIGH and ema_fast > ema_slow:
            signal = "BUY"
        elif prev_rsi > RSI_HIGH and RSI_LOW < last_rsi < RSI_HIGH and ema_fast < ema_slow:
            signal = "SELL"

        if signal and can_enter_trade():
            entry, tp, sl = place_entry(signal, next_open)
            in_position = True
            current_position = {"side":signal,"entry":entry}
            print(f"[{now_str()}] 🚀 ENTER {signal} @ {entry:.4f} | TP {tp:.4f} SL {sl:.4f}", flush=True)

        time.sleep(POLL_INTERVAL)

    except Exception as e:
        print("⚠️ ERROR:", e)
        time.sleep(3)
