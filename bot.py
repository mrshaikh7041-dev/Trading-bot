#!/usr/bin/env python3
import ccxt, time, random, traceback
from datetime import datetime, timedelta, timezone

# ===================== MODE =====================
MODE = "SIM"      # "SIM" or "LIVE"

# ===================== API ======================
API_KEY = ""
API_SECRET = ""

# ===================== SETTINGS =================
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

IST = timezone(timedelta(hours=5, minutes=30))

# ===================== COINS ====================
COINS = {
    "BNB/USDT":  {"lot": 0.04, "side": "BUY",  "sessions": ["ASIA","LONDON"]},
    "XRP/USDT":  {"lot": 15,   "side": "SELL", "sessions": ["ASIA"]},
    "AVAX/USDT": {"lot": 1,    "side": "SELL", "sessions": ["ASIA","LONDON"]},
    "SOL/USDT":  {"lot": 0.1,  "side": "BUY",  "sessions": ["ASIA","LONDON"]},
}

# ===================== EXCHANGE =================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})

if MODE == "SIM":
    exchange.set_sandbox_mode(True)

# ===================== HELPERS ==================
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

def set_leverage(symbol):
    try:
        exchange.set_leverage(LEVERAGE, symbol)
    except:
        pass

def fetch_candles(symbol):
    return exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=25)

def get_balance():
    global sim_balance
    if MODE == "SIM":
        return sim_balance
    bal = exchange.fetch_balance()
    return float(bal["USDT"]["free"])

# ===================== ENTRY FILTER =====================
def bollinger_signal(ohlc, side):
    closes = [c[4] for c in ohlc]
    highs  = [c[2] for c in ohlc]
    lows   = [c[3] for c in ohlc]

    mid = sum(closes[-20:]) / 20
    std = (sum([(x - mid) ** 2 for x in closes[-20:]]) / 20) ** 0.5

    upper = mid + 1.5 * std
    lower = mid - 1.5 * std

    if side == "BUY":
        return lows[-1] <= lower
    else:
        return highs[-1] >= upper


# ===================== BOT-SIDE SL EXEC =====================
def check_sl_hit(side, price, sl):
    if side == "BUY":
        return price <= sl
    else:
        return price >= sl


# ===================== TP ORDER =====================
def place_tp(symbol, side, qty, tp):
    if MODE == "SIM":
        return

    tp_side = "sell" if side == "BUY" else "buy"

    exchange.create_order(
        symbol=symbol,
        type="limit",
        side=tp_side,
        amount=qty,
        price=round(tp, 6),
        params={"reduceOnly": True}
    )


# ===================== MAIN =====================
def main():
    global sim_balance

    log("🔥 BOT STARTED")
    log(f"MODE = {MODE}")

    if MODE == "SIM":
        sim_balance = SIM_START_BALANCE
        log(f"SIM BALANCE = {sim_balance}")
    else:
        log(f"LIVE BALANCE = {get_balance()}")

    for s in COINS:
        set_leverage(s)

    cooldown = {s: None for s in COINS}
    open_pos = {s: False for s in COINS}

    day_start = now().date()
    day_start_balance = get_balance()
    blocked_today = False

    while True:
        try:
            now_t = now()

            if now_t.date() != day_start:
                day_start = now_t.date()
                day_start_balance = get_balance()
                blocked_today = False
                log("🔄 New trading day started")

            for symbol, cfg in COINS.items():

                if blocked_today:
                    continue

                session = get_session(now_t)
                if session not in cfg["sessions"]:
                    continue

                if open_pos[symbol]:
                    continue

                if cooldown[symbol] and now_t < cooldown[symbol]:
                    continue

                balance = get_balance()
                if balance <= 0:
                    log("❌ Balance zero — stopping bot")
                    return

                if (day_start_balance - balance) >= day_start_balance * DAILY_DD_LIMIT:
                    log("🚫 DAILY DD HIT → Trading paused")
                    blocked_today = True
                    continue

                ohlc = fetch_candles(symbol)
                if len(ohlc) < 25:
                    continue

                side = cfg["side"]
                if not bollinger_signal(ohlc, side):
                    continue

                time.sleep(random.randint(*EXEC_DELAY))

                price = get_price(symbol)
                price *= 1 + random.uniform(*SLIPPAGE_RANGE)

                qty = cfg["lot"]

                if MODE == "LIVE":
                    exchange.create_market_order(
                        symbol,
                        "buy" if side == "BUY" else "sell",
                        qty
                    )

                entry = price
                tp = entry + TP_USDT / qty if side == "BUY" else entry - TP_USDT / qty
                sl = entry - SL_USDT / qty if side == "BUY" else entry + SL_USDT / qty

                log(f"📌 ENTRY {symbol} {side} @ {entry:.4f}")
                log(f"   TP={tp:.4f} | SL={sl:.4f}")

                if MODE == "LIVE":
                    place_tp(symbol, side, qty, tp)

                open_pos[symbol] = True

                # ===== monitor position =====
                while True:
                    price_now = get_price(symbol)

                    # SL check (BOT SIDE)
                    if check_sl_hit(side, price_now, sl):
                        pnl = (price_now - entry) * qty if side == "BUY" else (entry - price_now) * qty
                        pnl -= entry * qty * 0.0012

                        if MODE == "SIM":
                            sim_balance += pnl

                        log(f"❌ SL HIT {symbol} | PNL={pnl:.4f} | BAL={get_balance():.4f}")

                        cooldown[symbol] = now() + timedelta(minutes=COOLDOWN_MINUTES)
                        open_pos[symbol] = False
                        break

                    # TP handled by exchange (LIVE)
                    if MODE == "SIM":
                        if (side == "BUY" and price_now >= tp) or (side == "SELL" and price_now <= tp):
                            pnl = (tp - entry) * qty if side == "BUY" else (entry - tp) * qty
                            pnl -= entry * qty * 0.0012
                            sim_balance += pnl

                            log(f"✅ TP HIT {symbol} | PNL={pnl:.4f} | BAL={get_balance():.4f}")
                            open_pos[symbol] = False
                            break

                    time.sleep(1)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
