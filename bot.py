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

TP_POINTS = 7.80
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

# ========== INDICATORS ==========

def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
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

def place_entry_with_tp_sl(side: str, approx_price: float):
    close_side = "sell" if side == "BUY" else "buy"

    # ---- ENTRY ----
    order = exchange.create_order(
        SYMBOL,
        "market",
        side.lower(),
        LOT_SIZE
    )

    entry_price = float(order.get("average") or order.get("price") or approx_price)

    if side == "BUY":
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    # ---- TAKE PROFIT (LIMIT) ----
    exchange.create_order(
        SYMBOL,
        "limit",
        close_side,
        LOT_SIZE,
        tp_price,
        {
            "reduceOnly": True,
            "timeInForce": "GTC"
        }
    )

    # ---- STOP LOSS (STOP_MARKET - FIXED) ----
    exchange.create_order(
        SYMBOL,
        "STOP_MARKET",
        close_side,
        LOT_SIZE,
        None,
        {
            "stopPrice": sl_price,
            "reduceOnly": True,
            "workingType": "MARK_PRICE"
        }
    )

    return entry_price, tp_price, sl_price

# ========== POSITION CLOSE ==========

def on_position_closed(exit_price):
    global in_position, current_position, cooldown_until_utc

    side = current_position["side"]
    entry = current_position["entry"]

    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE

    result = "TP" if (
        (side == "BUY" and exit_price > entry) or
        (side == "SELL" and exit_price < entry)
    ) else "SL"

    if result == "SL":
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        print(f"[{now_str()}] ❌ SL HIT | Cooldown {COOLDOWN_MINUTES} min", flush=True)
    else:
        cooldown_until_utc = None
        print(f"[{now_str()}] ✅ TP HIT | No cooldown", flush=True)

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.2f} | PNL={pnl:.4f}", flush=True)
    log.info(f"EXIT {side} @ {exit_price:.2f} RESULT={result} PNL={pnl:.4f}")

    in_position = False
    current_position = None

# ========== STARTUP ==========

print(f"[{now_str()}] 🚀 BOT STARTED (EMA + RSI STRATEGY)")
log.info("BOT STARTED")

# ========== MAIN LOOP ==========

while True:
    try:
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
        df["rsi"] = rsi_wilder(df["c"], RSI_PERIOD)

        price = df.iloc[-2]["c"]
        ema21_price = df.iloc[-2]["ema21"]
        rsi_val = df.iloc[-2]["rsi"]
        next_open = df.iloc[-1]["o"]

        signal = None

        if trend_up and ema21_price * 0.998 <= price <= ema21_price * 1.002 and 38 <= rsi_val <= 45:
            signal = "BUY"
            print(f"[{now_str()}] 📈 BUY signal", flush=True)

        elif trend_down and ema21_price * 0.998 <= price <= ema21_price * 1.002 and 55 <= rsi_val <= 62:
            signal = "SELL"
            print(f"[{now_str()}] 📉 SELL signal", flush=True)

        if signal:
            entry, tp, sl = place_entry_with_tp_sl(signal, next_open)
            in_position = True
            current_position = {"side": signal, "entry": entry}
            print(
                f"[{now_str()}] 🚀 ENTER {signal} @ {entry:.2f} | TP={tp:.2f} SL={sl:.2f}",
                flush=True
            )
            log.info(f"ENTER {signal} @ {entry:.2f} TP={tp:.2f} SL={sl:.2f}")

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ Bot stopped", flush=True)
        break
    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(3)
