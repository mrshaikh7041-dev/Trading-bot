#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# ========== USER CONFIG ==========
SYMBOL = "BNB/USDT"
TIMEFRAME = "1m"

LOT_SIZE = 0.05
RSI_PERIOD = 14
RSI_LOW = 10
RSI_HIGH = 37

TP_POINTS = 8
SL_POINTS = 4
POLL_INTERVAL = 5
COOLDOWN_MINUTES = 15

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# ========== LOGGING ==========
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ========== TIME HELPERS ==========
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")

# ========== EXCHANGE ==========
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.options["adjustForTimeDifference"] = True
exchange.load_markets()

FUT_SYMBOL = SYMBOL.replace("/", "")

try:
    exchange.set_leverage(75, SYMBOL)
except Exception as e:
    log.warning("set_leverage failed: %s", e)

# ========== STATE ==========
in_position = False
current_position = None           # {side, entry, time}
cooldown_until_utc = None         # SL ke baad time-based cooldown
wait_for_zone_exit = False        # TP / manual ke baad RSI zone se bahar ka wait

last_balance_check = None
balance_check_interval = 30       # seconds

initial_balance = None
current_balance = None
total_profit = 0.0

# ========== HELPERS ==========

def fetch_balance_usdt():
    try:
        bal = exchange.fetch_balance()
        usdt = bal["USDT"]
        return {
            "free": float(usdt["free"]),
            "used": float(usdt["used"]),
            "total": float(usdt["total"])
        }
    except Exception as e:
        log.error("Balance fetch error: %s", e)
        return None

def show_balance():
    global initial_balance, current_balance, total_profit

    b = fetch_balance_usdt()
    if not b:
        return

    current_balance = b["total"]
    if initial_balance is None:
        initial_balance = current_balance

    total_profit = current_balance - initial_balance
    pct = (total_profit / initial_balance) * 100 if initial_balance > 0 else 0.0

    print(
        f"[{now_str()}] 💰 BALANCE: ${current_balance:.2f} | "
        f"PNL: ${total_profit:+.2f} ({pct:+.2f}%) | "
        f"USED: ${b['used']:.2f} | FREE: ${b['free']:.2f}",
        flush=True
    )

def append_csv(row: dict):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)

def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100 / (1 + (up / down)))

def fetch_position_size() -> float:
    """Binance USDT-M futures position size (contracts)"""
    try:
        bal = exchange.fetch_balance()
        positions = bal.get("info", {}).get("positions", [])
        for p in positions:
            if p.get("symbol") == FUT_SYMBOL:
                return float(p.get("positionAmt", "0"))
    except Exception as e:
        log.error("fetch_position_size error: %s", e)
    return 0.0

# ========== TRADING HELPERS ==========

def can_enter_trade() -> bool:
    """Cooldown + no open position (exchange + bot state)"""
    global in_position, cooldown_until_utc, current_position

    now_utc = datetime.now(timezone.utc)

    # Time-based cooldown
    if cooldown_until_utc and now_utc < cooldown_until_utc:
        remaining = (cooldown_until_utc - now_utc).total_seconds() / 60
        print(f"[{now_str()}] 🧊 Cooldown active: {remaining:.1f} min left", flush=True)
        return False

    # Exchange reality
    ex_size = fetch_position_size()
    if abs(ex_size) > 1e-8:
        side = "BUY" if ex_size > 0 else "SELL"
        if not in_position:
            print(f"[{now_str()}] 🔄 Sync: exchange has position size={ex_size:.4f}", flush=True)
            in_position = True
            current_position = {
                "side": side,
                "entry": 0.0,
                "time": now_ist().isoformat()
            }
        return False

    # Bot thinks in position but exchange size 0 -> reset
    if in_position and abs(ex_size) < 1e-8:
        in_position = False
        current_position = None

    return not in_position

def place_entry_with_tp_sl(side: str, approx_price: float):
    close_side = "sell" if side == "BUY" else "buy"

    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    entry_price = float(order.get("average") or order.get("price") or approx_price)

    if side == "BUY":
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    # TP
    exchange.create_order(
        SYMBOL, "limit", close_side, LOT_SIZE, tp_price,
        {"reduceOnly": True, "timeInForce": "GTC"}
    )

    # SL
    exchange.create_order(
        SYMBOL, "stop_market", close_side, LOT_SIZE, None,
        {"stopPrice": sl_price, "reduceOnly": True}
    )

    return entry_price, tp_price, sl_price

def safe_enter_trade(signal: str, next_open_price: float):
    global in_position, current_position

    print(f"[{now_str()}] 🔍 Checking entry for {signal}", flush=True)
    if not can_enter_trade():
        print(f"[{now_str()}] ❌ Entry blocked by can_enter_trade", flush=True)
        return

    try:
        entry, tp, sl = place_entry_with_tp_sl(signal, next_open_price)
        in_position = True
        current_position = {
            "side": signal,
            "entry": entry,
            "time": now_ist().isoformat()
        }
        print(
            f"[{now_str()}] 🚀 ENTERED {signal} @ {entry:.4f} | TP={tp:.4f} SL={sl:.4f}",
            flush=True
        )
        log.info("Entered %s @ %.4f", signal, entry)
    except Exception as e:
        print(f"[{now_str()}] ❌ Entry failed: {e}", flush=True)
        log.error("Entry failed: %s", e)

def on_position_closed(exit_price: float):
    """Position close handler: PNL calc, cooldown/zone rules, cancel leftover TP/SL"""
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit

    if current_position is None:
        return

    side = current_position["side"]
    entry = current_position["entry"]

    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE

    # Reason guess nahi kar rahe (TP/SL), simple "AUTO"
    reason = "AUTO"

    append_csv({
        "time": current_position["time"],
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "pnl": round(pnl, 6),
        "reason": reason
    })

    # After auto close: RSI-exit-wait enable
    wait_for_zone_exit = True

    # Cancel leftover TP/SL
    try:
        for o in exchange.fetch_open_orders(SYMBOL):
            try:
                exchange.cancel_order(o["id"], SYMBOL)
                print(f"[{now_str()}] ❌ Cancelled leftover order {o['id']}", flush=True)
            except Exception as e:
                print(f"[{now_str()}] ⚠️ Cancel failed: {e}", flush=True)
    except Exception as e:
        log.error("fetch_open_orders error: %s", e)

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f}", flush=True)
    in_position = False
    current_position = None

# ========== STARTUP ==========
print(f"[{now_str()}] 🚀 RSI REVERSAL BOT STARTING...")
print(f"[{now_str()}] ⚙️ Config: RSI({RSI_LOW}-{RSI_HIGH}) TP={TP_POINTS} SL={SL_POINTS}")
show_balance()

# ========== MAIN LOOP ==========
while True:
    try:
        # Balance print
        now_utc = datetime.now(timezone.utc)
        if last_balance_check is None or (now_utc - last_balance_check).total_seconds() >= balance_check_interval:
            show_balance()
            last_balance_check = now_utc

        # If currently in a trade -> just watch for close
        if in_position:
            size = fetch_position_size()
            if abs(size) < 1e-8:
                # Position actually closed on exchange
                try:
                    ticker = exchange.fetch_ticker(SYMBOL)
                    exit_price = float(ticker.get("last") or ticker.get("close"))
                except Exception:
                    exit_price = current_position["entry"]
                on_position_closed(exit_price)
            time.sleep(POLL_INTERVAL)
            continue

        # Out of position, and not in cooldown -> look for signal
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            time.sleep(POLL_INTERVAL)
            continue

        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        if len(df) < 4:
            time.sleep(POLL_INTERVAL)
            continue

        prev_row = df.iloc[-3]      # i-1
        last_closed = df.iloc[-2]   # i
        entry_candle = df.iloc[-1]  # i+1 (next open)

        prev_rsi = float(prev_row["rsi"])
        last_rsi = float(last_closed["rsi"])
        next_open_price = float(entry_candle["open"])

        print(
            f"[{now_str()}] 🕯 RSI prev={prev_rsi:.2f} last={last_rsi:.2f} | zone=({RSI_LOW}-{RSI_HIGH})",
            flush=True
        )

        # TP / manual ke baad jab tak RSI zone ke andar hai -> entry block
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            print(f"[{now_str()}] 🚫 Waiting for RSI to exit zone before next trade", flush=True)
            time.sleep(POLL_INTERVAL)
            continue

        # RSI zone se bahar aa gaya -> fresh entries allowed
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False
            print(f"[{now_str()}] ✅ RSI exited zone - new entries allowed", flush=True)

        # ---- BACKTEST STRATEGY ----
        signal = None
        if prev_rsi < RSI_LOW and (RSI_LOW < last_rsi < RSI_HIGH):
            signal = "BUY"
            print(f"[{now_str()}] 📈 BUY signal (cross up into zone)", flush=True)
        elif prev_rsi > RSI_HIGH and (RSI_LOW < last_rsi < RSI_HIGH):
            signal = "SELL"
            print(f"[{now_str()}] 📉 SELL signal (cross down into zone)", flush=True)
        # ---------------------------

        if signal:
            safe_enter_trade(signal, next_open_price)

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ Bot stopping...", flush=True)
        show_balance()
        break
    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(3)
