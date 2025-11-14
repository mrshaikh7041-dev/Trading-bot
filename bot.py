#!/usr/bin/env python3
"""
LIVE-ONLY Binance Futures RSI Bot
- Only BNB/USDT
- One open position per symbol
- TP/SL reduceOnly
- Market entry at NEXT candle
- Leverage auto-set
"""

import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta, timezone
import sys
import logging
import math

# ========== LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/ubuntu/Trading-bot/bnb_rsi_bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.info

# ========== CONFIG ==========
API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

SYMBOL = "BNB/USDT"
LOT_SIZE = 0.02  # BASE quantity
TIMEFRAME = "1m"

RSI_PERIOD = 14
RSI_LOW = 20
RSI_HIGH = 60

TP_POINTS = 6.0
SL_POINTS = 3.0

COOLDOWN_MINUTES = 20
POLL_INTERVAL = 1
LEVERAGE = 75

# ========== TIME ==========
IST = timezone(timedelta(hours=5, minutes=30))
def now():
    return datetime.now(timezone.utc).astimezone(IST)

# ========== EXCHANGE INIT ==========
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# ========== PRECISION HELPERS ==========
def round_qty(q):
    info = exchange.markets[SYMBOL]
    prec = info["precision"]["amount"]
    return float(round(q, prec))

def set_leverage():
    try:
        exchange.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        log(f"[LEV_ERR] {e}")

# ========== FETCH HISTORY ==========
def fetch(seed=1000):
    bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=seed)
    df = pd.DataFrame(bars, columns=["time","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert(IST)
    return df

df = fetch()

# ========== RSI ==========
def rsi():
    close = df["close"]
    delta = close.diff()
    gain = delta.where(delta>0,0).ewm(span=RSI_PERIOD, adjust=False).mean()
    loss = (-delta.where(delta<0,0)).ewm(span=RSI_PERIOD, adjust=False).mean()
    rs = gain/loss
    return float((100 - 100/(1+rs)).iloc[-1])

# ========== POSITION STATE ==========
in_position = False
entry_side = None
cooldown_until = None

# ========== MAIN LOOP ==========
log("🚀 LIVE MODE ENABLED - REAL MONEY ⚠️")
set_leverage()

while True:
    try:
        # fetch most recent candle
        raw = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=2)
        k = raw[-1]
        df.loc[len(df)] = [
            pd.to_datetime(k[0],unit='ms',utc=True).tz_convert(IST),
            k[1], k[2], k[3], k[4], k[5]
        ]
        if len(df)>1500: df = df.iloc[-1200:]

        current_rsi = rsi()
        price = float(df.iloc[-1]["open"])

        if not in_position:
            if cooldown_until and now() < cooldown_until:
                time.sleep(POLL_INTERVAL)
                continue

            if current_rsi < RSI_LOW:
                entry_side = "BUY"
            elif current_rsi > RSI_HIGH:
                entry_side = "SELL"
            else:
                time.sleep(POLL_INTERVAL)
                continue

            qty = round_qty(LOT_SIZE)
            if qty <= 0:
                log(f"[QTY_ERR] Invalid qty {qty}")
                continue

            # Calculate TP/SL
            if entry_side == "BUY":
                tp = price + TP_POINTS
                sl = price - SL_POINTS
            else:
                tp = price - TP_POINTS
                sl = price + SL_POINTS

            # MARKET ENTRY
            try:
                exchange.create_order(SYMBOL, "market", entry_side, qty)
                in_position = True
                log(f"[ENTER] {entry_side} qty={qty} @ {price}")
            except Exception as e:
                log(f"[ENTRY_ERR] {e}")
                time.sleep(POLL_INTERVAL)
                continue

            # TP reduceOnly
            try:
                exchange.create_order(
                    SYMBOL, "limit",
                    "SELL" if entry_side=="BUY" else "BUY",
                    qty, tp,
                    {'reduceOnly':True}
                )
                log(f"[TP_SET] {tp}")
            except: pass

            # SL reduceOnly
            try:
                exchange.create_order(
                    SYMBOL, "STOP_MARKET",
                    "SELL" if entry_side=="BUY" else "BUY",
                    qty, None,
                    {'stopPrice':sl,'reduceOnly':True}
                )
                log(f"[SL_SET] {sl}")
            except: pass

        else:
            # Check Binance for open positions
            try:
                pos = exchange.fetch_positions([SYMBOL])[0]
                size = float(pos.get("contracts") or pos.get("positionAmt") or 0)
                if abs(size) < 1e-8:
                    in_position = False
                    entry_side = None
                    cooldown_until = now() + timedelta(minutes=COOLDOWN_MINUTES)
                    log("[EXITED] Position closed")
            except:
                pass

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log("🛑 Stopped by user")
        break
    except Exception as e:
        log(f"[LOOP_ERR] {e}")
        time.sleep(3)
