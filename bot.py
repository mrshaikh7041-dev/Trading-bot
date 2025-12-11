#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
import traceback
from datetime import datetime, timezone, timedelta

# ========== CONFIG ==========
DEBUG = True

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

EMA_FAST = 13
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

def dbg(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs, flush=True)

# ========== EXCHANGE ==========
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.options["adjustForTimeDifference"] = True

try:
    exchange.load_markets()
except Exception as e:
    print(f"[{now_str()}] ⚠️ load_markets failed: {e}")
    traceback.print_exc()

FUT_SYMBOL = SYMBOL.replace("/", "")

try:
    exchange.set_leverage(75, SYMBOL)
except Exception as e:
    log.warning("set_leverage failed: %s", e)
    dbg(f"[{now_str()}] set_leverage warning: {e}")

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
def safe_fetch_balance_raw():
    """Return raw fetch_balance result or None"""
    try:
        return exchange.fetch_balance()
    except Exception as e:
        log.error("fetch_balance error: %s", e)
        if DEBUG:
            traceback.print_exc()
        return None

def fetch_balance_usdt():
    b = safe_fetch_balance_raw()
    if not b:
        return None
    # Some exchanges return top-level keys, some return 'USDT' dict; be defensive
    try:
        if "USDT" in b:
            usdt = b["USDT"]
            return {
                "free": float(usdt.get("free", 0)),
                "used": float(usdt.get("used", 0)),
                "total": float(usdt.get("total", 0))
            }
        # some CCXT builds put balances under b['info']['assets'] etc — try safe fallbacks
        info = b.get("info", {}) if isinstance(b, dict) else {}
        # try to find USDT in info.positions? fallback to totalValue if present
        # best-effort: look for total or equity fields
        if isinstance(b, dict) and "total" in b:
            total = float(b.get("total", 0))
            free = float(b.get("free", 0)) if "free" in b else total
            used = float(b.get("used", 0)) if "used" in b else 0.0
            return {"free": free, "used": used, "total": total}
        return None
    except Exception as e:
        log.error("parse balance error: %s", e)
        if DEBUG:
            traceback.print_exc()
        return None

def show_balance():
    global initial_balance, current_balance, total_profit
    b = fetch_balance_usdt()
    if not b:
        dbg(f"[{now_str()}] ⚠️ Unable to fetch balance (None).")
        return
    current_balance = b["total"]
    if initial_balance is None:
        initial_balance = current_balance
    total_profit = current_balance - initial_balance
    pct = (total_profit / initial_balance) * 100 if initial_balance else 0.0
    print(f"[{now_str()}] 💰 BAL: ${current_balance:.2f} | PNL: ${total_profit:+.2f} ({pct:+.2f}%)", flush=True)

def append_csv(row: dict):
    try:
        exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=row.keys())
            if not exists:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        log.error("append_csv error: %s", e)
        if DEBUG:
            traceback.print_exc()

def rsi_wilder(series, period=14):
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    # avoid division by zero
    with pd.option_context('mode.use_inf_as_na', True):
        rs = up / down.replace({0: pd.NA})
        rs = rs.fillna(0)
    return 100 - (100 / (1 + (up / down.replace({0: pd.NA})).fillna(0)))

def fetch_position_size():
    """Return current positionAmt as float. Safe for different response shapes."""
    try:
        bal = safe_fetch_balance_raw()
        if not bal:
            return 0.0
        info = bal.get("info") if isinstance(bal, dict) else None
        # Newer CCXT returns dictionary with 'positions' inside info
        positions = []
        if info and isinstance(info, dict):
            positions = info.get("positions") or info.get("assets") or []
        # also some versions place positions directly under bal.get('positions')
        if not positions:
            positions = bal.get("positions") or []
        for p in positions:
            # positions may be dict-like with 'symbol' key (e.g., 'XRPUSDT')
            symbol_key = p.get("symbol") if isinstance(p, dict) else None
            if symbol_key and symbol_key.upper() == FUT_SYMBOL.upper():
                amt = p.get("positionAmt") or p.get("positionAmt")
                try:
                    return float(amt)
                except:
                    return 0.0
        return 0.0
    except Exception as e:
        log.error("fetch_position_size error: %s", e)
        if DEBUG:
            traceback.print_exc()
        return 0.0

# EMA CALC
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

# ========== TRADING HELPERS ==========
def can_enter_trade():
    global in_position, cooldown_until_utc, current_position
    now_utc = datetime.now(timezone.utc)

    if cooldown_until_utc and now_utc < cooldown_until_utc:
        remaining = (cooldown_until_utc - now_utc).total_seconds() / 60
        dbg(f"[{now_str()}] 🧊 Cooldown active: {remaining:.1f} min left")
        return False

    ex_size = fetch_position_size()
    dbg(f"[{now_str()}] 🔁 Exchange reported position size: {ex_size}")

    if abs(ex_size) > 1e-8:
        # sync bot state to exchange
        in_position = True
        side = "BUY" if ex_size > 0 else "SELL"
        if current_position is None:
            current_position = {"side": side, "entry": 0.0}
            dbg(f"[{now_str()}] ⚠️ Bot synced: detected existing position on exchange -> {ex_size} ({side})")
        return False

    # if bot thinks it's in position but exchange reports zero => reset
    if in_position and abs(ex_size) < 1e-8:
        dbg(f"[{now_str()}] 🔄 Sync: bot was in_position but exchange size 0 -> reset")
        in_position = False
        current_position = None

    return not in_position

def place_entry_with_tp_sl(side, approx_price):
    close_side = "sell" if side == "BUY" else "buy"
    try:
        dbg(f"[{now_str()}] 📝 Creating market order {side} size={LOT_SIZE}")
        order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
        dbg(f"[{now_str()}] ↪ market order response: {order}")
    except Exception as e:
        log.error("create market order failed: %s", e)
        if DEBUG:
            traceback.print_exc()
        raise

    # entry price: try average, fallback to filledPrice fields or approx_price
    entry_price = approx_price
    try:
        if isinstance(order, dict):
            entry_price = float(order.get("average") or order.get("price") or approx_price)
        else:
            entry_price = float(approx_price)
    except Exception:
        entry_price = approx_price

    # compute tp/sl based on side
    if side == "BUY":
        tp = entry_price + TP_POINTS
        sl = entry_price - SL_POINTS
    else:
        tp = entry_price - TP_POINTS
        sl = entry_price + SL_POINTS

    # place TP (limit reduceOnly)
    try:
        tp_order = exchange.create_order(
            SYMBOL, "limit", close_side, LOT_SIZE, tp,
            {"reduceOnly": True, "timeInForce": "GTC"}
        )
        dbg(f"[{now_str()}] ↪ TP order response: {tp_order}")
    except Exception as e:
        log.error("create TP order failed: %s", e)
        if DEBUG:
            traceback.print_exc()

    # place SL (stop_market reduceOnly)
    try:
        sl_order = exchange.create_order(
            SYMBOL, "stop_market", close_side, LOT_SIZE, None,
            {"stopPrice": sl, "reduceOnly": True}
        )
        dbg(f"[{now_str()}] ↪ SL order response: {sl_order}")
    except Exception as e:
        log.error("create SL order failed: %s", e)
        if DEBUG:
            traceback.print_exc()

    # finally return entry,tp,sl
    return entry_price, tp, sl

def safe_enter_trade(signal, next_open_price):
    global in_position, current_position
    dbg(f"[{now_str()}] 🔍 Checking entry for {signal}")
    if not can_enter_trade():
        dbg(f"[{now_str()}] ❌ Entry blocked by can_enter_trade")
        return
    try:
        entry, tp, sl = place_entry_with_tp_sl(signal, next_open_price)
        in_position = True
        current_position = {"side": signal, "entry": entry}
        print(f"[{now_str()}] 🚀 ENTER {signal} @ {entry:.4f} | TP {tp:.4f} SL {sl:.4f}")
        log.info("Entered %s @ %.4f", signal, entry)
    except Exception as e:
        print(f"[{now_str()}] ❌ Entry failed: {e}")
        if DEBUG:
            traceback.print_exc()

def on_position_closed(exit_price):
    global in_position, current_position, wait_for_zone_exit, cooldown_until_utc
    if current_position is None:
        dbg(f"[{now_str()}] ⚠️ on_position_closed called but current_position is None")
        return
    side = current_position.get("side")
    entry = float(current_position.get("entry", 0.0))
    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE

    append_csv({
        "time": now_str(),
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "pnl": round(pnl, 6)
    })

    # set wait flag
    wait_for_zone_exit = True

    # cancel leftover orders if any
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL) or []
        dbg(f"[{now_str()}] 🔎 open_orders before cancel: {open_orders}")
        for o in open_orders:
            try:
                exchange.cancel_order(o.get("id"), SYMBOL)
                dbg(f"[{now_str()}] ❌ Cancelled leftover order {o.get('id')}")
            except Exception as e:
                dbg(f"[{now_str()}] ⚠️ Cancel failed for {o.get('id')}: {e}")
    except Exception as e:
        log.error("fetch_open_orders/cancel error: %s", e)
        if DEBUG:
            traceback.print_exc()

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f}")
    in_position = False
    current_position = None
    # impose cooldown after losing trade
    if pnl < 0:
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        dbg(f"[{now_str()}] 🧊 Loss cooldown set until {cooldown_until_utc.isoformat()}")

# ========== STARTUP ==========
print(f"[{now_str()}] 🚀 BOT STARTING (EMA filter + debug={'ON' if DEBUG else 'OFF'})")
show_balance()

# ========== MAIN LOOP ==========
while True:
    try:
        now_utc = datetime.now(timezone.utc)
        if last_balance_check is None or (now_utc - last_balance_check).total_seconds() >= balance_check_interval:
            show_balance()
            last_balance_check = now_utc

        # if in-position: watch for close
        if in_position:
            size = fetch_position_size()
            if abs(size) < 1e-8:
                # if exchange reports closed, get last price and call close handler
                try:
                    ticker = exchange.fetch_ticker(SYMBOL)
                    exit_price = float(ticker.get("last") or ticker.get("close"))
                except Exception:
                    exit_price = current_position.get("entry", 0.0)
                on_position_closed(exit_price)
            time.sleep(POLL_INTERVAL)
            continue

        # cooldown check
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            remaining = (cooldown_until_utc - now_utc).total_seconds() / 60
            dbg(f"[{now_str()}] 🧊 Cooldown active ({remaining:.1f} min), skipping entries")
            time.sleep(POLL_INTERVAL)
            continue

        # fetch OHLCV safe
        try:
            ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit= max(RSI_PERIOD + 5, 50))
        except Exception as e:
            dbg(f"[{now_str()}] ⚠️ fetch_ohlcv failed: {e}")
            if DEBUG:
                traceback.print_exc()
            time.sleep(POLL_INTERVAL)
            continue

        if not ohlc or len(ohlc) < RSI_PERIOD + 3:
            dbg(f"[{now_str()}] ⚠️ Not enough OHLCV data, got {len(ohlc) if ohlc else 0} rows")
            time.sleep(POLL_INTERVAL)
            continue

        df = pd.DataFrame(ohlc, columns=["t","o","h","l","c","v"])
        df["rsi"] = rsi_wilder(df["c"], RSI_PERIOD)
        df["ema_fast"] = ema(df["c"], EMA_FAST)
        df["ema_slow"] = ema(df["c"], EMA_SLOW)

        prev = df.iloc[-3]
        last = df.iloc[-2]
        next_open = df.iloc[-1]["o"]

        prev_rsi = float(prev["rsi"]) if pd.notna(prev["rsi"]) else 0.0
        last_rsi = float(last["rsi"]) if pd.notna(last["rsi"]) else 0.0
        ema_fast = float(last["ema_fast"]) if pd.notna(last["ema_fast"]) else 0.0
        ema_slow = float(last["ema_slow"]) if pd.notna(last["ema_slow"]) else 0.0

        dbg(f"[{now_str()}] 🕯 prev_rsi={prev_rsi:.2f} last_rsi={last_rsi:.2f} ema_fast={ema_fast:.4f} ema_slow={ema_slow:.4f} next_open={next_open:.4f}")

        # WAIT after TP/manual
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            dbg(f"[{now_str()}] 🚫 Waiting for RSI to exit zone before next trade")
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False
            dbg(f"[{now_str()}] ✅ RSI exited zone - new entries allowed")

        signal = None

        # === BUY CONDITION ===
        if (prev_rsi < RSI_LOW and RSI_LOW < last_rsi < RSI_HIGH and ema_fast > ema_slow):
            signal = "BUY"

        # === SELL CONDITION ===
        elif (prev_rsi > RSI_HIGH and RSI_LOW < last_rsi < RSI_HIGH and ema_fast < ema_slow):
            signal = "SELL"

        dbg(f"[{now_str()}] 🔔 signal={signal}")

        if signal:
            safe_enter_trade(signal, float(next_open))

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ Bot stopping...", flush=True)
        show_balance()
        break
    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}")
        log.error("Loop error: %s", e)
        if DEBUG:
            traceback.print_exc()
        time.sleep(3)
