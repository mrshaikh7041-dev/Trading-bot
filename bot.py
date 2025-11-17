#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# =============== USER CONFIG ===============
SYMBOL = "BNB/USUT"
TIMEFRAME = "1m"
LOT_SIZE = 0.02          # EXACT yehi qty order me jayegi
RSI_PERIOD = 14
RSI_LOW = 20
RSI_HIGH = 60
TP_POINTS = 6.0
SL_POINTS = 3.0
POLL_INTERVAL = 2        # seconds

# Path agar chaho to change kar sakta hai
LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace("/", "-")}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# =============== LOGGING ===============
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# =============== TIME HELPERS ===============
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")

# =============== EXCHANGE SETUP ===============
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.options["adjustForTimeDifference"] = True
exchange.load_markets()

# Try setting leverage once (ignore error if not supported)
try:
    exchange.set_leverage(75, SYMBOL)
    log.info("Leverage set to 75 for %s", SYMBOL)
except Exception as e:
    log.warning("set_leverage failed: %s", e)

# =============== STATE ===============
in_position = False
current_position = None   # dict: side, entry, time
last_entry_candle_time = None  # ms of last candle where we entered

# =============== HELPERS ===============
def append_csv(row: dict):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)

def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def place_market_entry(side: str, approx_price: float) -> float:
    params = {"reduceOnly": False}
    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE, None, params)
    entry = order.get("average") or order.get("price") or approx_price
    return float(entry)

def place_tp_sl(side: str, entry: float):
    """
    EXACT TP/SL — koi rounding nahi.
    TP = entry ± TP_POINTS
    SL = entry ∓ SL_POINTS
    """
    close_side = "sell" if side == "BUY" else "buy"
    if side == "BUY":
        tp_price = entry + TP_POINTS
        sl_price = entry - SL_POINTS
    else:
        tp_price = entry - TP_POINTS
        sl_price = entry + SL_POINTS

    # yaha koi round_price nahi – direct float jaisa hai waisa jayega
    tp_order = exchange.create_order(
        SYMBOL, "limit", close_side, LOT_SIZE, tp_price,
        {"reduceOnly": True}
    )
    sl_order = exchange.create_order(
        SYMBOL, "STOP_MARKET", close_side, LOT_SIZE, None,
        {"stopPrice": sl_price, "reduceOnly": True}
    )
    return tp_order.get("id"), sl_order.get("id"), tp_price, sl_price

def fetch_position_size() -> float:
    """
    Uses unified fetch_positions so koi fapiPrivate* confusion nahi.
    Returns contracts amount (positive long, negative short).
    """
    try:
        positions = exchange.fetch_positions([SYMBOL])
    except Exception as e:
        log.error("fetch_positions error: %s", e)
        return 0.0

    for p in positions:
        if p.get("symbol") == SYMBOL:
            size = p.get("contracts")
            if size is None:
                try:
                    size = float(p.get("info", {}).get("positionAmt", 0))
                except Exception:
                    size = 0
            return float(size or 0)
    return 0.0

def check_position_closed():
    global in_position, current_position

    size = fetch_position_size()
    if abs(size) < 1e-8 and in_position and current_position:
        # Position closed (TP/SL/manual)
        try:
            ticker = exchange.fetch_ticker(SYMBOL)
            exit_price = float(ticker.get("last") or ticker.get("close"))
        except Exception:
            exit_price = current_position["entry"]

        entry = current_position["entry"]
        side = current_position["side"]
        pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" \
              else (entry - exit_price) * LOT_SIZE

        row = {
            "time": current_position["time"],
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "pnl": round(pnl, 6),
        }
        append_csv(row)
        print(f"[{now_str()}] EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f}", flush=True)
        log.info("Exit %s @ %.4f | PNL=%.6f", side, exit_price, pnl)

        in_position = False
        current_position = None

# =============== MAIN LOOP ===============
print(f"[{now_str()}] 🚀 RSI LIVE BOT STARTED | {SYMBOL} | MARKET ONLY (polling)", flush=True)
log.info("Bot started for %s", SYMBOL)

while True:
    try:
        # ---------------- CANDLES + RSI ----------------
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        # last closed candle = second last row (last is still forming)
        last_closed = df.iloc[-2]
        candle_time = int(last_closed["time"])
        close_price = float(last_closed["close"])
        rsi = float(last_closed["rsi"])

        # ---------------- IF IN POSITION -> CHECK EXIT ----------------
        if in_position:
            check_position_closed()

        # ---------------- ENTRY LOGIC ----------------
        if not in_position:
            signal = None
            if rsi < RSI_LOW:
                signal = "BUY"
            elif rsi > RSI_HIGH:
                signal = "SELL"

            if signal:
                # avoid more than 1 entry on same candle
                if last_entry_candle_time == candle_time:
                    pass
                else:
                    print(f"[{now_str()}] SIGNAL {signal} @ {close_price:.4f} | RSI={rsi:.2f}", flush=True)
                    log.info("Signal %s at price %.4f RSI=%.2f", signal, close_price, rsi)

                    entry_price = place_market_entry(signal, close_price)
                    tp_id, sl_id, tp_price, sl_price = place_tp_sl(signal, entry_price)

                    current_position = {
                        "side": signal,
                        "entry": entry_price,
                        "time": now_ist().isoformat(),
                        "tp_id": tp_id,
                        "sl_id": sl_id,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "candle_time": candle_time,
                    }
                    in_position = True
                    last_entry_candle_time = candle_time

                    print(
                        f"[{now_str()}] ENTER {signal} @ {entry_price:.4f} | TP={tp_price:.4f} SL={sl_price:.4f}",
                        flush=True
                    )
                    log.info(
                        "Enter %s @ %.4f TP=%.4f SL=%.4f",
                        signal, entry_price, tp_price, sl_price
                    )

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ KeyboardInterrupt, exiting...", flush=True)
        log.info("Bot stopped by user")
        break

    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(3)
