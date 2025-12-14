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
TIMEFRAME = "3m"

LOT_SIZE = 0.01

RSI_PERIOD = 14
TP_POINTS = 7.0
SL_POINTS = 4.0

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
log = logging.getLogger("BNB-BOT")

# ========== TIME HELPERS ==========
IST = timezone(timedelta(hours=5, minutes=30))

def now_str():
    return datetime.now(timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")

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

# ========== INDICATORS ==========
def rsi(series, period=14):
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100 / (1 + up / down))

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# ========== HELPERS ==========
def fetch_position_size():
    try:
        bal = exchange.fetch_balance()
        for p in bal["info"]["positions"]:
            if p["symbol"] == FUT_SYMBOL:
                return float(p["positionAmt"])
    except Exception as e:
        log.error("fetch_position_size error: %s", e)
    return 0.0

# ========== ORDER FUNCTIONS ==========
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
        SYMBOL, "STOP_MARKET", close_side, LOT_SIZE, None,
        {"stopPrice": sl, "reduceOnly": True, "workingType": "MARK_PRICE"}
    )

    return entry, tp, sl

# ========== POSITION CLOSE ==========
def on_position_closed(exit_price):
    global in_position, current_position, cooldown_until_utc

    side = current_position["side"]
    entry = current_position["entry"]

    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE

    # TP vs SL
    if side == "BUY":
        result = "TP" if exit_price > entry else "SL"
    else:
        result = "TP" if exit_price < entry else "SL"

    if result == "SL":
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        print(f"[{now_str()}] ❌ SL HIT | Cooldown {COOLDOWN_MINUTES} min", flush=True)
        log.info(f"SL HIT | Cooldown {COOLDOWN_MINUTES} min")
    else:
        cooldown_until_utc = None
        print(f"[{now_str()}] ✅ TP HIT | No cooldown", flush=True)
        log.info("TP HIT | No cooldown")

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.2f} | PNL={pnl:.4f}", flush=True)
    log.info(f"EXIT {side} @ {exit_price:.2f} RESULT={result} PNL={pnl:.4f}")

    in_position = False
    current_position = None

# ========== START ==========
print(f"[{now_str()}] 🚀 BNB 7-POINT BOT STARTED (SL-ONLY COOLDOWN)")
log.info("BOT STARTED (SL-ONLY COOLDOWN)")

# ========== MAIN LOOP ==========
while True:
    try:
        log.info("BOT LOOP ALIVE")
        now_utc = datetime.now(timezone.utc)

        # ---- MONITOR POSITION ----
        if in_position:
            if abs(fetch_position_size()) < 1e-8:
                price = exchange.fetch_ticker(SYMBOL)["last"]
                on_position_closed(price)
            time.sleep(POLL_INTERVAL)
            continue

        # ---- COOLDOWN ----
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            remain = (cooldown_until_utc - now_utc).total_seconds() / 60
            print(f"[{now_str()}] 🧊 Cooldown active {remain:.1f} min", flush=True)
            log.info(f"Cooldown active {remain:.1f} min")
            time.sleep(POLL_INTERVAL)
            continue

        # ---- TREND (5m) ----
        htf = pd.DataFrame(
            exchange.fetch_ohlcv(SYMBOL, "5m", limit=60),
            columns=["t","o","h","l","c","v"]
        )
        htf["ema21"] = ema(htf["c"], 21)
        htf["ema50"] = ema(htf["c"], 50)

        trend_up = htf["ema21"].iloc[-2] > htf["ema50"].iloc[-2]
        trend_down = htf["ema21"].iloc[-2] < htf["ema50"].iloc[-2]

        # ---- ENTRY TF (3m) ----
        df = pd.DataFrame(
            exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=60),
            columns=["t","o","h","l","c","v"]
        )
        df["ema21"] = ema(df["c"], 21)
        df["rsi"] = rsi(df["c"], RSI_PERIOD)

        price = df.iloc[-2]["c"]
        ema21_price = df.iloc[-2]["ema21"]
        rsi_val = df.iloc[-2]["rsi"]
        next_open = df.iloc[-1]["o"]

        signal = None

        if trend_up and ema21_price * 0.998 <= price <= ema21_price * 1.002 and 38 <= rsi_val <= 45:
            signal = "BUY"
        elif trend_down and ema21_price * 0.998 <= price <= ema21_price * 1.002 and 55 <= rsi_val <= 62:
            signal = "SELL"

        if signal:
            entry, tp, sl = place_entry_with_tp_sl(signal, next_open)
            in_position = True
            current_position = {"side": signal, "entry": entry}
            print(f"[{now_str()}] 🚀 ENTER {signal} @ {entry:.2f} | TP={tp:.2f} SL={sl:.2f}", flush=True)
            log.info(f"ENTER {signal} @ {entry:.2f} TP={tp:.2f} SL={sl:.2f}")

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("Bot stopped")
        log.info("BOT STOPPED MANUALLY")
        break
    except Exception as e:
        print("Loop error:", e)
        log.error("Loop error: %s", e)
        time.sleep(3)
