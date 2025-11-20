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
TIMEFRAME = "1m"
LOT_SIZE = 5.0                # XRP per trade
RSI_PERIOD = 14
RSI_LOW = 10                  # Reversal zone low
RSI_HIGH = 37                 # Reversal zone high

TP_POINTS = 0.032             # TP distance
SL_POINTS = 0.016             # SL distance
POLL_INTERVAL = 2             # seconds
COOLDOWN_MINUTES = 15         # only after SL

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

try:
    exchange.set_leverage(75, SYMBOL)
    log.info("Leverage set to 75 for %s", SYMBOL)
except Exception as e:
    log.warning("set_leverage failed: %s", e)

# =============== STATE ===============
cooldown_until_utc = None
last_entry_candle_time = None          # kis candle ke baad last entry hui
pending_signal = None                  # "BUY" / "SELL"
pending_signal_candle_time = None      # jis closed candle par signal bana tha

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

def get_position_status():
    """
    Monitor: exchange se real-time position state.
    FLAT / OPEN_BUY / OPEN_SELL
    """
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for p in positions:
            if p.get("symbol") == SYMBOL:
                size = float(p.get("contracts") or 0)
                if size > 0:
                    return "OPEN_BUY"
                if size < 0:
                    return "OPEN_SELL"
                return "FLAT"
    except Exception as e:
        log.error("get_position_status error: %s", e)
    return "UNKNOWN"

def place_entry(side: str, approx_price: float) -> float:
    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE,
                                  None, {"reduceOnly": False})
    entry = order.get("average") or order.get("price") or approx_price
    return float(entry)

def place_tp_sl(side: str, entry: float):
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

def log_exit(side: str, entry: float, exit_price: float):
    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" \
        else (entry - exit_price) * LOT_SIZE

    row = {
        "time": now_ist().isoformat(),
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "pnl": round(pnl, 6),
    }
    append_csv(row)
    print(f"[{now_str()}] EXIT {side} @ {exit_price:.6f} | PNL={pnl:.6f}", flush=True)
    log.info("Exit %s @ %.6f | PNL=%.6f", side, exit_price, pnl)

# =============== MAIN LOOP ===============
print(f"[{now_str()}] 🚀 RSI REVERSAL BOT STARTED | {SYMBOL} | FUTURES", flush=True)
log.info("Bot started for %s", SYMBOL)

while True:
    try:
        now_utc = datetime.now(timezone.utc)

        # 1) Position monitor: single entry guarantee
        pos_status = get_position_status()

        # Agar position open hai -> koi naya signal/entry nahi
        if pos_status != "FLAT":
            # open trade ki wajah se queued signal ko clear kar do
            if pending_signal is not None:
                log.info("Clearing pending signal because position is open.")
                pending_signal = None
                pending_signal_candle_time = None
            time.sleep(POLL_INTERVAL)
            continue

        # 2) Cooldown after SL
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            time.sleep(POLL_INTERVAL)
            continue

        # 3) Candles + RSI
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        if len(df) < 3:
            time.sleep(POLL_INTERVAL)
            continue

        forming = df.iloc[-1]      # current forming candle
        last_closed = df.iloc[-2]  # latest closed candle
        prev_closed = df.iloc[-3]  # previous closed candle

        forming_time = int(forming["time"])
        candle_time = int(last_closed["time"])
        close_price = float(last_closed["close"])
        rsi_curr = float(last_closed["rsi"])
        rsi_prev = float(prev_closed["rsi"])

        # ========= STEP A: EXECUTE PENDING SIGNAL ON NEXT CANDLE OPEN =========
        if pending_signal is not None:
            # next candle started? (forming_time > signal candle time)
            if forming_time > pending_signal_candle_time:
                # Entry approx at new candle open
                next_open = float(forming["open"])
                side = pending_signal

                print(
                    f"[{now_str()}] EXECUTING PENDING {side} AT NEXT CANDLE OPEN ~ {next_open:.6f}",
                    flush=True
                )
                log.info("Executing pending %s at approx next open %.6f", side, next_open)

                # Safety: recheck position FLAT just before entry
                if get_position_status() != "FLAT":
                    log.warning("Aborting pending entry, position opened meanwhile.")
                    pending_signal = None
                    pending_signal_candle_time = None
                    time.sleep(POLL_INTERVAL)
                    continue

                # ----- ACTUAL ENTRY -----
                try:
                    entry_price = place_entry(side, next_open)
                    tp_id, sl_id, tp_price, sl_price = place_tp_sl(side, entry_price)

                    last_entry_candle_time = candle_time
                    print(
                        f"[{now_str()}] ENTER {side} @ {entry_price:.6f} | "
                        f"TP={tp_price:.6f} SL={sl_price:.6f}",
                        flush=True
                    )
                    log.info(
                        "Enter %s @ %.6f TP=%.6f SL=%.6f",
                        side, entry_price, tp_price, sl_price
                    )

                    # Clear pending after actual entry
                    pending_signal = None
                    pending_signal_candle_time = None

                    # ---- WAIT TILL POSITION CLOSES (TP/SL/MANUAL) ----
                    while True:
                        pos_now = get_position_status()
                        if pos_now == "FLAT":
                            try:
                                ticker = exchange.fetch_ticker(SYMBOL)
                                exit_price = float(ticker.get("last") or ticker.get("close"))
                            except Exception:
                                exit_price = entry_price

                            log_exit(side, entry_price, exit_price)

                            # Cooldown only if closer to SL
                            dist_to_tp = abs(exit_price - tp_price)
                            dist_to_sl = abs(exit_price - sl_price)
                            if dist_to_sl < dist_to_tp:
                                cooldown_until_utc = datetime.now(timezone.utc) + timedelta(
                                    minutes=COOLDOWN_MINUTES
                                )
                                log.info(
                                    "SL-ish exit, cooldown until %s",
                                    cooldown_until_utc.isoformat(),
                                )
                            break

                        time.sleep(1)

                except Exception as e:
                    print(f"[{now_str()}] ⚠️ Pending entry failed: {e}", flush=True)
                    log.error("Pending entry failed: %s", e)
                    # reset pending
                    pending_signal = None
                    pending_signal_candle_time = None

                # after handling pending entry/exit, go next loop
                time.sleep(POLL_INTERVAL)
                continue
            else:
                # pending hai but new candle abhi start nahi hua
                time.sleep(POLL_INTERVAL)
                continue

        # ========= STEP B: CREATE NEW SIGNAL (ONLY WHEN NO PENDING & NO POSITION) =========
        if pending_signal is None and not pd.isna(rsi_prev) and not pd.isna(rsi_curr):

            signal = None

            # BUY: prev RSI < 10, current RSI > 10
            if rsi_prev < RSI_LOW and rsi_curr > RSI_LOW:
                signal = "BUY"
            # SELL: prev RSI > 37, current RSI < 37
            elif rsi_prev > RSI_HIGH and rsi_curr < RSI_HIGH:
                signal = "SELL"

            if signal and last_entry_candle_time == candle_time:
                # is candle par abhi recent trade liya tha -> skip
                signal = None

            if signal:
                # Queue signal for NEXT candle open
                pending_signal = signal
                pending_signal_candle_time = candle_time

                print(
                    f"[{now_str()}] SIGNAL QUEUED {signal} on closed candle @ {close_price:.6f} | "
                    f"RSI(prev={rsi_prev:.2f}, curr={rsi_curr:.2f})",
                    flush=True
                )
                log.info(
                    "Signal queued %s at closed candle price %.6f (RSI prev=%.2f curr=%.2f)",
                    signal, close_price, rsi_prev, rsi_curr
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
