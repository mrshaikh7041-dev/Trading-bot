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

TP_POINTS = 0.025
SL_POINTS = 0.016

EMA_FAST = 50
EMA_SLOW = 200

POLL_INTERVAL = 5
COOLDOWN_MINUTES = 15

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"

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

# ========== SAFE FETCHES ==========
def safe_fetch_ohlcv():
    """Prevents NoneType crashes"""
    while True:
        try:
            data = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=250)
            if not data or len(data) < 10:
                print("⚠️ No candle data, retrying...")
                time.sleep(1)
                continue
            return data
        except Exception as e:
            print(f"⚠️ OHLC fetch error: {e}")
            time.sleep(1)

def safe_ticker():
    """Prevents crash when ticker is None"""
    try:
        t = exchange.fetch_ticker(SYMBOL)
        if t is None:
            return None
        if t.get("last") is None:
            return None
        return t
    except:
        return None

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
    global initial_balance
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
        return 0.0
    return 0.0

# ========== ENTRY VALIDATION ==========
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

# ========== ORDER PLACEMENT ==========
def place_entry(side):
    """Safe entry + safe TP/SL placement"""
    close_side = "sell" if side=="BUY" else "buy"

    # Market entry
    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    entry = float(order["average"])

    tp = entry + TP_POINTS if side=="BUY" else entry - TP_POINTS
    sl = entry - SL_POINTS if side=="BUY" else entry + SL_POINTS

    # SAFE TP
    try:
        exchange.create_order(
            SYMBOL, "limit", close_side, LOT_SIZE, tp,
            {"reduceOnly": True, "timeInForce": "GTC"}
        )
    except Exception as e:
        print("⚠️ TP order failed:", e)

    # SAFE SL
    try:
        exchange.create_order(
            SYMBOL, "stop", close_side, LOT_SIZE, None,
            {"stopPrice": sl, "reduceOnly": True}
        )
    except Exception as e:
        print("⚠️ SL order failed:", e)

    return entry, tp, sl

# ========== EXIT HANDLING ==========
def on_position_closed(exit_price):
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit

    side = current_position["side"]
    entry = current_position["entry"]

    pnl = (exit_price-entry)*LOT_SIZE if side=="BUY" else (entry-exit_price)*LOT_SIZE

    if pnl < 0:
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
    else:
        wait_for_zone_exit = True

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f}")

    # Clean leftover orders
    for o in exchange.fetch_open_orders(SYMBOL):
        try:
            exchange.cancel_order(o["id"], SYMBOL)
        except:
            pass

    in_position = False
    current_position = None

# ========== MAIN LOOP ==========
print(f"[{now_str()}] 🚀 OPTION-C BOT STARTED (Fixed Version)")
show_balance()

while True:
    try:
        show_balance()

        # === POSITION MONITOR ===
        if in_position:
            if abs(fetch_position_size()) == 0:
                t = safe_ticker()
                if t:
                    exit_price = float(t["last"])
                    on_position_closed(exit_price)
            time.sleep(POLL_INTERVAL)
            continue

        # === SAFE OHLC FETCH ===
        ohlc = safe_fetch_ohlcv()
        df = pd.DataFrame(ohlc, columns=["t","o","h","l","c","v"])

        # === INDICATORS ===
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

        # === WAIT AFTER TP ===
        if wait_for_zone_exit:
            if RSI_LOW < last_rsi < RSI_HIGH:
                time.sleep(POLL_INTERVAL)
                continue
            else:
                wait_for_zone_exit = False

        # === SIGNAL ===
        signal = None
        if prev_rsi < RSI_LOW and RSI_LOW < last_rsi < RSI_HIGH and ema_fast > ema_slow:
            signal = "BUY"
        elif prev_rsi > RSI_HIGH and RSI_LOW < last_rsi < RSI_HIGH and ema_fast < ema_slow:
            signal = "SELL"

        # === EXECUTE ENTRY ===
        if signal and can_enter_trade():
            entry, tp, sl = place_entry(signal)
            in_position = True
            current_position = {"side":signal,"entry":entry}
            print(f"[{now_str()}] 🚀 ENTER {signal} @ {entry:.4f} | TP {tp:.4f} | SL {sl:.4f}")

        time.sleep(POLL_INTERVAL)

    except Exception as e:
        print(f"⚠️ ERROR: {e}")
        time.sleep(2)
