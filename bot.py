#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# ========== USER CONFIG ==========
SYMBOL = "AVAX/USDT"
TIMEFRAME = "1m"

LOT_SIZE = 1
RSI_PERIOD = 14
RSI_LOW = 10
RSI_HIGH = 37

TP_POINTS = 0.32
SL_POINTS = 0.16

EMA_FAST = 21
EMA_SLOW = 50

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
exchange.set_leverage(75, SYMBOL)

FUT_SYMBOL = SYMBOL.replace("/", "")

# ========== STATE ==========
in_position = False
current_position = None
cooldown_until_utc = None
wait_for_zone_exit = False

# ========== INDICATORS ==========
def rsi(series, p=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/p, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# ========== POSITION ==========
def fetch_position_size():
    try:
        bal = exchange.fetch_balance()
        for p in bal["info"]["positions"]:
            if p["symbol"] == FUT_SYMBOL:
                return float(p["positionAmt"])
    except:
        pass
    return 0.0

# ========== SL WATCHER (BOT LEVEL) ==========
def sl_watcher():
    if not current_position:
        return
    side = current_position["side"]
    sl = current_position["sl"]
    price = float(exchange.fetch_ticker(SYMBOL)["last"])

    if side == "BUY" and price <= sl:
        log.warning("🛑 BOT SL HIT (BUY)")
        force_close(price)

    if side == "SELL" and price >= sl:
        log.warning("🛑 BOT SL HIT (SELL)")
        force_close(price)

# ========== FORCE CLOSE ==========
def force_close(price):
    global in_position, current_position, cooldown_until_utc

    side = current_position["side"]
    close_side = "sell" if side == "BUY" else "buy"

    exchange.create_order(
        SYMBOL,
        "market",
        close_side,
        LOT_SIZE,
        {"reduceOnly": True}
    )

    log.info(f"FORCED EXIT {side} @ {price}")
    cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)

    in_position = False
    current_position = None

# ========== ENTRY ==========
def place_entry(side):
    close_side = "sell" if side == "BUY" else "buy"

    order = exchange.create_order(
        SYMBOL, "market", side.lower(), LOT_SIZE
    )
    entry = float(order.get("average") or exchange.fetch_ticker(SYMBOL)["last"])

    tp = entry + TP_POINTS if side == "BUY" else entry - TP_POINTS
    sl = entry - SL_POINTS if side == "BUY" else entry + SL_POINTS

    # TP
    exchange.create_order(
        SYMBOL, "limit", close_side, LOT_SIZE, tp,
        {"reduceOnly": True, "timeInForce": "GTC"}
    )

    # SL (correct algo endpoint)
    try:
        exchange.create_order(
            SYMBOL, "stop_market", close_side, LOT_SIZE, None,
            {"stopPrice": sl, "closePosition": True}
        )
        log.info("SL placed via exchange")
    except Exception as e:
        log.warning(f"SL exchange failed, bot watcher active: {e}")

    return entry, tp, sl

# ========== START ==========
print(f"[{now_str()}] 🚀 BOT STARTED")

# ========== MAIN LOOP ==========
while True:
    try:
        if in_position:
            sl_watcher()

            if abs(fetch_position_size()) == 0:
                price = float(exchange.fetch_ticker(SYMBOL)["last"])
                in_position = False
                current_position = None
                wait_for_zone_exit = True
                log.info(f"POSITION CLOSED @ {price}")

            time.sleep(POLL_INTERVAL)
            continue

        if cooldown_until_utc and datetime.now(timezone.utc) < cooldown_until_utc:
            time.sleep(POLL_INTERVAL)
            continue

        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=200)
        df = pd.DataFrame(ohlc, columns=["t","o","h","l","c","v"])

        df["rsi"] = rsi(df["c"], RSI_PERIOD)
        df["ema_fast"] = ema(df["c"], EMA_FAST)
        df["ema_slow"] = ema(df["c"], EMA_SLOW)

        prev = df.iloc[-3]
        last = df.iloc[-2]

        prev_rsi = float(prev["rsi"])
        last_rsi = float(last["rsi"])
        ema_fast = float(last["ema_fast"])
        ema_slow = float(last["ema_slow"])

        if wait_for_zone_exit:
            if RSI_LOW < last_rsi < RSI_HIGH:
                time.sleep(POLL_INTERVAL)
                continue
            wait_for_zone_exit = False

        signal = None
        if prev_rsi < RSI_LOW and RSI_LOW < last_rsi < RSI_HIGH and ema_fast > ema_slow:
            signal = "BUY"
        elif prev_rsi > RSI_HIGH and RSI_LOW < last_rsi < RSI_HIGH and ema_fast < ema_slow:
            signal = "SELL"

        if signal:
            entry, tp, sl = place_entry(signal)
            current_position = {
                "side": signal,
                "entry": entry,
                "tp": tp,
                "sl": sl
            }
            in_position = True
            log.info(f"ENTER {signal} @ {entry} TP {tp} SL {sl}")

        time.sleep(POLL_INTERVAL)

    except Exception as e:
        log.error(f"ERROR: {e}")
        time.sleep(2)
