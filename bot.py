#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# =============== USER CONFIG ===============
SYMBOL = "XRP/USDT"
TIMEFRAME = "1m"          # Backtest-tested TF (can change to "3m" if you want)
LOT_SIZE = 5.0            # XRP quantity per trade (change here if needed)
RSI_PERIOD = 14
RSI_LOW = 10              # Reversal zone low
RSI_HIGH = 37             # Reversal zone high

TP_POINTS = 0.032         # Same as backtest
SL_POINTS = 0.016
POLL_INTERVAL = 2         # seconds
COOLDOWN_MINUTES = 15     # only after SL

# Path agar chaho to change kar sakta hai
LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

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
    exchange.set_leverage(75, SYMBOL)   # 75x jaisa backtest me; chahe to 100x kar sakta
    log.info("Leverage set to 75 for %s", SYMBOL)
except Exception as e:
    log.warning("set_leverage failed: %s", e)

# =============== STATE ===============
in_position = False
current_position = None        # dict: side, entry, time, tp/sl ids...
last_entry_candle_time = None  # ms of last candle where we entered
cooldown_until_utc = None      # SL ke baad next allowed entry time (UTC)

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

    tp_order = exchange.create_order(
        SYMBOL, "limit", close_side, LOT_SIZE, tp_price,
        {"reduceOnly": True}
    )

    sl_order = exchange.create_order(
        SYMBOL, "stop_market", close_side, LOT_SIZE, None,
        {"stopPrice": sl_price, "reduceOnly": True}
    )

    return tp_order.get("id"), sl_order.get("id"), tp_price, sl_price


def fetch_position_size() -> float:
    """
    Uses unified fetch_positions.
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
    """
    Position close detect + PnL log + SL par cooldown.
    """
    global in_position, current_position, cooldown_until_utc

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
        tp_price = current_position["tp_price"]
        sl_price = current_position["sl_price"]

        pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" \
            else (entry - exit_price) * LOT_SIZE

        # Decide if this was closer to SL or TP -> cooldown only if SL
        dist_to_tp = abs(exit_price - tp_price)
        dist_to_sl = abs(exit_price - sl_price)
        hit_sl = dist_to_sl < dist_to_tp

        if hit_sl:
            cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
            log.info("SL hit, cooldown active until %s UTC", cooldown_until_utc.isoformat())

        row = {
            "time": current_position["time"],
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "pnl": round(pnl, 6),
        }
        append_csv(row)
        print(f"[{now_str()}] EXIT {side} @ {exit_price:.6f} | PNL={pnl:.6f}", flush=True)
        log.info("Exit %s @ %.6f | PNL=%.6f", side, exit_price, pnl)

        in_position = False
        current_position = None


# =============== MAIN LOOP ===============
print(f"[{now_str()}] 🚀 RSI LIVE BOT STARTED | {SYMBOL} | FUTURES", flush=True)
log.info("Bot started for %s", SYMBOL)

while True:
    try:
        # Global single-position safety: sync with exchange
        size = fetch_position_size()
        if abs(size) > 1e-8 and not in_position:
            # There's a position on exchange but bot thinks not
            in_position = True
            log.warning("Detected open position on exchange, syncing state.")

        if abs(size) < 1e-8 and in_position and current_position is None:
            # No position on exchange but flag stuck
            in_position = False

        # ---------------- CANDLES + RSI ----------------
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        # last closed candle = second last row
        if len(df) < 3:
            time.sleep(POLL_INTERVAL)
            continue

        prev_closed = df.iloc[-3]   # previous closed
        last_closed = df.iloc[-2]   # most recent closed

        candle_time = int(last_closed["time"])
        close_price = float(last_closed["close"])
        rsi_curr = float(last_closed["rsi"])
        rsi_prev = float(prev_closed["rsi"])

        now_utc = datetime.now(timezone.utc)

        # ---------------- IF IN POSITION -> CHECK EXIT ----------------
        if in_position:
            check_position_closed()

        # Cooldown after SL: skip entries
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            time.sleep(POLL_INTERVAL)
            continue

        # ---------------- ENTRY LOGIC (RSI Reversal) ----------------
        if not in_position and not pd.isna(rsi_prev) and not pd.isna(rsi_curr):

            signal = None

            # BUY: prev RSI < 10, current RSI > 10
            if rsi_prev < RSI_LOW and rsi_curr > RSI_LOW:
                signal = "BUY"
            # SELL: prev RSI > 37, current RSI < 37
            elif rsi_prev > RSI_HIGH and rsi_curr < RSI_HIGH:
                signal = "SELL"

            # Double-entry protection: 1 trade per closed candle
            if signal and last_entry_candle_time != candle_time:
                # 🔐 FINAL SAFETY: exchange pe already position to nahi?
                size_check = fetch_position_size()
                if abs(size_check) > 1e-8:
                    in_position = True
                    log.warning("Abort entry: position already open on exchange.")
                else:
                    in_position = True                      # position lock
                    last_entry_candle_time = candle_time    # candle lock

                    print(
                        f"[{now_str()}] SIGNAL {signal} @ {close_price:.6f} | "
                        f"RSI(prev={rsi_prev:.2f}, curr={rsi_curr:.2f})",
                        flush=True
                    )
                    log.info("Signal %s at price %.6f RSI_prev=%.2f RSI_curr=%.2f",
                             signal, close_price, rsi_prev, rsi_curr)

                    try:
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

                        print(
                            f"[{now_str()}] ENTER {signal} @ {entry_price:.6f} | "
                            f"TP={tp_price:.6f} SL={sl_price:.6f}",
                            flush=True
                        )
                        log.info(
                            "Enter %s @ %.6f TP=%.6f SL=%.6f",
                            signal, entry_price, tp_price, sl_price
                        )

                    except Exception as e:
                        # ❌ Agar entry / tp/sl fail ho jaye → position unlock
                        in_position = False
                        current_position = None
                        print(f"[{now_str()}] ⚠️ Entry failed: {e}", flush=True)
                        log.error("Entry failed: %s", e)

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ KeyboardInterrupt, exiting...", flush=True)
        log.info("Bot stopped by user")
        break

    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(3)
