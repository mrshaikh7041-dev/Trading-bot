#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
import traceback
from datetime import datetime, timezone, timedelta

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

EMA_FAST = 13   # you wanted scalping: default example
EMA_SLOW = 34

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

# (you said keys are fake — replace if needed)
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
def now_ist(): return datetime.now(timezone.utc).astimezone(IST)
def now_str(): return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")

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

# ========== HELPERS ==========
def safe_print(*args, **kwargs):
    print(f"[{now_str()}]", *args, **kwargs, flush=True)

def fetch_balance_usdt():
    try:
        bal = exchange.fetch_balance()
        usdt = bal.get("USDT") or bal.get("USDT.T") or {}
        return {
            "free": float(usdt.get("free", 0.0)),
            "used": float(usdt.get("used", 0.0)),
            "total": float(usdt.get("total", 0.0))
        }
    except Exception as e:
        log.error("Balance fetch error: %s", e)
        return None

def show_balance():
    global initial_balance, current_balance
    b = fetch_balance_usdt()
    if not b:
        safe_print("⚠️ Balance unavailable")
        return
    current_balance = b["total"]
    if initial_balance is None:
        initial_balance = current_balance
    pnl = current_balance - initial_balance
    pct = (pnl / initial_balance) * 100 if initial_balance else 0.0
    safe_print(f"💰 BAL: ${current_balance:.2f} | PNL: ${pnl:+.2f} ({pct:+.2f}%) | FREE {b['free']:.2f}")

def append_csv(row: dict):
    exists = os.path.isfile(CSV_FILE)
    try:
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=row.keys())
            if not exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.error("append_csv error: %s", e)

def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100 / (1 + (up / down)))

def ema(series: pd.Series, period: int):
    return series.ewm(span=period, adjust=False).mean()

def fetch_position_size():
    try:
        bal = exchange.fetch_balance()
        info_positions = bal.get("info", {}).get("positions", [])
        for p in info_positions:
            if p.get("symbol") == FUT_SYMBOL:
                return float(p.get("positionAmt", "0") or 0)
    except Exception as e:
        log.error("fetch_position_size error: %s", e)
    return 0.0

# ========== ORDER HELPERS ==========
def cancel_all_open_orders_safe():
    try:
        orders = exchange.fetch_open_orders(SYMBOL)
        for o in orders:
            try:
                exchange.cancel_order(o['id'], SYMBOL)
                safe_print(f"❌ Cancelled order {o.get('id')}")
            except Exception as ee:
                safe_print("⚠ cancel failed for", o.get('id'), ee)
    except Exception as e:
        log.error("fetch_open_orders error: %s", e)

def fetch_last_price():
    try:
        t = exchange.fetch_ticker(SYMBOL)
        return float(t.get("last") or t.get("close") or 0.0)
    except:
        return 0.0

def place_entry_with_tp_sl(side: str, approx_price: float):
    """
    Place market entry then place TP (limit reduceOnly) and SL (stop_market reduceOnly).
    Returns (entry_price, tp_price, sl_price) or raises.
    """
    close_side = "sell" if side == "BUY" else "buy"
    try:
        order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    except Exception as e:
        # some exchanges return different errors — log and rethrow
        log.error("market order failed: %s", e)
        raise

    # get executed average or fallback to approx/fetch
    entry_price = None
    try:
        entry_price = float(order.get("average") or order.get("price") or approx_price)
    except Exception:
        entry_price = approx_price

    if not entry_price or entry_price == 0:
        # try fetch last price
        entry_price = fetch_last_price()

    if side == "BUY":
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    # place TP (limit reduceOnly)
    try:
        exchange.create_order(
            SYMBOL, "limit", close_side, LOT_SIZE, round(tp_price, 8),
            {"reduceOnly": True, "timeInForce": "GTC"}
        )
    except Exception as e:
        log.error("TP order failed: %s", e)
        # continue — we still try to place SL

    # place SL (stop_market reduceOnly)
    try:
        exchange.create_order(
            SYMBOL, "stop_market", close_side, LOT_SIZE, None,
            {"stopPrice": round(sl_price, 8), "reduceOnly": True}
        )
    except Exception as e:
        log.error("SL order failed: %s", e)
        # If SL failed, we must be careful; but return values for bookkeeping.

    return entry_price, tp_price, sl_price

# ========== TRADING LOGIC ==========
def can_enter_trade():
    global in_position, cooldown_until_utc, current_position
    now_utc = datetime.now(timezone.utc)

    if cooldown_until_utc and now_utc < cooldown_until_utc:
        safe_print("🧊 Cooldown active — no entry")
        return False

    ex_size = fetch_position_size()
    if abs(ex_size) > 1e-8:
        # exchange has position - sync
        side = "BUY" if ex_size > 0 else "SELL"
        if not in_position:
            safe_print(f"🔁 Syncing: exchange position exists size={ex_size:.4f} -> set in_position True")
            in_position = True
            current_position = {"side": side, "entry": 0.0, "time": now_str()}
        return False

    # reset bot state if mismatch
    if in_position and abs(ex_size) < 1e-8:
        safe_print("🔄 Bot thought in_position but exchange size 0 -> resetting")
        in_position = False

    return not in_position

def safe_enter_trade(signal: str, signal_candle: dict, next_open_price: float):
    """
    Confirm breakout: next_open must break signal candle high (BUY) or low (SELL)
    Then place market entry + TP/SL
    """
    global in_position, current_position, cooldown_until_utc

    safe_print(f"🔍 Checking entry {signal}, signal_candle H:{signal_candle['high']:.6f} L:{signal_candle['low']:.6f} next_open:{next_open_price:.6f}")

    if not can_enter_trade():
        safe_print("❌ Entry blocked by can_enter_trade")
        return

    # confirmation: breakout of signal candle
    if signal == "BUY":
        if next_open_price <= float(signal_candle["high"]):
            safe_print("❌ BUY blocked - next open did not break signal high")
            return
    else:  # SELL
        if next_open_price >= float(signal_candle["low"]):
            safe_print("❌ SELL blocked - next open did not break signal low")
            return

    # passed confirmation -> place orders
    try:
        entry, tp, sl = place_entry_with_tp_sl(signal, next_open_price)
        in_position = True
        current_position = {"side": signal, "entry": entry, "time": now_str()}
        safe_print(f"✅ ENTERED {signal} @ {entry:.6f} | TP={tp:.6f} SL={sl:.6f}")
        log.info("Entered %s @ %.6f", signal, entry)
    except Exception as e:
        safe_print("❌ Entry exception:", e)
        log.error("Entry exception: %s\n%s", e, traceback.format_exc())

def on_position_closed(exit_price: float):
    """
    Handle position closed event: compute pnl, write csv, enable RSI wait, cancel leftover orders.
    """
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit

    if current_position is None:
        safe_print("⚠ on_position_closed called but current_position is None")
        return

    side = current_position["side"]
    entry = current_position["entry"]
    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE

    append_csv({
        "time": now_str(),
        "side": side,
        "entry": round(entry, 8),
        "exit": round(exit_price, 8),
        "pnl": round(pnl, 8),
        "note": "AUTO_CLOSE"
    })

    # if loss -> cooldown
    if pnl < 0:
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        safe_print(f"🔥 Loss detected -> cooldown until {cooldown_until_utc.isoformat()}")
    else:
        wait_for_zone_exit = True
        safe_print("✅ Profit -> enabling zone wait before new entries")

    # cancel any leftover orders
    cancel_all_open_orders_safe()

    safe_print(f"📊 EXIT {side} @ {exit_price:.6f} | PNL={pnl:.6f}")
    in_position = False
    current_position = None

# ========== STARTUP ==========
safe_print("🚀 BOT STARTING (RSI REV + EMA + BREAKOUT CONFIRMATION)")
safe_print(f"Config: RSI({RSI_LOW}-{RSI_HIGH}), EMA {EMA_FAST}/{EMA_SLOW}, TP {TP_POINTS}, SL {SL_POINTS}")
show_balance()

# ========== MAIN LOOP ==========
while True:
    try:
        now_utc = datetime.now(timezone.utc)
        if last_balance_check is None or (now_utc - (last_balance_check or now_utc)).total_seconds() >= balance_check_interval:
            show_balance()
            last_balance_check = now_utc

        # if bot thinks in position -> wait for exchange to close
        if in_position:
            size = fetch_position_size()
            if abs(size) < 1e-8:
                # position closed on exchange
                exit_price = fetch_last_price() or current_position.get("entry", 0.0)
                on_position_closed(exit_price)
            time.sleep(POLL_INTERVAL)
            continue

        # cooldown check
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            remaining = (cooldown_until_utc - now_utc).total_seconds() / 60
            safe_print(f"🧊 Cooldown active ({remaining:.1f} min left)")
            time.sleep(POLL_INTERVAL)
            continue

        # fetch ohlcv
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=max(RSI_PERIOD + 5, EMA_SLOW + 5))
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])

        if len(df) < max(RSI_PERIOD + 3, EMA_SLOW + 3):
            safe_print("⚠ Not enough candles yet")
            time.sleep(POLL_INTERVAL)
            continue

        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)
        df["ema_fast"] = ema(df["close"], EMA_FAST)
        df["ema_slow"] = ema(df["close"], EMA_SLOW)

        # pick candles: prev(signal candle index = -3), last closed = -2, next_open candle = -1
        prev = df.iloc[-3]      # this is the candle before last closed
        last_closed = df.iloc[-2]   # signal candle
        next_candle = df.iloc[-1]   # the candle to use open for breakout confirmation

        prev_rsi = float(prev["rsi"])
        last_rsi = float(last_closed["rsi"])
        next_open = float(next_candle["open"])
        ema_fast_val = float(last_closed["ema_fast"])
        ema_slow_val = float(last_closed["ema_slow"])

        safe_print(f"prev_rsi={prev_rsi:.2f} last_rsi={last_rsi:.2f} ema_f={ema_fast_val:.4f} ema_s={ema_slow_val:.4f} next_open={next_open:.6f}")

        # wait_for_zone_exit logic (after TP / manual)
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            safe_print("🚫 Waiting for RSI to exit zone before next trade")
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False
            safe_print("✅ RSI exited zone - new entries allowed")

        # signal detection
        signal = None
        if prev_rsi < RSI_LOW and (RSI_LOW < last_rsi < RSI_HIGH):
            # potential BUY
            if ema_fast_val > ema_slow_val:
                signal = "BUY"
                safe_print("📈 Condition: RSI cross into zone + EMA up -> BUY candidate")
            else:
                safe_print("📉 BUY candidate but EMA not up -> blocked")
        elif prev_rsi > RSI_HIGH and (RSI_LOW < last_rsi < RSI_HIGH):
            # potential SELL
            if ema_fast_val < ema_slow_val:
                signal = "SELL"
                safe_print("📉 Condition: RSI cross into zone + EMA down -> SELL candidate")
            else:
                safe_print("📈 SELL candidate but EMA not down -> blocked")

        # If we have a signal -> require breakout confirmation using next_open vs last_closed's high/low
        if signal:
            signal_candle = {"high": float(last_closed["high"]), "low": float(last_closed["low"])}
            safe_enter_trade(signal, signal_candle, next_open)

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        safe_print("⛔ Bot stopped by user")
        show_balance()
        break

    except Exception as e:
        safe_print("⚠️ Loop error:", e)
        log.error("Loop error: %s\n%s", e, traceback.format_exc())
        time.sleep(3)
