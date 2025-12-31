#!/usr/bin/env python3
import ccxt, time, random, traceback
from datetime import datetime, timedelta, timezone

# ================= MODE =================
MODE = "SIM"   # "SIM" or "LIVE"

# ================= API ==================
API_KEY = ""
API_SECRET = ""

# ================= SETTINGS =================
TIMEFRAME = "1m"
LEVERAGE = 75
CHECK_INTERVAL = 2

TP_USDT = 0.32
SL_USDT = 0.16

SIM_START_BALANCE = 3.0
DAILY_DD_LIMIT = 0.50

COOLDOWN_MINUTES = 30
SLIPPAGE_RANGE = (0.0002, 0.0008)
EXEC_DELAY = (1, 3)

FEE_RATE = 0.0012

IST = timezone(timedelta(hours=5, minutes=30))

# ================= COINS =================
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
    "options": {"defaultType": "future"}
})

if MODE == "SIM":
    exchange.set_sandbox_mode(True)

# ================= UTILS =================
def now():
    return datetime.now(IST)

def log(msg):
    print(f"{now()} | {msg}", flush=True)

def get_session(t):
    h = t.hour + t.minute / 60
    if 6 <= h < 13.5: return "ASIA"
    if 13.5 <= h < 18.5: return "LONDON"
    if 18.5 <= h < 23.5: return "NY"
    return None

def get_price(symbol):
    return exchange.fetch_ticker(symbol)["last"]

def fetch_ohlc(symbol):
    return exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=25)

def set_leverage(symbol):
    try:
        exchange.set_leverage(LEVERAGE, symbol)
    except:
        pass

# ================= BOLLINGER =================
def bollinger_signal(ohlc, side):
    closes = [x[4] for x in ohlc]
    highs  = [x[2] for x in ohlc]
    lows   = [x[3] for x in ohlc]

    mid = sum(closes[-20:]) / 20
    std = (sum((c - mid) ** 2 for c in closes[-20:]) / 20) ** 0.5

    upper = mid + 1.5 * std
    lower = mid - 1.5 * std

    if side == "BUY":
        return lows[-1] <= lower
    else:
        return highs[-1] >= upper

# ================= TP ORDER =================
def place_tp(symbol, side, qty, tp):
    if MODE != "LIVE":
        return
    exchange.create_order(
        symbol=symbol,
        type="limit",
        side="sell" if side == "BUY" else "buy",
        amount=qty,
        price=round(tp, 6),
        params={"reduceOnly": True}
    )

# ================= MAIN =================
def main():
    global sim_balance

    log("🔥 BOT STARTED")
    log(f"MODE = {MODE}")

    if MODE == "SIM":
        sim_balance = SIM_START_BALANCE
        log(f"SIM START BALANCE = {sim_balance}")
    else:
        bal = exchange.fetch_balance()
        log(f"LIVE BALANCE = {bal['USDT']['free']}")

    for s in COINS:
        set_leverage(s)

    open_pos = {s: False for s in COINS}
    cooldown = {s: None for s in COINS}

    day_start = now().date()
    day_start_balance = sim_balance if MODE == "SIM" else exchange.fetch_balance()["USDT"]["free"]
    blocked_today = False

    while True:
        try:
            t = now()

            # reset daily
            if t.date() != day_start:
                day_start = t.date()
                day_start_balance = sim_balance if MODE == "SIM" else exchange.fetch_balance()["USDT"]["free"]
                blocked_today = False
                log("🔄 NEW DAY STARTED")

            for symbol, cfg in COINS.items():

                if blocked_today:
                    continue

                if cooldown[symbol] and t < cooldown[symbol]:
                    continue

                session = get_session(t)
                if session not in cfg["sessions"]:
                    continue

                if open_pos[symbol]:
                    continue

                ohlc = fetch_ohlc(symbol)
                if len(ohlc) < 25:
                    continue

                if not bollinger_signal(ohlc, cfg["side"]):
                    continue

                # delay + slippage
                time.sleep(random.randint(*EXEC_DELAY))
                price = get_price(symbol)
                price *= 1 + random.uniform(*SLIPPAGE_RANGE)

                qty = cfg["lot"]
                side = cfg["side"]

                entry = price
                tp = entry + TP_USDT / qty if side == "BUY" else entry - TP_USDT / qty
                sl = entry - SL_USDT / qty if side == "BUY" else entry + SL_USDT / qty

                log(f"📌 ENTRY {symbol} {side} @ {entry:.4f}")
                log(f"    TP={tp:.4f} SL={sl:.4f}")

                if MODE == "LIVE":
                    exchange.create_market_order(
                        symbol,
                        "buy" if side == "BUY" else "sell",
                        qty
                    )
                    place_tp(symbol, side, qty, tp)

                open_pos[symbol] = True

                # ===== POSITION MONITOR =====
                while True:
                    price_now = get_price(symbol)

                    # STOP LOSS
                    sl_hit = (side == "BUY" and price_now <= sl) or \
                             (side == "SELL" and price_now >= sl)

                    tp_hit = (side == "BUY" and price_now >= tp) or \
                             (side == "SELL" and price_now <= tp)

                    if sl_hit or tp_hit:
                        pnl = (
                            (price_now - entry) * qty
                            if side == "BUY"
                            else (entry - price_now) * qty
                        )
                        pnl -= entry * qty * FEE_RATE

                        if MODE == "SIM":
                            sim_balance += pnl

                        result = "TP" if tp_hit else "SL"
                        log(f"{'✅' if tp_hit else '❌'} {result} {symbol} | PNL={pnl:.4f} | BAL={sim_balance:.4f}")

                        open_pos[symbol] = False

                        # daily DD check AFTER close
                        current_balance = sim_balance if MODE == "SIM" else exchange.fetch_balance()["USDT"]["free"]
                        if (day_start_balance - current_balance) >= day_start_balance * DAILY_DD_LIMIT:
                            blocked_today = True
                            log("🚫 DAILY DD HIT → NEW TRADES BLOCKED")

                        cooldown[symbol] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                        break

                    time.sleep(1)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log("ERROR: " + str(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
