#!/usr/bin/env python3
import ccxt, time
from datetime import datetime, timedelta, timezone

# ================= MODE =================
MODE = "SIM"   # "SIM" or "LIVE"

# ================= API ==================
API_KEY = ""
API_SECRET = ""

# ================= SETTINGS =================
TIMEFRAME = "1m"
CHECK_INTERVAL = 2
LEVERAGE = 75

TP_USDT = 0.32
SL_USDT = 0.16

SIM_START_BALANCE = 10.0
DAILY_DD_LIMIT = 0.50
COOLDOWN_MINUTES = 30
LOSS_PAUSE_MIN = 60
MAX_LOSS_STREAK = 2

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

if MODE == "SIM":
    exchange.set_sandbox_mode(True)

# ================= UTILS =================
def now():
    return datetime.now(IST)

def log(msg):
    print(f"{now()} | {msg}", flush=True)

def fetch_price(symbol):
    return exchange.fetch_ticker(symbol)["last"]

def fetch_ohlc(symbol):
    return exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=250)

# ================= EMA =================
def ema(values, period):
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

# ================= MAIN =================
def main():
    sim_balance = SIM_START_BALANCE
    positions = {}
    cooldown = {}
    loss_streak = 0
    pause_until = None

    day = now().date()
    day_start_balance = sim_balance
    blocked_today = False

    log("🔥 BOT STARTED")
    log(f"MODE = {MODE}")
    log(f"START BAL = {sim_balance}")

    while True:
        try:
            t = now()

            # ===== DAILY RESET =====
            if t.date() != day:
                day = t.date()
                day_start_balance = sim_balance
                blocked_today = False
                loss_streak = 0
                log("🔄 NEW DAY")

            if pause_until and t < pause_until:
                time.sleep(5)
                continue

            # ===== MANAGE OPEN POSITIONS =====
            for symbol in list(positions.keys()):
                pos = positions[symbol]
                price = fetch_price(symbol)

                hit_tp = price >= pos["tp"] if pos["side"] == "BUY" else price <= pos["tp"]
                hit_sl = price <= pos["sl"] if pos["side"] == "BUY" else price >= pos["sl"]

                if hit_tp or hit_sl:
                    exit_price = pos["tp"] if hit_tp else pos["sl"]
                    pnl = (
                        (exit_price - pos["entry"]) * pos["qty"]
                        if pos["side"] == "BUY"
                        else (pos["entry"] - exit_price) * pos["qty"]
                    )
                    pnl -= pos["entry"] * pos["qty"] * FEE_RATE
                    sim_balance += pnl

                    log(f"{'✅ TP' if hit_tp else '❌ SL'} {symbol} | PNL={pnl:.4f} | BAL={sim_balance:.4f}")

                    if hit_sl:
                        loss_streak += 1
                        if loss_streak >= MAX_LOSS_STREAK:
                            pause_until = now() + timedelta(minutes=LOSS_PAUSE_MIN)
                            log("⏸ LOSS STREAK → PAUSE 60 MIN")
                    else:
                        loss_streak = 0

                    del positions[symbol]
                    cooldown[symbol] = t + timedelta(minutes=COOLDOWN_MINUTES)

                    if (day_start_balance - sim_balance) >= day_start_balance * DAILY_DD_LIMIT:
                        blocked_today = True
                        log("🚫 DAILY DD HIT")

            # ===== ENTRY CHECK =====
            if blocked_today:
                time.sleep(3)
                continue

            for symbol, cfg in COINS.items():
                if symbol in positions:
                    continue
                if symbol in cooldown and t < cooldown[symbol]:
                    continue

                ohlc = fetch_ohlc(symbol)
                if len(ohlc) < 210:
                    continue

                closes = [x[4] for x in ohlc]

                ema9   = ema(closes[-50:], 9)
                ema21  = ema(closes[-50:], 21)
                ema200 = ema(closes[-210:], 200)

                side = None
                price = closes[-1]

                if ema9 > ema21 and price > ema200:
                    side = "BUY"
                elif ema9 < ema21 and price < ema200:
                    side = "SELL"
                else:
                    continue

                entry = fetch_price(symbol)
                lot = cfg["lot"]

                tp = entry + TP_USDT / lot if side == "BUY" else entry - TP_USDT / lot
                sl = entry - SL_USDT / lot if side == "BUY" else entry + SL_USDT / lot

                positions[symbol] = {
                    "side": side,
                    "entry": entry,
                    "tp": tp,
                    "sl": sl,
                    "qty": lot
                }

                log(f"📌 ENTRY {symbol} {side} @ {entry:.4f} | TP={tp:.4f} SL={sl:.4f}")

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log("ERROR: " + str(e))
            time.sleep(5)

if __name__ == "__main__":
    main()
