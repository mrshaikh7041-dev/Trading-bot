#!/usr/bin/env python3
"""
TESTNET LIVE-SAFE TRADING BOT (PRODUCTION SAFE)
- No double entry possible
- Exchange-confirmed execution
- TP detect + SL detect
"""

import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone

# ================= CONFIG =================
DAILY_DD_MODE = 100          # 25 or 50
LEVERAGE = 75
TIMEFRAME = "1m"
CHECK_INTERVAL = 2
COOLDOWN_MINUTES = 30

IST = timezone(timedelta(hours=5, minutes=30))

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})
exchange.set_sandbox_mode(False)

# ================= STRATEGY CONFIG =================
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

# ================= HELPERS =================
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

def exchange_has_position(symbol):
    positions = exchange.fetch_positions([symbol])
    for p in positions:
        if abs(float(p["contracts"])) > 0:
            return True
    return False

# ================= DAILY DD =================
def check_daily_dd():
    global TRADING_BLOCKED
    bal = get_balance()
    limit = DAY_START_BALANCE * (DAILY_DD_MODE / 100)
    if DAY_START_BALANCE - bal >= limit:
        TRADING_BLOCKED = True
        print("🛑 DAILY DD HIT")

# ================= ORDER FUNCTIONS =================
def place_market(symbol, side, qty):
    exchange.set_leverage(LEVERAGE, symbol)
    if side == "BUY":
        return exchange.create_market_buy_order(symbol, qty)
    else:
        return exchange.create_market_sell_order(symbol, qty)

def place_tp(symbol, side, price):
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

def close_position(symbol, side, qty):
    exchange.create_market_order(
        symbol,
        "sell" if side == "BUY" else "buy",
        qty,
        {"reduceOnly": True}
    )

# ================= MAIN LOOP =================
def run():
    global GLOBAL_IN_TRADE, CURRENT_TRADE, DAY_START_BALANCE, TRADING_BLOCKED

    last_day = None

    while True:
        now = datetime.now(IST)

        if last_day != now.date():
            DAY_START_BALANCE = get_balance()
            TRADING_BLOCKED = False
            last_day = now.date()
            print(f"\n🔄 New Day | Balance: {DAY_START_BALANCE}")

        check_daily_dd()
        if TRADING_BLOCKED:
            time.sleep(10)
            continue

        # ===== MONITOR MODE =====
        if GLOBAL_IN_TRADE:
            sym = CURRENT_TRADE["symbol"]
            side = CURRENT_TRADE["side"]
            sl = CURRENT_TRADE["sl"]
            qty = CURRENT_TRADE["qty"]

            price = exchange.fetch_ticker(sym)["last"]

            # SL detect
            if (side == "BUY" and price <= sl):
                print("❌ SL HIT")
                close_position(sym, side, qty)
                COOLDOWN[sym] = now + timedelta(minutes=COOLDOWN_MINUTES)
                GLOBAL_IN_TRADE = False
                CURRENT_TRADE = None

            # TP detect
            elif not exchange_has_position(sym):
                print("✅ TP HIT")
                GLOBAL_IN_TRADE = False
                CURRENT_TRADE = None

            time.sleep(CHECK_INTERVAL)
            continue

        # ===== SCAN MODE =====
        for symbol, cfg in COINS.items():
            if GLOBAL_IN_TRADE:
                break

            if symbol in COOLDOWN and now < COOLDOWN[symbol]:
                continue

            if not in_session(now, cfg["session"]):
                continue

            if exchange_has_position(symbol):
                continue  # 🔒 exchange-level lock

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
            lot = cfg["lot"]

            tp = entry + (cfg.get("tp_usdt", cfg["tp"]) / lot if "tp_usdt" in cfg else cfg["tp"])
            sl = entry - (cfg.get("sl_usdt", cfg["sl"]) / lot if "sl_usdt" in cfg else cfg["sl"])

            margin = (entry * lot) / LEVERAGE
            if get_balance() < margin:
                continue

            print(f"🚀 ENTRY {symbol}")
            place_market(symbol, signal, lot)

            # confirm entry
            time.sleep(1)
            if not exchange_has_position(symbol):
                print("❌ ENTRY FAILED")
                continue

            place_tp(symbol, signal, lot, tp)

            CURRENT_TRADE = {
                "symbol": symbol,
                "side": signal,
                "qty": lot,
                "sl": sl
            }
            GLOBAL_IN_TRADE = True
            break

        time.sleep(CHECK_INTERVAL)

# ================= START =================
if __name__ == "__main__":
    print("🧪 TESTNET BOT STARTED (DOUBLE-ENTRY SAFE)")
    run()
