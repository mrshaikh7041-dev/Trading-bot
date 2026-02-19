#!/usr/bin/env python3
import ccxt, time, traceback
import pandas as pd
from datetime import datetime, timedelta, timezone

# ================= MODE =================
MODE = "SIM"    # "SIM" or "LIVE"

# ================= API ==================
API_KEY = ""
API_SECRET = ""

# ================= SETTINGS =================
TIMEFRAME = "1m"
CHECK_INTERVAL = 2

TP_USDT = 0.32
SL_USDT = 0.16

SIM_START_BALANCE = 3.0
DAILY_DD_LIMIT = 0.50

COOLDOWN_MINUTES = 30
FEE_RATE = 0.0012

IST = timezone(timedelta(hours=5, minutes=30))

# ================= COINS =================
COINS = {
    "BNB/USDT":  {"lot": 0.04},
    "XRP/USDT":  {"lot": 15},
    "AVAX/USDT": {"lot": 1},
    "SOL/USDT":  {"lot": 0.1},
}

# ================= EXCHANGE =================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

# ================= GLOBAL STATE =================
sim_balance = SIM_START_BALANCE
positions = {}
cooldown = {}

day = None
day_start_balance = SIM_START_BALANCE
blocked_today = False

# ================= UTILS =================
def now():
    return datetime.now(IST)

def log(msg):
    print(f"{now()} | {msg}", flush=True)

def get_balance():
    global sim_balance
    if MODE == "SIM":
        return sim_balance
    bal = exchange.fetch_balance()
    return float(bal["USDT"]["free"])

# ================= DATA =================
def fetch_ohlc(symbol):
    ohlc = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=210)
    df = pd.DataFrame(
        ohlc,
        columns=["time","open","high","low","close","volume"]
    )
    return df

# ================= EMA SIGNAL =================
def ema_signal(df):

    df["ema9"] = df["close"].ewm(span=9).mean()
    df["ema21"] = df["close"].ewm(span=21).mean()
    df["ema200"] = df["close"].ewm(span=200).mean()

    i = len(df) - 1

    prev9 = df["ema9"].iloc[i-1]
    prev21 = df["ema21"].iloc[i-1]

    curr9 = df["ema9"].iloc[i]
    curr21 = df["ema21"].iloc[i]

    price = df["close"].iloc[i]
    ema200 = df["ema200"].iloc[i]

    # crossover logic
    if price > ema200 and prev9 <= prev21 and curr9 > curr21:
        return "BUY"

    if price < ema200 and prev9 >= prev21 and curr9 < curr21:
        return "SELL"

    return None

# ================= TP PLACE =================
def place_tp(symbol, side, qty, tp):

    if MODE != "LIVE":
        return

    exchange.create_order(
        symbol=symbol,
        type="limit",
        side="sell" if side=="BUY" else "buy",
        amount=qty,
        price=round(tp,6),
        params={"reduceOnly": True}
    )

# ================= ENTRY =================
def open_trade(symbol, side, entry):

    global sim_balance

    qty = COINS[symbol]["lot"]

    tp = entry + TP_USDT/qty if side=="BUY" else entry - TP_USDT/qty
    sl = entry - SL_USDT/qty if side=="BUY" else entry + SL_USDT/qty

    if MODE == "LIVE":

        exchange.create_market_order(
            symbol,
            "buy" if side=="BUY" else "sell",
            qty
        )

        place_tp(symbol, side, qty, tp)

    positions[symbol] = {
        "side": side,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "qty": qty
    }

    log(f"ENTRY {symbol} {side} @ {entry:.4f}")
    log(f"TP={tp:.4f} SL={sl:.4f}")

# ================= EXIT =================
def close_trade(symbol, price, reason):

    global sim_balance

    pos = positions[symbol]

    if reason=="TP":
        exit_price = pos["tp"]
    else:
        exit_price = pos["sl"]

    pnl = (
        (exit_price - pos["entry"]) * pos["qty"]
        if pos["side"]=="BUY"
        else (pos["entry"] - exit_price) * pos["qty"]
    )

    pnl -= pos["entry"] * pos["qty"] * FEE_RATE

    if MODE=="SIM":
        sim_balance += pnl

    log(f"{reason} {symbol} PNL={pnl:.4f} BAL={get_balance():.4f}")

    del positions[symbol]

    if reason=="SL":
        cooldown[symbol] = now() + timedelta(minutes=COOLDOWN_MINUTES)

# ================= MAIN =================
def main():

    global day, day_start_balance, blocked_today

    log("BOT STARTED")
    log(f"MODE={MODE}")
    log(f"START BAL={get_balance()}")

    while True:

        try:

            t = now()

            # ===== new day reset =====

            if day != t.date():

                day = t.date()
                day_start_balance = get_balance()
                blocked_today = False

                log("NEW DAY")

            # ===== manage open trades =====

            for symbol in list(positions.keys()):

                price = exchange.fetch_ticker(symbol)["last"]

                pos = positions[symbol]

                if pos["side"]=="BUY":

                    if price <= pos["sl"]:
                        close_trade(symbol, price, "SL")

                    elif price >= pos["tp"] and MODE=="SIM":
                        close_trade(symbol, price, "TP")

                else:

                    if price >= pos["sl"]:
                        close_trade(symbol, price, "SL")

                    elif price <= pos["tp"] and MODE=="SIM":
                        close_trade(symbol, price, "TP")

            # ===== daily DD =====

            bal = get_balance()

            if (day_start_balance - bal) >= day_start_balance * DAILY_DD_LIMIT:
                blocked_today = True

            # ===== entry =====

            if not blocked_today:

                for symbol in COINS:

                    if symbol in positions:
                        continue

                    if symbol in cooldown and now() < cooldown[symbol]:
                        continue

                    df = fetch_ohlc(symbol)

                    signal = ema_signal(df)

                    if not signal:
                        continue

                    entry = exchange.fetch_ticker(symbol)["last"]

                    open_trade(symbol, signal, entry)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:

            log("ERROR " + str(e))
            traceback.print_exc()
            time.sleep(5)

# ================= START =================
if __name__ == "__main__":
    main()
