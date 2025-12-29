#!/usr/bin/env python3
import ccxt
import time
import logging
import random
from datetime import datetime, timedelta, timezone

# ===================== MODE =====================
MODE = "SIM"   # "SIM" or "LIVE"

# ================= CONFIG =================
API_KEY = ""
API_SECRET = ""

TIMEFRAME = "1m"
LEVERAGE = 75

TP_USDT = 0.32
SL_USDT = 0.16

CHECK_INTERVAL = 2
COOLDOWN_MINUTES = 30

DAILY_DD_LIMIT = 0.50   # 50%

FEE_RATE = 0.0012
SLIPPAGE_RANGE = (0.0002, 0.0008)

IST = timezone(timedelta(hours=5, minutes=30))

# ================= COINS =================
COINS = {
    "BNB/USDT":  {"lot": 0.04, "side": "BUY",  "sessions": ["ASIA","LONDON"]},
    "XRP/USDT":  {"lot": 15,   "side": "SELL", "sessions": ["ASIA"]},
    "AVAX/USDT": {"lot": 1,    "side": "SELL", "sessions": ["ASIA","LONDON"]},
    "SOL/USDT":  {"lot": 0.1,  "side": "BUY",  "sessions": ["ASIA","LONDON"]},
}

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# ================= EXCHANGE =================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

if MODE == "SIM":
    exchange.set_sandbox_mode(True)

# ================= HELPERS =================
def now_ist():
    return datetime.now(IST)

def get_session(ts):
    h = ts.hour + ts.minute / 60
    if 6 <= h < 13.5: return "ASIA"
    if 13.5 <= h < 18.5: return "LONDON"
    if 18.5 <= h < 23.5: return "NY"
    return None

def get_balance():
    bal = exchange.fetch_balance()
    return float(bal["USDT"]["free"])

def set_leverage(symbol):
    try:
        exchange.set_leverage(LEVERAGE, symbol)
    except:
        pass

def has_position(symbol):
    pos = exchange.fetch_positions([symbol])
    for p in pos:
        if abs(float(p.get("contracts", 0))) > 0:
            return True
    return False

def get_price(symbol):
    return exchange.fetch_ticker(symbol)["last"]

# ================= TP / SL =================
def place_tp_sl(symbol, side, qty, entry):
    if side == "BUY":
        tp = entry + TP_USDT / qty
        sl = entry - SL_USDT / qty
        close_side = "sell"
    else:
        tp = entry - TP_USDT / qty
        sl = entry + SL_USDT / qty
        close_side = "buy"

    if MODE == "LIVE":
        exchange.create_order(
            symbol=symbol,
            type="limit",
            side=close_side,
            amount=qty,
            price=round(tp, 6),
            params={"reduceOnly": True}
        )

        exchange.create_order(
            symbol=symbol,
            type="stop_market",
            side=close_side,
            amount=qty,
            params={
                "stopPrice": round(sl, 6),
                "reduceOnly": True
            }
        )

    return tp, sl


# ================= MAIN =================
def main():
    log.info("===================================")
    log.info("BOT STARTED")
    log.info(f"MODE = {MODE}")
    log.info(f"START BALANCE = {get_balance():.4f}")
    log.info("===================================")

    for s in COINS:
        set_leverage(s)

    cooldown = {c: None for c in COINS}
    open_trade = {c: None for c in COINS}

    start_day = now_ist().date()
    day_start_balance = get_balance()
    blocked_today = False

    while True:
        try:
            now = now_ist()

            # new day reset
            if now.date() != start_day:
                start_day = now.date()
                day_start_balance = get_balance()
                blocked_today = False
                log.info("🔄 New Day Started")

            if blocked_today:
                time.sleep(5)
                continue

            for symbol, cfg in COINS.items():

                if cooldown[symbol] and now < cooldown[symbol]:
                    continue

                session = get_session(now)
                if session not in cfg["sessions"]:
                    continue

                if open_trade[symbol]:
                    continue

                if has_position(symbol):
                    continue

                balance = get_balance()
                if balance <= 0:
                    log.error("BALANCE ZERO — STOPPING BOT")
                    return

                if (day_start_balance - balance) >= day_start_balance * DAILY_DD_LIMIT:
                    log.warning(
                        f"DAILY DD HIT | Start={day_start_balance:.2f} Now={balance:.2f}"
                    )
                    blocked_today = True
                    continue

                # ---- fetch candles ----
                candles = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=25)
                closes = [c[4] for c in candles]
                highs = [c[2] for c in candles]
                lows = [c[3] for c in candles]

                mid = sum(closes[-20:]) / 20
                std = (sum([(x - mid) ** 2 for x in closes[-20:]]) / 20) ** 0.5
                upper = mid + 1.5 * std
                lower = mid - 1.5 * std

                side = cfg["side"]

                # entry condition
                if side == "BUY" and lows[-1] > lower:
                    continue
                if side == "SELL" and highs[-1] < upper:
                    continue

                price = get_price(symbol)
                qty = cfg["lot"]

                # simulate slippage
                price *= 1 + random.uniform(*SLIPPAGE_RANGE)

                log.info(
                    f"ENTRY → {symbol} | SIDE={side} | PRICE={price:.5f} | QTY={qty}"
                )

                if MODE == "LIVE":
                    order = exchange.create_market_order(
                        symbol,
                        "buy" if side == "BUY" else "sell",
                        qty
                    )
                    entry_price = order["average"] or price
                else:
                    entry_price = price

                tp, sl = place_tp_sl(symbol, side, qty, entry_price)

                log.info(
                    f"TP={tp:.5f} | SL={sl:.5f}"
                )

                open_trade[symbol] = {
                    "side": side,
                    "entry": entry_price,
                    "qty": qty,
                    "tp": tp,
                    "sl": sl
                }

            # ===== MONITOR POSITIONS =====
            for symbol, pos in list(open_trade.items()):
                if not pos:
                    continue

                price = get_price(symbol)
                side = pos["side"]

                hit = None
                if side == "BUY":
                    if price <= pos["sl"]:
                        hit = "SL"
                    elif price >= pos["tp"]:
                        hit = "TP"
                else:
                    if price >= pos["sl"]:
                        hit = "SL"
                    elif price <= pos["tp"]:
                        hit = "TP"

                if hit:
                    pnl = (
                        (price - pos["entry"]) * pos["qty"]
                        if side == "BUY"
                        else (pos["entry"] - price) * pos["qty"]
                    )
                    pnl -= pos["entry"] * pos["qty"] * FEE_RATE

                    balance = get_balance() if MODE == "LIVE" else balance + pnl

                    log.info(
                        f"EXIT → {symbol} | {hit} HIT | "
                        f"ENTRY={pos['entry']:.5f} EXIT={price:.5f} "
                        f"PNL={pnl:.4f} BAL={balance:.4f}"
                    )

                    if hit == "SL":
                        cooldown[symbol] = now + timedelta(minutes=COOLDOWN_MINUTES)

                    open_trade[symbol] = None

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            log.error(f"ERROR → {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
