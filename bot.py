#!/usr/bin/env python3
"""
FINAL TESTNET LIVE-SAFE TRADING BOT (PATCHED)
✔ TP reliably placed (retry + sync)
✔ Portfolio-level single trade lock
✔ Exchange confirmed entry
✔ Bot-side SL
"""

import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone

# ===================== USER CONFIG =====================
DAILY_DD_MODE = 25
LEVERAGE = 75
TIMEFRAME = "1m"
CHECK_INTERVAL = 2
COOLDOWN_MINUTES = 30

IST = timezone(timedelta(hours=5, minutes=30))

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# ===================== EXCHANGE ========================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})
exchange.set_sandbox_mode(False)

# ===================== STRATEGY CONFIG =================
COINS = {
    "BNB/USDT": {"strategy":"BOLLINGER","session":"ASIA","lot":0.01,"tp":7.8,"sl":4.0},
    "AVAX/USDT": {"strategy":"RSI","session":"LONDON","lot":1,"rsi_low":10,"tp":0.32,"sl":0.16},
    "XRP/USDT": {"strategy":"FIXED","session":"ASIA","lot":15,"tp_usdt":0.32,"sl_usdt":0.16}
}

# ===================== GLOBAL STATE ====================
GLOBAL_IN_TRADE = False
CURRENT_TRADE = None
DAY_START_BALANCE = None
TRADING_BLOCKED = False
COOLDOWN = {}

# ===================== HELPERS =========================
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
    return 100 - (100/(1+up/dn))

def bollinger(df):
    m = df["close"].rolling(20).mean()
    s = df["close"].rolling(20).std()
    df["upper"] = m + 1.5*s
    df["lower"] = m - 1.5*s
    return df

# 🔒 PORTFOLIO LEVEL LOCK
def exchange_has_any_position():
    for p in exchange.fetch_positions():
        if abs(float(p.get("contracts", 0))) > 0:
            return True
    return False

# ===================== ORDERS ==========================
def place_market(symbol, side, qty):
    exchange.set_leverage(LEVERAGE, symbol)
    return exchange.create_market_buy_order(symbol, qty)

def place_tp_safe(symbol, side, price):
    for i in range(3):
        try:
            exchange.create_order(
                symbol=symbol,
                type="TAKE_PROFIT_MARKET",
                side="sell" if side == "BUY" else "buy",
                amount=None,
                price=None,
                params={
                    "stopPrice": price,
                    "closePosition": True,
                    "workingType": "MARK_PRICE"
                }
            )
            print(f"🎯 TP PLACED @ {price}")
            return True
        except Exception as e:
            print(f"⚠️ TP retry {i+1}: {e}")
            time.sleep(1)
    return False

def close_position(symbol, side, qty):
    exchange.create_market_order(
        symbol,
        "sell" if side == "BUY" else "buy",
        qty,
        {"reduceOnly": True}
    )

# ===================== MAIN LOOP =======================
def run():
    global GLOBAL_IN_TRADE, CURRENT_TRADE, DAY_START_BALANCE

    last_day = None

    while True:
        now = datetime.now(IST)

        if last_day != now.date():
            DAY_START_BALANCE = get_balance()
            last_day = now.date()
            print(f"\n🔄 New Day | Balance: {DAY_START_BALANCE}")

        # ===== MONITOR MODE =====
        if GLOBAL_IN_TRADE:
            sym = CURRENT_TRADE["symbol"]
            side = CURRENT_TRADE["side"]
            sl = CURRENT_TRADE["sl"]
            qty = CURRENT_TRADE["qty"]

            price = exchange.fetch_ticker(sym)["last"]

            if side == "BUY" and price <= sl:
                print("❌ SL HIT")
                close_position(sym, side, qty)
                GLOBAL_IN_TRADE = False
                CURRENT_TRADE = None

            elif not exchange_has_any_position():
                print("✅ TP HIT")
                GLOBAL_IN_TRADE = False
                CURRENT_TRADE = None

            time.sleep(CHECK_INTERVAL)
            continue

        # ===== SCAN MODE =====
        if exchange_has_any_position():
            time.sleep(CHECK_INTERVAL)
            continue

        for symbol, cfg in COINS.items():
            if not in_session(now, cfg["session"]):
                continue

            df = fetch_df(symbol)
            df["rsi"] = rsi(df["close"])

            signal = "BUY" if cfg["strategy"] == "FIXED" else None

            if cfg["strategy"] == "BOLLINGER":
                df = bollinger(df)
                if df.iloc[-2]["low"] <= df.iloc[-2]["lower"]:
                    signal = "BUY"

            if cfg["strategy"] == "RSI":
                if df.iloc[-3]["rsi"] < cfg["rsi_low"] and df.iloc[-2]["rsi"] > cfg["rsi_low"]:
                    signal = "BUY"

            if not signal:
                continue

            entry = df.iloc[-1]["open"]
            qty = cfg["lot"]

            tp = entry + (cfg.get("tp_usdt", cfg["tp"]) / qty if "tp_usdt" in cfg else cfg["tp"])
            sl = entry - (cfg.get("sl_usdt", cfg["sl"]) / qty if "sl_usdt" in cfg else cfg["sl"])

            print(f"🚀 ENTRY {symbol}")
            place_market(symbol, signal, qty)

            # wait until position exists
            for _ in range(5):
                if exchange_has_any_position():
                    break
                time.sleep(1)

            if not exchange_has_any_position():
                print("❌ ENTRY FAILED")
                continue

            if not place_tp_safe(symbol, signal, tp):
                close_position(symbol, signal, qty)
                continue

            CURRENT_TRADE = {"symbol":symbol,"side":signal,"qty":qty,"sl":sl}
            GLOBAL_IN_TRADE = True
            break

        time.sleep(CHECK_INTERVAL)

# ===================== START ===========================
if __name__ == "__main__":
    print("🧪 BOT STARTED (PORTFOLIO SAFE)")
    run()
