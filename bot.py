#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta
import math

# ========== USER CONFIG ==========
SYMBOL = "XRP/USDT"
TIMEFRAME = "1m"

LOT_SIZE = 10
RSI_PERIOD = 14
RSI_LOW = 10
RSI_HIGH = 37

TP_POINTS = 0.032
SL_POINTS = 0.016
POLL_INTERVAL = 5
COOLDOWN_MINUTES = 15

EMA_FAST = 13   # you said you may change to 13/34; adjust here if needed
EMA_SLOW = 34

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
current_position = None
cooldown_until_utc = None
wait_for_zone_exit = False

last_balance_check = None
balance_check_interval = 30

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
    pct = (total_profit / initial_balance) * 100 if initial_balance and initial_balance != 0 else 0.0
    print(f"[{now_str()}] 💰 BAL: ${current_balance:.2f} | PNL: ${total_profit:+.2f} ({pct:+.2f}%) | FREE: ${b['free']:.2f}", flush=True)

def append_csv(row: dict):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)

def rsi_wilder(series, period=14):
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100 / (1 + (up / down)))

def fetch_position_size():
    try:
        bal = exchange.fetch_balance()
        for p in bal.get("info", {}).get("positions", []):
            if p.get("symbol") == FUT_SYMBOL:
                return float(p.get("positionAmt", "0"))
    except Exception as e:
        log.error("fetch_position_size error: %s", e)
    return 0.0

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# ========== ALGO ORDER (SL) HELPERS ==========
def place_algo_stop(side: str, quantity: float, stop_price: float, max_retries=5):
    """
    Place a STOP (reduceOnly) order via Binance Algo Orders.
    Tries several CCXT method fallbacks and a raw request fallback.
    Returns response dict on success, raises Exception on final failure.
    """
    params = {
        "symbol": FUT_SYMBOL,          # e.g. XRPUSDT
        "side": side,                  # "BUY" or "SELL"
        "type": "STOP",                # algo type STOP
        "quantity": float(quantity),
        "stopPrice": float(round(stop_price, 8)),  # keep precision
        "reduceOnly": True,
        # optional: "timeInForce": "GTC"
    }

    attempt = 0
    last_exc = None
    while attempt < max_retries:
        attempt += 1
        try:
            # 1) Try CCXT helper name (common)
            if hasattr(exchange, 'fapiPrivatePostAlgoOrder'):
                resp = exchange.fapiPrivatePostAlgoOrder(params)
                print(f"[{now_str()}] ✅ Algo STOP placed via fapiPrivatePostAlgoOrder (attempt {attempt})", flush=True)
                return resp
            # 2) Try sapi variant
            if hasattr(exchange, 'sapiPostAlgoOrder'):
                resp = exchange.sapiPostAlgoOrder(params)
                print(f"[{now_str()}] ✅ Algo STOP placed via sapiPostAlgoOrder (attempt {attempt})", flush=True)
                return resp
            # 3) Try generic request to fapi endpoint
            try:
                resp = exchange.request('fapi/v1/algo/order', 'POST', params)
                print(f"[{now_str()}] ✅ Algo STOP placed via request(fapi/v1/algo/order) (attempt {attempt})", flush=True)
                return resp
            except Exception as e2:
                last_exc = e2
                # fallthrough to logging and retry
                raise e2
        except Exception as e:
            last_exc = e
            print(f"[{now_str()}] ⚠️ Algo STOP place attempt {attempt} failed: {e}", flush=True)
            log.error("Algo STOP attempt %d failed: %s", attempt, e)
            # exponential backoff
            time.sleep(1.0 * attempt)
            continue

    # after retries
    raise Exception(f"Algo STOP placement failed after {max_retries} attempts. Last error: {last_exc}")

# ========== TRADING HELPERS ==========
def can_enter_trade():
    global in_position, cooldown_until_utc, current_position
    now_utc = datetime.now(timezone.utc)

    if cooldown_until_utc and now_utc < cooldown_until_utc:
        remaining = (cooldown_until_utc - now_utc).total_seconds() / 60
        print(f"[{now_str()}] 🧊 Cooldown active: {remaining:.1f} min left", flush=True)
        return False

    ex_size = fetch_position_size()
    if abs(ex_size) > 1e-8:
        # sync state
        side = "BUY" if ex_size > 0 else "SELL"
        if not in_position:
            in_position = True
            current_position = {"side": side, "entry": 0.0, "time": now_str()}
            print(f"[{now_str()}] 🔁 Sync: exchange shows existing position size {ex_size}", flush=True)
        return False

    if in_position and abs(ex_size) < 1e-8:
        # reset if mismatch
        in_position = False

    return not in_position

def place_entry_with_tp_sl(side, approx_price):
    """
    Place market entry, then TP (limit) and SL (algo STOP).
    SL placement uses place_algo_stop with retries.
    If SL cannot be placed, we KEEP position open and keep trying later (do NOT force close).
    """
    close_side = "sell" if side == "BUY" else "buy"
    print(f"[{now_str()}] 🟢 Sending market entry {side} qty {LOT_SIZE}", flush=True)
    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    entry_price = float(order.get("average") or order.get("price") or approx_price)
    print(f"[{now_str()}] ✅ Market entry filled @ {entry_price:.6f}", flush=True)

    # take profit placement (limit reduceOnly)
    if side == "BUY":
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    # Place TP via standard limit reduceOnly (should work)
    try:
        tp_order = exchange.create_order(SYMBOL, "limit", close_side, LOT_SIZE, tp_price, {"reduceOnly": True, "timeInForce": "GTC"})
        print(f"[{now_str()}] ✅ TP limit placed @ {tp_price:.6f}", flush=True)
    except Exception as e:
        print(f"[{now_str()}] ⚠️ TP place failed: {e}", flush=True)
        log.error("TP place failed: %s", e)

    # Place SL via Algo STOP (critical)
    try:
        # Algo side must be opposite close_side? For STOP order: we specify side equal to close_side
        # Binance expects the side of the algo order to close the position (sell to close a buy)
        algo_side = close_side.upper()
        resp = place_algo_stop(algo_side, LOT_SIZE, sl_price, max_retries=5)
        print(f"[{now_str()}] ✅ SL (ALGO) placed @ {sl_price:.6f}", flush=True)
    except Exception as e:
        # CRITICAL: SL failed repeatedly. DO NOT force-close the position.
        print(f"[{now_str()}] ❌ SL Algo placement failed after retries: {e}", flush=True)
        log.error("SL Algo placement final failure: %s", e)
        # Keep position open; set a flag so main loop will attempt again later
        # We store the attempted SL to retry
        return entry_price, tp_price, None

    return entry_price, tp_price, sl_price

def safe_enter_trade(signal: str, next_open_price: float):
    global in_position, current_position
    print(f"[{now_str()}] 🔍 Checking entry for {signal}", flush=True)
    if not can_enter_trade():
        print(f"[{now_str()}] ❌ Entry blocked by can_enter_trade()", flush=True)
        return

    try:
        entry, tp, sl = place_entry_with_tp_sl(signal, next_open_price)
        in_position = True
        current_position = {
            "side": signal,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "time": now_str()
        }
        print(f"[{now_str()}] 🚀 ENTERED {signal} @ {entry:.6f} | TP={tp} SL={sl}", flush=True)
        log.info("Entered %s @ %.6f TP=%.6f SL=%s", signal, entry, tp, sl)
    except Exception as e:
        print(f"[{now_str()}] ❌ Entry failed: {e}", flush=True)
        log.error("Entry failed: %s", e)

def on_position_closed(exit_price: float):
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit
    if current_position is None:
        return

    side = current_position.get("side")
    entry = current_position.get("entry")
    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE

    append_csv({
        "time": current_position.get("time"),
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "pnl": round(pnl, 6),
        "note": "closed"
    })

    # After any close, wait for RSI zone exit
    wait_for_zone_exit = True

    # Cancel leftover TP/SL orders if any (best-effort)
    try:
        for o in exchange.fetch_open_orders(SYMBOL):
            try:
                exchange.cancel_order(o["id"], SYMBOL)
                print(f"[{now_str()}] ❌ Cancelled leftover order {o['id']}", flush=True)
            except Exception as e:
                print(f"[{now_str()}] ⚠️ Cancel failed for {o.get('id')}: {e}", flush=True)
    except Exception as e:
        log.error("fetch_open_orders error on close: %s", e)

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.6f} | PNL={pnl:.6f}", flush=True)
    in_position = False
    current_position = None
    # If PNL negative -> cooldown
    if pnl < 0:
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)

# ========== STARTUP ==========
print(f"[{now_str()}] 🚀 BOT STARTING (ALGO-SL Enabled) Config: RSI({RSI_LOW}-{RSI_HIGH}) EMA({EMA_FAST}/{EMA_SLOW}) TP={TP_POINTS} SL={SL_POINTS}")
show_balance()

# ========== MAIN LOOP ==========
while True:
    try:
        now_utc = datetime.now(timezone.utc)
        if last_balance_check is None or (now_utc - last_balance_check).total_seconds() >= balance_check_interval:
            show_balance()
            last_balance_check = now_utc

        # If in position -> monitor for close and if SL not placed earlier, retry SL placement
        if in_position and current_position:
            # If exchange shows position size 0 -> position closed externally
            size = fetch_position_size()
            if abs(size) < 1e-8:
                try:
                    ticker = exchange.fetch_ticker(SYMBOL)
                    exit_price = float(ticker.get("last") or ticker.get("close"))
                except Exception:
                    exit_price = current_position.get("entry")
                on_position_closed(exit_price)
                time.sleep(POLL_INTERVAL)
                continue

            # If SL was missing (sl==None), attempt to place it again now (background retry)
            if current_position.get("sl") is None:
                # compute candidate sl from stored entry & side
                entry = current_position.get("entry")
                side = current_position.get("side")
                if side == "BUY":
                    sl_candidate = entry - SL_POINTS
                    algo_side = "SELL"
                else:
                    sl_candidate = entry + SL_POINTS
                    algo_side = "BUY"
                print(f"[{now_str()}] 🔁 Retry placing missing SL @ {sl_candidate:.6f}", flush=True)
                try:
                    place_algo_stop(algo_side, LOT_SIZE, sl_candidate, max_retries=5)
                    current_position["sl"] = sl_candidate
                    print(f"[{now_str()}] ✅ SL placed on retry @ {sl_candidate:.6f}", flush=True)
                except Exception as e:
                    print(f"[{now_str()}] ⚠️ SL retry failed: {e} (will retry again later)", flush=True)
                    log.error("SL retry failed: %s", e)
                time.sleep(POLL_INTERVAL)
                continue

            # otherwise just wait
            time.sleep(POLL_INTERVAL)
            continue

        # Out of position: respect cooldown
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            time.sleep(POLL_INTERVAL)
            continue

        # Fetch candles
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)
        df["ema_fast"] = ema(df["close"], EMA_FAST)
        df["ema_slow"] = ema(df["close"], EMA_SLOW)

        if len(df) < 4:
            time.sleep(POLL_INTERVAL)
            continue

        prev_row = df.iloc[-3]
        last_closed = df.iloc[-2]
        entry_candle = df.iloc[-1]

        prev_rsi = float(prev_row["rsi"])
        last_rsi = float(last_closed["rsi"])
        next_open_price = float(entry_candle["open"])
        ema_fast_val = float(last_closed["ema_fast"])
        ema_slow_val = float(last_closed["ema_slow"])

        print(f"[{now_str()}] 🕯 prev_rsi={prev_rsi:.2f} last_rsi={last_rsi:.2f} ema_f={ema_fast_val:.4f} ema_s={ema_slow_val:.4f}", flush=True)

        # wait after TP/manual close until RSI exit
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            print(f"[{now_str()}] 🚫 Waiting for RSI to exit zone before next trade", flush=True)
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False
            print(f"[{now_str()}] ✅ RSI exited zone - new entries allowed", flush=True)

        # Determine signal with EMA filter
        signal = None
        if prev_rsi < RSI_LOW and (RSI_LOW < last_rsi < RSI_HIGH) and (ema_fast_val > ema_slow_val):
            signal = "BUY"
            print(f"[{now_str()}] 📈 BUY signal", flush=True)
        elif prev_rsi > RSI_HIGH and (RSI_LOW < last_rsi < RSI_HIGH) and (ema_fast_val < ema_slow_val):
            signal = "SELL"
            print(f"[{now_str()}] 📉 SELL signal", flush=True)

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
