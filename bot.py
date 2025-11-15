#!/usr/bin/env python3
"""
LIVE-ONLY Binance Futures RSI Bot
Flexible Quantity Mode (no qty restrictions)
"""

import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import sys
import logging
import math

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/ubuntu/Trading-bot/bnb_rsi_bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.info

# ===== USER CONFIG =====
API_KEY = "YOUR_REAL_API_KEY"
API_SECRET = "YOUR_REAL_API_SECRET"

SYMBOL = "BNB/USDT"
LOT_SIZE = 0.01  # 👈 You control this. Flexible. No checks.

TIMEFRAME = "1m"

RSI_PERIOD = 14
RSI_LOW = 20
RSI_HIGH = 60

TP_POINTS = 6.0
SL_POINTS = 3.0

COOLDOWN_MINUTES = 20
POLL_INTERVAL = 1
LEVERAGE = 75

# ===== TIMEZONE =====
IST = timezone(timedelta(hours=5, minutes=30))
def now():
    return datetime.now(timezone.utc).astimezone(IST)

# ===== EXCHANGE INIT =====
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})
exchange.load_markets()

# ===== SET LEVERAGE =====
try:
    exchange.set_leverage(LEVERAGE, SYMBOL)
except Exception as e:
    log(f"[LEV_WARN] {e}")

# ===== SEED DATA =====
def fetch():
    bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=300)
    df = pd.DataFrame(bars, columns=["time","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert(IST)
    return df

df = fetch()

# ===== RSI WILDER =====
def rsi():
    close = df["close"]
    delta = close.diff()
    gain = delta.where(delta>0,0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss = (-delta.where(delta<0,0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs = gain/loss
    return float(100 - 100/(1+rs).iloc[-1])

# ===== STATE =====
in_position = False
entry_side = None
cooldown_until = None

# ===== MAIN LOOP =====
log("🚀 LIVE MODE ON — FLEXIBLE QTY ⚠ REAL FUNDS")
while True:
    try:
        raw = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=2)
        k = raw[-1]
        df.loc[len(df)] = [
            pd.to_datetime(k[0],unit='ms',utc=True).tz_convert(IST),
            k[1], k[2], k[3], k[4], k[5]
        ]
        if len(df)>500: df = df.iloc[-400:]

        r = rsi()
        price = float(df.iloc[-1]["open"])

        if not in_position:

            if cooldown_until and now() < cooldown_until:
                time.sleep(POLL_INTERVAL)
                continue

            if r < RSI_LOW:
                entry_side = "BUY"
            elif r > RSI_HIGH:
                entry_side = "SELL"
            else:
                time.sleep(POLL_INTERVAL)
                continue

            qty = LOT_SIZE  # 👈 direct use — no filter, no rounding
            if entry_side == "BUY":
                tp = price + TP_POINTS
                sl = price - SL_POINTS
            else:
                tp = price - TP_POINTS
                sl = price + SL_POINTS

            try:
                exchange.create_order(SYMBOL, "market", entry_side, qty)
                in_position = True
                log(f"[ENTER] {entry_side} qty={qty} @ {price}")
            except Exception as e:
                log(f"[ENTRY_REJECTED] {e}")
                entry_side = None
                time.sleep(POLL_INTERVAL)
                continue

            try:
                exchange.create_order(
                    SYMBOL, "limit",
                    "SELL" if entry_side=="BUY" else "BUY",
                    qty, tp,
                    {"reduceOnly": True}
                )
                log(f"[TP_SET] {tp}")
            except: pass

            try:
                exchange.create_order(
                    SYMBOL, "STOP_MARKET",
                    "SELL" if entry_side=="BUY" else "BUY",
                    qty, None,
                    {"stopPrice": sl, "reduceOnly": True}
                )
                log(f"[SL_SET] {sl}")
            except: pass

        else:
            try:
                pos = exchange.fetch_positions([SYMBOL])[0]
                size = abs(float(pos.get("contracts") or pos.get("positionAmt") or 0))
                if size < 1e-8:
                    in_position = False
                    entry_side = None
                    cooldown_until = now() + timedelta(minutes=COOLDOWN_MINUTES)
                    log("[EXIT] Position closed")
            except Exception as e:
                log(f"[POS_CHECK_ERR] {e}")

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log("🛑 STOPPED")
        break
    except Exception as e:
        log(f"[ERROR] {e}")
        time.sleep(3)
