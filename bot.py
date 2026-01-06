#!/usr/bin/env python3
import ccxt, time, random
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

def fetch_price(symbol):
    return exchange.fetch_ticker(symbol)["last"]

def fetch_ohlc(symbol):
    return exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=25)

def bollinger_signal(ohlc, side):
    closes = [x[4] for x in ohlc]
    highs  = [x[2] for x in ohlc]
    lows   = [x[3] for x in ohlc]

    mid = sum(closes[-20:]) / 20
    std = (sum((c - mid) ** 2 for c in closes[-20:]) / 20) ** 0.5
    upper = mid + 1.5 * std
    lower = mid - 1.5 * std

    return lows[-1] <= lower if side == "BUY" else highs[-1] >= upper

# ================= MAIN =================
def main():
    sim_balance = SIM_START_BALANCE
    positions = {}
    cooldown = {}

    day = now().date()
    day_start_balance = sim_balance
    blocked_today = False

    log("🔥 BOT STARTED")
    log(f"MODE = {MODE}")
    log(f"START BAL = {sim_balance}")

    while True:
        try:
            t = now()

            # New day reset
            if t.date() != day:
                day = t.date()
                day_start_balance = sim_balance
                blocked_today = False
                log("🔄 NEW DAY")

            for symbol, cfg in COINS.items():

                # ===== MANAGE OPEN POSITION =====
                if symbol in positions:
                    pos = positions[symbol]
                    price = fetch_price(symbol)

                    hit_tp = price >= pos["tp"] if pos["side"] == "BUY" else price <= pos["tp"]
                    hit_sl = price <= pos["sl"] if pos["side"] == "BUY" else price >= pos["sl"]

                    if hit_tp or hit_sl:
                        pnl = (
                            (pos["tp"] - pos["entry"]) * pos["qty"]
                            if hit_tp else
                            (pos["sl"] - pos["entry"]) * pos["qty"]
                        )
                        pnl -= pos["entry"] * pos["qty"] * FEE_RATE
                        sim_balance += pnl

                        log(f"{'✅ TP' if hit_tp else '❌ SL'} {symbol} | PNL={pnl:.4f} | BAL={sim_balance:.4f}")

                        del positions[symbol]
                        cooldown[symbol] = t + timedelta(minutes=COOLDOWN_MINUTES)

                        if (day_start_balance - sim_balance) >= day_start_balance * DAILY_DD_LIMIT:
                            blocked_today = True
                            log("🚫 DAILY DD HIT")

                # ===== ENTRY CHECK =====
                else:
                    if blocked_today: continue
                    if symbol in cooldown and t < cooldown[symbol]: continue
                    if get_session(t) not in cfg["sessions"]: continue

                    ohlc = fetch_ohlc(symbol)
                    if not bollinger_signal(ohlc, cfg["side"]): continue

                    price = fetch_price(symbol)
                    qty = cfg["lot"]
                    side = cfg["side"]

                    entry = price
                    tp = entry + TP_USDT / qty if side == "BUY" else entry - TP_USDT / qty
                    sl = entry - SL_USDT / qty if side == "BUY" else entry + SL_USDT / qty

                    positions[symbol] = {
                        "side": side,
                        "entry": entry,
                        "tp": tp,
                        "sl": sl,
                        "qty": qty
                    }

                    log(f"📌 ENTRY {symbol} {side} @ {entry:.4f} | TP={tp:.4f} SL={sl:.4f}")

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log("ERROR: " + str(e))
            time.sleep(5)

if __name__ == "__main__":
    main()
