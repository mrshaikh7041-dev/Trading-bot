#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import logging
from datetime import datetime, timezone, timedelta

# ================= USER CONFIG =================
SYMBOL = "AVAX/USDT"
TIMEFRAME = "1m"

LOT_SIZE = 1

RSI_PERIOD = 14
RSI_LOW = 10
RSI_HIGH = 37

TP_POINTS = 0.32
SL_POINTS = 0.16

EMA_FAST = 21
EMA_SLOW = 50

POLL_INTERVAL = 5
COOLDOWN_MINUTES = 15

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger()

# ================= TIME =================
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

# ================= EXCHANGE =================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.load_markets()
exchange.set_leverage(75, SYMBOL)
FUT_SYMBOL = SYMBOL.replace("/", "")

# ================= STATE =================
in_position = False
current_position = None
cooldown_until = None
wait_for_zone_exit = False

# ================= INDICATORS =================
def rsi(series, p=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/p, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# ================= POSITION =================
def fetch_position_size():
    bal = exchange.fetch_balance()
    for p in bal["info"]["positions"]:
        if p["symbol"] == FUT_SYMBOL:
            return float(p["positionAmt"])
    return 0.0

# ================= SL WATCHER =================
def sl_hit(side, sl_price):
    last_price = float(exchange.fetch_ticker(SYMBOL)["last"])
    if side == "BUY" and last_price <= sl_price:
        return True
    if side == "SELL" and last_price >= sl_price:
        return True
    return False

# ================= ENTRY =================
def place_entry(side):
    close_side = "sell" if side == "BUY" else "buy"

    order = exchange.create_order(
        SYMBOL, "market", side.lower(), LOT_SIZE
    )
    entry = float(order["average"])

    tp = entry + TP_POINTS if side == "BUY" else entry - TP_POINTS
    sl = entry - SL_POINTS if side == "BUY" else entry + SL_POINTS

    # ✅ TP only (NO STOP_MARKET SL)
    exchange.create_order(
        SYMBOL,
        "limit",
        close_side,
        LOT_SIZE,
        tp,
        {"reduceOnly": True}
    )

    return entry, tp, sl

# ================= EXIT =================
def close_position(price):
    global in_position, current_position, cooldown_until, wait_for_zone_exit

    side = current_position["side"]
    close_side = "sell" if side == "BUY" else "buy"

    exchange.create_order(
        SYMBOL,
        "market",
        close_side,
        LOT_SIZE,
        {"reduceOnly": True}
    )

    pnl = (price - current_position["entry"]) * LOT_SIZE if side == "BUY" else \
          (current_position["entry"] - price) * LOT_SIZE

    log.info(f"EXIT {side} @ {price:.4f} | PNL={pnl:.4f}")

    if pnl < 0:
        cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
    else:
        wait_for_zone_exit = True

    in_position = False
    current_position = None

# ================= START =================
log.info("🚀 BOT STARTED")

# ================= MAIN LOOP =================
while True:
    try:
        # ================= IN POSITION =================
        if in_position:
            side = current_position["side"]
            sl_price = current_position["sl"]

            # ✅ BOT LEVEL SL (SAFE)
            if sl_hit(side, sl_price):
                log.warning(f"🛑 SL HIT (BOT) @ {sl_price:.4f}")
                close_position(sl_price)

            # TP or manual close
            elif abs(fetch_position_size()) == 0:
                price = float(exchange.fetch_ticker(SYMBOL)["last"])
                close_position(price)

            time.sleep(POLL_INTERVAL)
            continue

        # ================= COOLDOWN =================
        if cooldown_until and datetime.now(timezone.utc) < cooldown_until:
            time.sleep(POLL_INTERVAL)
            continue

        # ================= DATA =================
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=200)
        df = pd.DataFrame(ohlc, columns=["t","o","h","l","c","v"])

        df["rsi"] = rsi(df["c"], RSI_PERIOD)
        df["ema_fast"] = ema(df["c"], EMA_FAST)
        df["ema_slow"] = ema(df["c"], EMA_SLOW)

        prev = df.iloc[-3]
        last = df.iloc[-2]

        prev_rsi = float(prev["rsi"])
        last_rsi = float(last["rsi"])
        ema_fast = float(last["ema_fast"])
        ema_slow = float(last["ema_slow"])

        # ================= ZONE EXIT =================
        if wait_for_zone_exit:
            if RSI_LOW < last_rsi < RSI_HIGH:
                time.sleep(POLL_INTERVAL)
                continue
            else:
                wait_for_zone_exit = False

        signal = None

        # ================= STRATEGY =================
        if prev_rsi < RSI_LOW and RSI_LOW < last_rsi < RSI_HIGH and ema_fast > ema_slow:
            signal = "BUY"
        elif prev_rsi > RSI_HIGH and RSI_LOW < last_rsi < RSI_HIGH and ema_fast < ema_slow:
            signal = "SELL"

        if signal:
            entry, tp, sl = place_entry(signal)
            current_position = {
                "side": signal,
                "entry": entry,
                "sl": sl
            }
            in_position = True
            log.info(f"ENTER {signal} @ {entry:.4f} | TP {tp:.4f} SL {sl:.4f}")

        time.sleep(POLL_INTERVAL)

    except Exception as e:
        log.error(f"ERROR: {e}")
        time.sleep(3)
