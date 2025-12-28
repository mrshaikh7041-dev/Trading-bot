#!/usr/bin/env python3
import ccxt
import time
import traceback
from datetime import datetime, timedelta, timezone

# ================= CONFIG =================
API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

TIMEFRAME = "1m"
LEVERAGE = 75

TP_USDT = 0.32
SL_USDT = 0.16

CHECK_INTERVAL = 2
COOLDOWN_MINUTES = 30

DAILY_DD_LIMITS = [1.0]   # evaluated separately if you want

IST = timezone(timedelta(hours=5, minutes=30))

# ================= COINS CONFIG =================
COINS = {
    "BNB/USDT":  {"lot": 0.04, "side": "BUY",  "sessions": ["ASIA","LONDON"]},
    "XRP/USDT":  {"lot": 15,   "side": "SELL", "sessions": ["ASIA"]},
    "AVAX/USDT": {"lot": 1,    "side": "SELL", "sessions": ["ASIA","LONDON"]},
    "SOL/USDT":  {"lot": 0.1,  "side": "BUY",  "sessions": ["ASIA","LONDON"]},
}

# ================= EXCHANGE =================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "future",
    }
})

exchange.set_sandbox_mode(False)

# ================= HELPERS =================
def now_ist():
    return datetime.now(IST)

def get_session(ts):
    h = ts.hour + ts.minute / 60
    if 6 <= h < 13.5:
        return "ASIA"
    if 13.5 <= h < 18.5:
        return "LONDON"
    if 18.5 <= h < 23.5:
        return "NY"
    return None

def get_balance():
    bal = exchange.fetch_balance()
    return float(bal["USDT"]["free"])

def set_leverage(symbol, lev):
    try:
        exchange.set_leverage(lev, symbol)
    except:
        pass

def has_open_position(symbol):
    positions = exchange.fetch_positions([symbol])
    for p in positions:
        if abs(float(p["contracts"])) > 0:
            return True
    return False

def cancel_all(symbol):
    try:
        exchange.cancel_all_orders(symbol)
    except:
        pass

def get_price(symbol):
    return exchange.fetch_ticker(symbol)["last"]

# ================= TP / SL PLACEMENT =================
def place_tp_sl(symbol, side, qty, entry):
    if side == "BUY":
        tp = entry + TP_USDT / qty
        sl = entry - SL_USDT / qty
        tp_side = "sell"
        sl_side = "sell"
    else:
        tp = entry - TP_USDT / qty
        sl = entry + SL_USDT / qty
        tp_side = "buy"
        sl_side = "buy"

    # TAKE PROFIT (LIMIT REDUCE ONLY)
    exchange.create_order(
        symbol=symbol,
        type="limit",
        side=tp_side,
        amount=qty,
        price=round(tp, 6),
        params={"reduceOnly": True}
    )

    # STOP LOSS (STOP MARKET REDUCE ONLY)
    exchange.create_order(
        symbol=symbol,
        type="stop_market",
        side=sl_side,
        amount=qty,
        params={
            "stopPrice": round(sl, 6),
            "reduceOnly": True
        }
    )

# ================= MAIN =================
def main():
    print("\n🔥 TESTNET BOT STARTED")
    print("Time:", now_ist())

    balance = get_balance()
    print("Starting Balance:", balance)

    start_of_day = datetime.now(IST).date()
    day_start_balance = balance
    blocked_today = False

    for sym in COINS:
        set_leverage(sym, LEVERAGE)

    cooldown = {c: None for c in COINS}

    while True:
        try:
            now = now_ist()

            # reset daily DD
            if now.date() != start_of_day:
                start_of_day = now.date()
                day_start_balance = get_balance()
                blocked_today = False

            if blocked_today:
                time.sleep(5)
                continue

            for symbol, cfg in COINS.items():
                session = get_session(now)
                if session not in cfg["sessions"]:
                    continue

                if cooldown[symbol] and now < cooldown[symbol]:
                    continue

                if has_open_position(symbol):
                    continue

                balance = get_balance()
                if balance <= 0:
                    print("❌ Balance zero. Stopping bot.")
                    return

                # daily DD
                if (day_start_balance - balance) >= day_start_balance * 0.5:
                    print("⚠️ Daily DD hit. Trading paused.")
                    blocked_today = True
                    continue

                # fetch candles
                ohlc = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=25)
                closes = [c[4] for c in ohlc]
                lows = [c[3] for c in ohlc]
                highs = [c[2] for c in ohlc]

                import numpy as np
                mid = np.mean(closes[-20:])
                std = np.std(closes[-20:])
                upper = mid + 1.5 * std
                lower = mid - 1.5 * std

                side = cfg["side"]

                # ENTRY CONDITION
                if side == "BUY" and lows[-1] > lower:
                    continue
                if side == "SELL" and highs[-1] < upper:
                    continue

                price = get_price(symbol)
                qty = cfg["lot"]

                print(f"📌 ENTRY {symbol} {side} @ {price}")

                order = exchange.create_market_order(
                    symbol,
                    "buy" if side == "BUY" else "sell",
                    qty
                )

                avg_price = order["average"] or price

                place_tp_sl(symbol, side, qty, avg_price)

                cooldown[symbol] = now + timedelta(minutes=COOLDOWN_MINUTES)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("ERROR:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
