#!/usr/bin/env python3
"""
FINAL TESTNET LIVE-SAFE TRADING BOT
Built strictly from UNIVERSAL BACKTEST logic
"""

import ccxt
import pandas as pd
import time
import traceback
from datetime import datetime, timedelta, timezone

# ================= USER CONFIG =================
DAILY_DD_MODE = 50            # 25 or 50
LEVERAGE = 75
TIMEFRAME = "1m"
CHECK_INTERVAL = 2
COOLDOWN_MINUTES = 30

IST = timezone(timedelta(hours=5, minutes=30))

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# ================= EXCHANGE ===================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})
exchange.set_sandbox_mode(False)

# ================= STRATEGIES =================
COINS = {
    "BNB/USDT": {
        "strategy": "BOLLINGER",
        "session": "ASIA",
        "lot": 0.01,
        "tp": 7.8,
        "sl": 4.0
    },
    "AVAX/USDT": {
        "strategy": "RSI",
        "session": "LONDON",
        "lot": 1,
        "rsi_low": 10,
        "tp": 0.32,
        "sl": 0.16
    },
    "XRP/USDT": {
        "strategy": "FIXED",
        "session": "ASIA",
        "lot": 15,
        "tp_usdt": 0.32,
        "sl_usdt": 0.16
    }
}

# ================= GLOBAL STATE =================
GLOBAL_IN_TRADE = False
CURRENT_TRADE = None
DAY_START_BALANCE = None
TRADING_BLOCKED = False
COOLDOWN = {}

# ================= HELPERS =====================
def log(msg):
    print(f"{datetime.now(IST)} | {msg}", flush=True)

def in_session(ts, session):
    h = ts.hour
    return (0 <= h < 8) if session == "ASIA" else (8 <= h < 16)

def get_balance():
    return exchange.fetch_balance()["total"]["USDT"]

def fetch_df(symbol, limit=120):
    df = pd.DataFrame(
        exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=limit),
        columns=["time","open","high","low","close","volume"]
    )
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert(IST)
    return df

def rsi(series, p=14):
    d = series.diff()
    up = d.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    dn = -d.clip(upper=0).ewm(alpha=1/p, adjust=False).mean()
    return 100 - (100 / (1 + up/dn))

def bollinger(df):
    m = df["close"].rolling(20).mean()
    s = df["close"].rolling(20).std()
    df["upper"] = m + 1.5 * s
    df["lower"] = m - 1.5 * s
    return df

def exchange_has_position():
    positions = exchange.fetch_positions()
    for p in positions:
        if abs(float(p.get("contracts", 0))) > 0:
            return True
    return False

# ================= DAILY DD =====================
def check_daily_dd():
    global TRADING_BLOCKED
    bal = get_balance()
    limit = DAY_START_BALANCE * (DAILY_DD_MODE / 100)
    if DAY_START_BALANCE - bal >= limit:
        TRADING_BLOCKED = True
        log("🛑 DAILY DD HIT — Trading blocked till next day")

# ================= ORDERS ======================
def place_market(symbol, side, qty):
    exchange.set_leverage(LEVERAGE, symbol)
    return exchange.create_market_buy_order(symbol, qty)

def place_tp(symbol, side, price):
    exchange.create_order(
        symbol=symbol,
        type="TAKE_PROFIT_MARKET",
        side="sell",
        amount=None,
        price=None,
        params={
            "stopPrice": price,
            "closePosition": True,
            "workingType": "MARK_PRICE"
        }
    )
    log(f"🎯 TP placed @ {price}")

def close_position(symbol, qty):
    exchange.create_market_sell_order(symbol, qty, {"reduceOnly": True})

# ================= MAIN LOOP ===================
def run():
    global GLOBAL_IN_TRADE, CURRENT_TRADE
    global DAY_START_BALANCE, TRADING_BLOCKED

    last_day = None
    log(f"Connected | Balance: {get_balance()}")

    while True:
        try:
            now = datetime.now(IST)

            # ---- Daily reset ----
            if last_day != now.date():
                DAY_START_BALANCE = get_balance()
                TRADING_BLOCKED = False
                last_day = now.date()
                log(f"🔄 New Day | Balance: {DAY_START_BALANCE}")

            check_daily_dd()
            if TRADING_BLOCKED:
                time.sleep(10)
                continue

            # ========== MONITOR ==========
            if GLOBAL_IN_TRADE:
                sym = CURRENT_TRADE["symbol"]
                sl = CURRENT_TRADE["sl"]
                qty = CURRENT_TRADE["qty"]

                price = exchange.fetch_ticker(sym)["last"]

                if price <= sl:
                    log(f"❌ SL HIT {sym}")
                    close_position(sym, qty)
                    COOLDOWN[sym] = now + timedelta(minutes=COOLDOWN_MINUTES)
                    GLOBAL_IN_TRADE = False
                    CURRENT_TRADE = None

                elif not exchange_has_position():
                    log(f"✅ TP HIT {sym}")
                    GLOBAL_IN_TRADE = False
                    CURRENT_TRADE = None

                time.sleep(CHECK_INTERVAL)
                continue

            # ========== SCAN ==========
            if exchange_has_position():
                time.sleep(CHECK_INTERVAL)
                continue

            for symbol, cfg in COINS.items():
                if GLOBAL_IN_TRADE:
                    break

                if symbol in COOLDOWN and now < COOLDOWN[symbol]:
                    continue

                if not in_session(now, cfg["session"]):
                    continue

                df = fetch_df(symbol)
                df["rsi"] = rsi(df["close"])

                signal = None
                if cfg["strategy"] == "BOLLINGER":
                    df = bollinger(df)
                    if df.iloc[-2]["low"] <= df.iloc[-2]["lower"]:
                        signal = "BUY"

                elif cfg["strategy"] == "RSI":
                    if df.iloc[-3]["rsi"] < cfg["rsi_low"] and df.iloc[-2]["rsi"] > cfg["rsi_low"]:
                        signal = "BUY"

                elif cfg["strategy"] == "FIXED":
                    signal = "BUY"

                if not signal:
                    continue

                entry = df.iloc[-1]["open"]
                qty = cfg["lot"]

                if "tp_usdt" in cfg:
                    tp = entry + cfg["tp_usdt"] / qty
                    sl = entry - cfg["sl_usdt"] / qty
                else:
                    tp = entry + cfg["tp"]
                    sl = entry - cfg["sl"]

                margin = (entry * qty) / LEVERAGE
                if get_balance() < margin:
                    continue

                log(f"🚀 ENTRY {symbol}")
                place_market(symbol, "BUY", qty)
                time.sleep(1)

                if not exchange_has_position():
                    log("❌ Entry failed")
                    continue

                place_tp(symbol, "BUY", tp)

                CURRENT_TRADE = {
                    "symbol": symbol,
                    "qty": qty,
                    "sl": sl
                }
                GLOBAL_IN_TRADE = True
                break

            time.sleep(CHECK_INTERVAL)

        except Exception:
            log("⚠️ ERROR — continuing")
            traceback.print_exc()
            time.sleep(5)

# ================= START =====================
if __name__ == "__main__":
    log("🧪 FINAL TESTNET BOT STARTED")
    run()
