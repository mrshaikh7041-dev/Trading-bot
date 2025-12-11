#!/usr/bin/env python3
"""
Fixed RSI+EMA scalper bot for Binance USDT-M futures
Settings confirmed by user:
 - SYMBOL = "XRP/USDT"
 - EMA_FAST = 13, EMA_SLOW = 34
 - RSI_PERIOD = 14, RSI_LOW = 10, RSI_HIGH = 37
 - LOT_SIZE = 10
 - TP_POINTS = 0.032, SL_POINTS = 0.016
This includes defensive parsing, stop-market SL using closePosition=True, and debug prints.
"""
import ccxt
import pandas as pd
import time
import csv
import os
import logging
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

# ========== EXCHANGE ==========
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.options["adjustForTimeDifference"] = True

# load markets once
try:
    exchange.load_markets()
except Exception as e:
    print(f"[{now_str()}] ⚠️ load_markets failed: {e}")

FUT_SYMBOL = SYMBOL.replace("/", "")

try:
    # set leverage, ignore error if fails
    exchange.set_leverage(75, SYMBOL)
except Exception as e:
    log.warning("set_leverage failed: %s", e)

# ========== STATE ==========
in_position = False
current_position = None           # {"side","entry","time"}
cooldown_until_utc = None
wait_for_zone_exit = False

last_balance_check = None
balance_check_interval = 30

initial_balance = None
current_balance = None

# ========== HELPERS ==========
def safe_get(d, *keys, default=None):
    """Helper to safely traverse nested dicts/lists."""
    try:
        x = d
        for k in keys:
            x = x[k]
        return x
    except Exception:
        return default

def fetch_balance_usdt():
    """Return dict or None"""
    try:
        bal = exchange.fetch_balance()
        # ccxt sometimes returns top-level 'USDT' or inside 'total'
        usdt = bal.get("USDT") or safe_get(bal, "total") and {"free": safe_get(bal, "free", default=0), "used": safe_get(bal, "used", default=0), "total": safe_get(bal, "total", default=0)}
        if not usdt:
            # try 'info' paths (Binance returns 'total' inside)
            info = bal.get("info", {})
            # many shapes exist: check balances list
            return None
        return {
            "free": float(usdt.get("free", 0)),
            "used": float(usdt.get("used", 0)),
            "total": float(usdt.get("total", usdt.get("free", 0) + usdt.get("used", 0)))
        }
    except Exception as e:
        log.error("fetch_balance_usdt error: %s", e)
        return None

def show_balance():
    global initial_balance, current_balance
    b = fetch_balance_usdt()
    if not b:
        print(f"[{now_str()}] ⚠️ BALANCE: unable to fetch", flush=True)
        return
    current_balance = b["total"]
    if initial_balance is None:
        initial_balance = current_balance
    pnl = current_balance - initial_balance
    pct = (pnl / initial_balance * 100) if initial_balance else 0.0
    print(f"[{now_str()}] 💰 BAL: ${current_balance:.2f} | PNL: {pnl:+.2f}$ ({pct:+.2f}%)", flush=True)

def append_csv(row: dict):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)

# ========== INDICATORS ==========
def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(alpha=1/period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    rs = up / down.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

# ========== POSITION INFO ==========
def fetch_position_size():
    """Return positionAmt as float (contracts). Defensive parsing."""
    try:
        bal = exchange.fetch_balance()
        info = bal.get("info", {})
        positions = info.get("positions") or []
        for p in positions:
            # symbol format might be 'XRPUSDT' vs FUT_SYMBOL
            if p.get("symbol") == FUT_SYMBOL:
                return float(p.get("positionAmt", 0) or 0)
    except Exception as e:
        log.error("fetch_position_size error: %s", e)
    return 0.0

# ========== ORDER HELPERS ==========
def cancel_all_open_orders():
    try:
        orders = exchange.fetch_open_orders(SYMBOL)
        for o in orders:
            try:
                exchange.cancel_order(o["id"], SYMBOL)
                print(f"[{now_str()}] ❌ Cancelled order {o.get('id')}", flush=True)
            except Exception as e:
                print(f"[{now_str()}] ⚠️ Cancel order {o.get('id')} failed: {e}", flush=True)
    except Exception as e:
        log.error("fetch_open_orders error: %s", e)

def create_market_entry(side, amount):
    """Place market entry and return entry price (float) or None"""
    try:
        order = exchange.create_order(SYMBOL, "market", side.lower(), amount)
        # ccxt returns different keys: 'average' or 'fills' or 'info'
        avg = order.get("average")
        if avg is None:
            # try info
            info = order.get("info") or {}
            # some exchanges return 'fills' array with price
            fills = info.get("fills") or []
            if fills and isinstance(fills, list) and len(fills) > 0:
                try:
                    avg = float(fills[0].get("price"))
                except Exception:
                    pass
            avg = avg or safe_get(info, "avgPrice") or safe_get(info, "averagePrice")
        if avg is None:
            # fallback: fetch ticker last
            tick = exchange.fetch_ticker(SYMBOL)
            avg = float(tick.get("last") or tick.get("close"))
        return float(avg)
    except Exception as e:
        print(f"[{now_str()}] ❌ Market entry failed: {e}", flush=True)
        log.error("market entry failed: %s", e)
        return None

def place_tp_and_sl(side, amount, entry_price):
    """Place TP limit (reduceOnly) and SL stop_market (closePosition True)."""
    close_side = "sell" if side == "BUY" else "buy"
    # compute prices
    if side == "BUY":
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    tp_ok = False
    sl_ok = False

    # TP as limit reduceOnly
    try:
        resp_tp = exchange.create_order(
            SYMBOL, "limit", close_side, amount, tp_price,
            {"reduceOnly": True, "timeInForce": "GTC"}
        )
        tp_ok = True
        print(f"[{now_str()}] ✅ TP placed {tp_price:.4f} (order id: {safe_get(resp_tp,'id')})", flush=True)
    except Exception as e:
        print(f"[{now_str()}] ⚠️ TP place failed: {e}", flush=True)
        log.error("TP placement failed: %s", e)

    # SL as stop_market with closePosition=True
    # For Binance USDT-M v2, param 'stopPrice' and 'closePosition': True is supported
    try:
        resp_sl = exchange.create_order(
            SYMBOL, "stop_market", close_side, amount, None,
            {"stopPrice": sl_price, "reduceOnly": True, "closePosition": True}
        )
        sl_ok = True
        print(f"[{now_str()}] ✅ SL placed stopPrice={sl_price:.4f} (order id: {safe_get(resp_sl,'id')})", flush=True)
    except Exception as e:
        # try fallback: stop_market with workingType 'MARK_PRICE' or 'TP/SL' variant
        print(f"[{now_str()}] ⚠️ SL place failed (first try): {e}", flush=True)
        log.warning("SL place failed first try: %s", e)
        try:
            resp_sl = exchange.create_order(
                SYMBOL, "stop_market", close_side, amount, None,
                {"stopPrice": sl_price, "reduceOnly": True}
            )
            sl_ok = True
            print(f"[{now_str()}] ✅ SL placed fallback stopPrice={sl_price:.4f}", flush=True)
        except Exception as e2:
            print(f"[{now_str()}] ❌ SL place failed fallback: {e2}", flush=True)
            log.error("SL placement completely failed: %s", e2)

    return tp_ok, sl_ok, tp_price, sl_price

# ========== ENTRY ROUTINE ==========
def safe_enter_trade(signal: str, next_open_price: float):
    global in_position, current_position, cooldown_until_utc

    print(f"[{now_str()}] 🔍 Trying entry: {signal}", flush=True)

    # cooldown check
    now_utc = datetime.now(timezone.utc)
    if cooldown_until_utc and now_utc < cooldown_until_utc:
        print(f"[{now_str()}] 🧊 Cooldown active, blocked entry", flush=True)
        return

    # ensure exchange has no position
    ex_size = fetch_position_size()
    if abs(ex_size) > 1e-8:
        print(f"[{now_str()}] 🔄 Exchange still has position ({ex_size}), skipping entry", flush=True)
        in_position = True
        return

    # create market entry
    entry_price = create_market_entry(signal, LOT_SIZE)
    if entry_price is None:
        print(f"[{now_str()}] ❌ Entry aborted (no entry price)", flush=True)
        return

    # place TP and SL
    tp_ok, sl_ok, tp_price, sl_price = place_tp_and_sl(signal, LOT_SIZE, entry_price)

    # if SL placement failed we should cancel entry (or close position) - BE CAREFUL
    if not sl_ok:
        print(f"[{now_str()}] ⚠️ SL not placed! Attempting to close immediate to avoid unprotected position.", flush=True)
        try:
            # close the position immediately (market opposite)
            close_side = "sell" if signal == "BUY" else "buy"
            exchange.create_order(SYMBOL, "market", close_side, LOT_SIZE)
            print(f"[{now_str()}] ❌ Forced close executed due to missing SL", flush=True)
        except Exception as e:
            print(f"[{now_str()}] ❌ Forced close failed: {e}", flush=True)
        return

    # success - update bot state
    in_position = True
    current_position = {"side": signal, "entry": entry_price, "tp": tp_price, "sl": sl_price, "time": now_str()}
    print(f"[{now_str()}] 🚀 ENTERED {signal} @ {entry_price:.4f} | TP {tp_price:.4f} SL {sl_price:.4f}", flush=True)
    log.info("ENTER %s @ %.4f TP %.4f SL %.4f", signal, entry_price, tp_price, sl_price)

# ========== EXIT HANDLER ==========
def on_position_closed(exit_price: float):
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit
    if current_position is None:
        print(f"[{now_str()}] ⚠️ on_position_closed called but current_position None", flush=True)
        return

    side = current_position.get("side")
    entry = float(current_position.get("entry"))
    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE
    row = {
        "time": now_str(),
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "pnl": round(pnl, 6)
    }
    append_csv(row)
    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f}", flush=True)
    log.info("EXIT %s @ %.4f PNL %.4f", side, exit_price, pnl)

    # cancel any leftover orders
    cancel_all_open_orders()

    # if loss then cooldown
    if pnl < 0:
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        print(f"[{now_str()}] 🧊 Loss cooldown set for {COOLDOWN_MINUTES} minutes", flush=True)
    else:
        # set RSI zone wait so we don't re-enter immediately
        wait_for_zone_exit = True
        print(f"[{now_str()}] ⏳ TP hit or manual close - waiting for RSI zone exit", flush=True)

    in_position = False
    current_position = None

# ========== STARTUP ==========
print(f"[{now_str()}] 🚀 BOT STARTING (EMA filter + robust SL/TP placement)")
show_balance()

# ========== MAIN LOOP ==========
while True:
    try:
        now_utc = datetime.now(timezone.utc)
        # balance print
        if last_balance_check is None or (now_utc - last_balance_check).total_seconds() >= balance_check_interval:
            show_balance()
            last_balance_check = now_utc

        # if bot thinks in position -> poll exchange for real size
        if in_position:
            size = fetch_position_size()
            if abs(size) < 1e-8:
                # position closed on exchange
                try:
                    ticker = exchange.fetch_ticker(SYMBOL)
                    exit_price = float(ticker.get("last") or ticker.get("close"))
                except Exception:
                    exit_price = current_position.get("entry", 0.0)
                on_position_closed(exit_price)
            time.sleep(POLL_INTERVAL)
            continue

        # cooldown block
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            remaining = (cooldown_until_utc - now_utc).total_seconds() / 60
            print(f"[{now_str()}] 🧊 Cooldown: {remaining:.1f} min left", flush=True)
            time.sleep(POLL_INTERVAL)
            continue

        # fetch ohlcv and indicators
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 10)
        if not ohlc or len(ohlc) < RSI_PERIOD + 3:
            print(f"[{now_str()}] ⚠️ Not enough candles, skipping", flush=True)
            time.sleep(POLL_INTERVAL)
            continue

        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)
        df["ema_fast"] = ema(df["close"], EMA_FAST)
        df["ema_slow"] = ema(df["close"], EMA_SLOW)

        prev = df.iloc[-3]
        last = df.iloc[-2]
        next_open = df.iloc[-1]["open"]

        prev_rsi = float(prev["rsi"])
        last_rsi = float(last["rsi"])
        ema_fast_v = float(last["ema_fast"])
        ema_slow_v = float(last["ema_slow"])

        # debug print
        print(f"[{now_str()}] 🔎 prev_rsi={prev_rsi:.2f} last_rsi={last_rsi:.2f} ema_fast={ema_fast_v:.4f} ema_slow={ema_slow_v:.4f}", flush=True)

        # wait for RSI zone exit after TP/manual
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            print(f"[{now_str()}] 🚫 Waiting RSI exit from zone ({RSI_LOW}-{RSI_HIGH})", flush=True)
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False
            print(f"[{now_str()}] ✅ RSI left zone - new entries allowed", flush=True)

        # strategy signal + EMA trend filter
        signal = None
        if prev_rsi < RSI_LOW and (RSI_LOW < last_rsi < RSI_HIGH) and ema_fast_v > ema_slow_v:
            signal = "BUY"
            print(f"[{now_str()}] 📈 Signal BUY", flush=True)
        elif prev_rsi > RSI_HIGH and (RSI_LOW < last_rsi < RSI_HIGH) and ema_fast_v < ema_slow_v:
            signal = "SELL"
            print(f"[{now_str()}] 📉 Signal SELL", flush=True)

        if signal:
            safe_enter_trade(signal, float(next_open))

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ Bot stopping...", flush=True)
        show_balance()
        break
    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(2)
