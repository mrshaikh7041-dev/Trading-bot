#!/usr/bin/env python3
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

EMA_FAST = 13     # you said you prefer 13/34 for scalping
EMA_SLOW = 34

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

# ----- put your keys (you said they're fake for dev) -----
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

# ========== EXCHANGE SETUP ==========
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
def safe_get(d, *keys, default=None):
    try:
        for k in keys:
            d = d[k]
        return d
    except Exception:
        return default

def fetch_balance_usdt():
    try:
        bal = exchange.fetch_balance()
        # try unified structure
        usdt = None
        if isinstance(bal, dict):
            if "USDT" in bal:
                usdt = bal["USDT"]
            elif "total" in bal and isinstance(bal["total"], dict) and "USDT" in bal["total"]:
                usdt = bal["total"]["USDT"]
            else:
                # older ccxt returns nested "info"
                info = bal.get("info", {})
                # some endpoints return 'assets' or 'account'
                # fallback: try find USDT in top-level keys
                if "USDT" in bal:
                    usdt = bal["USDT"]
        if usdt is None:
            # fallback to parsing 'info' positions for walletBalance
            info = bal.get("info", {})
            if "total" in info and isinstance(info["total"], (int,float)):
                return {"free": float(bal.get("free",0)), "used": float(bal.get("used",0)), "total": float(bal.get("total",0))}
            # last resort: try 'free'/'used' numbers
            return {"free": float(bal.get("free",0)), "used": float(bal.get("used",0)), "total": float(bal.get("total",0))}
        return {
            "free": float(usdt.get("free", 0.0)),
            "used": float(usdt.get("used", 0.0)),
            "total": float(usdt.get("total", 0.0))
        }
    except Exception as e:
        log.error("Balance fetch error: %s", e)
        return None

def show_balance():
    global initial_balance, current_balance, total_profit
    b = fetch_balance_usdt()
    if not b:
        print(f"[{now_str()}] ⚠️ BAL fetch failed", flush=True)
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
        info = bal.get("info", {})
        positions = info.get("positions") or safe_get(bal, "info", "positions") or []
        for p in positions:
            # symbol in positions usually like "XRPUSDT"
            if p.get("symbol") == FUT_SYMBOL:
                return float(p.get("positionAmt", 0.0))
    except Exception as e:
        log.debug("fetch_position_size error: %s", e)
    return 0.0

# ========== ORDER / SL PLACEMENT with FALLBACKS ==========
def cancel_all_open_orders():
    try:
        orders = exchange.fetch_open_orders(SYMBOL)
        for o in orders:
            try:
                exchange.cancel_order(o['id'], SYMBOL)
            except Exception as e:
                log.warning("cancel order fail %s: %s", o.get('id'), e)
    except Exception as e:
        log.debug("fetch_open_orders fail: %s", e)

def place_tp(side_close, amount, tp_price):
    try:
        # regular limit reduceOnly TP
        return exchange.create_order(SYMBOL, 'limit', side_close, amount, tp_price, {'reduceOnly': True, 'timeInForce': 'GTC'})
    except Exception as e:
        log.warning("TP place failed: %s", e)
        return None

def place_sl_fallbacks(side_close, amount, sl_price):
    """
    Try 3 ways to place SL (stop-market primary, alt stop type, stop-limit fallback).
    Returns order if succeeded else None and final exception list.
    """
    exceptions = []
    # ATTEMPT 1: stop_market (preferred)
    try:
        o = exchange.create_order(SYMBOL, 'stop_market', side_close, amount, None, {'stopPrice': sl_price, 'reduceOnly': True})
        log.info("SL placed (stop_market) %s", o)
        return o, exceptions
    except Exception as e:
        exceptions.append(("stop_market", str(e)))
        log.warning("SL stop_market failed: %s", e)

    time.sleep(0.5)

    # ATTEMPT 2: try 'stop' / 'STOP' hybrid (some ccxt/binance variants accept it)
    try:
        o = exchange.create_order(SYMBOL, 'stop', side_close, amount, None, {'stopPrice': sl_price, 'reduceOnly': True})
        log.info("SL placed (stop) %s", o)
        return o, exceptions
    except Exception as e:
        exceptions.append(("stop", str(e)))
        log.warning("SL stop failed: %s", e)

    time.sleep(0.5)

    # ATTEMPT 3: stop-limit style (limit close at SL or slightly offset)
    try:
        # place a limit close at SL_price with reduceOnly as fallback
        o = exchange.create_order(SYMBOL, 'limit', side_close, amount, sl_price, {'reduceOnly': True, 'timeInForce':'GTC'})
        log.info("SL placed (limit fallback) %s", o)
        return o, exceptions
    except Exception as e:
        exceptions.append(("limit_fallback", str(e)))
        log.warning("SL limit fallback failed: %s", e)

    return None, exceptions

def place_entry_with_tp_sl(side, approx_price):
    """
    Places market entry then TP and SL with fallback logic for SL.
    Returns (entry_price, tp_price, sl_price, succeeded_bool)
    """
    close_side = 'sell' if side == 'BUY' else 'buy'
    try:
        order = exchange.create_order(SYMBOL, 'market', side.lower(), LOT_SIZE)
    except Exception as e:
        log.error("Market entry failed: %s", e)
        raise

    # attempt to get average/price
    entry_price = float(order.get('average') or order.get('price') or approx_price or 0.0)

    if side == 'BUY':
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    # Place TP (limit reduceOnly) - usually works
    tp_order = None
    try:
        tp_order = place_tp(close_side, LOT_SIZE, tp_price)
    except Exception as e:
        log.warning("TP place problematic: %s", e)

    # Place SL with multiple fallback attempts
    sl_order, exceptions = place_sl_fallbacks(close_side, LOT_SIZE, sl_price)
    if sl_order is None:
        # SL placement failed across methods -> do not force close immediately.
        # Log exceptions and return with flag so caller can decide.
        log.error("All SL placement methods failed. Exceptions: %s", exceptions)
        return entry_price, tp_price, sl_price, False, exceptions

    return entry_price, tp_price, sl_price, True, None

# ========== ON POSITION CLOSED ==========
def on_position_closed(exit_price: float):
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit
    if current_position is None:
        return
    side = current_position["side"]
    entry = current_position["entry"]
    pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE
    append_csv({
        "time": current_position.get("time", now_str()),
        "side": side,
        "entry": entry,
        "exit": exit_price,
        "pnl": round(pnl,6),
        "note": "AUTO-CLOSE"
    })
    wait_for_zone_exit = True
    # cancel leftover orders
    cancel_all_open_orders()
    in_position = False
    current_position = None
    # cooldown on negative pnl
    if pnl < 0:
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        print(f"[{now_str()}] 🧊 Cooldown set for {COOLDOWN_MINUTES} minutes due to losing trade.", flush=True)

# ========== STARTUP ==========
print(f"[{now_str()}] 🚀 RSI REV BOT STARTING (EMA filter + SL-fallbacks)", flush=True)
print(f"[{now_str()}] ⚙️ Config: RSI({RSI_LOW}-{RSI_HIGH}) EMA({EMA_FAST}/{EMA_SLOW}) TP={TP_POINTS} SL={SL_POINTS}", flush=True)
show_balance()

# ========== MAIN LOOP ==========
while True:
    try:
        now_utc = datetime.now(timezone.utc)
        if last_balance_check is None or (now_utc - last_balance_check).total_seconds() >= balance_check_interval:
            show_balance()
            last_balance_check = now_utc

        # if we think we're in a trade, watch for actual exchange position closed
        if in_position:
            size = fetch_position_size()
            if abs(size) < 1e-8:
                # position closed on exchange
                try:
                    ticker = exchange.fetch_ticker(SYMBOL)
                    exit_price = float(ticker.get('last') or ticker.get('close'))
                except Exception:
                    exit_price = current_position.get('entry', 0.0)
                on_position_closed(exit_price)
            time.sleep(POLL_INTERVAL)
            continue

        # Cooldown check
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            remain = (cooldown_until_utc - now_utc).total_seconds()/60
            print(f"[{now_str()}] 🧊 Cooldown active: {remain:.1f} minutes", flush=True)
            time.sleep(POLL_INTERVAL)
            continue

        # fetch candles
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=max(RSI_PERIOD+5, 50))
        df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","volume"])

        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)
        df["ema_fast"] = ema(df["close"], EMA_FAST)
        df["ema_slow"] = ema(df["close"], EMA_SLOW)

        if len(df) < 4:
            time.sleep(POLL_INTERVAL)
            continue

        prev = df.iloc[-3]
        last = df.iloc[-2]
        next_open = df.iloc[-1]["open"]

        prev_rsi = float(prev["rsi"])
        last_rsi = float(last["rsi"])
        ema_fast = float(last["ema_fast"])
        ema_slow = float(last["ema_slow"])

        print(f"[{now_str()}] 🔔 prev_rsi={prev_rsi:.2f} last_rsi={last_rsi:.2f} ema_f={ema_fast:.4f} ema_s={ema_slow:.4f}", flush=True)

        # wait after TP/manual until RSI exits zone
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            print(f"[{now_str()}] 🚫 Waiting: RSI still in zone ({RSI_LOW}-{RSI_HIGH})", flush=True)
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False
            print(f"[{now_str()}] ✅ RSI exited zone - can enter again", flush=True)

        # signal generation with EMA trend filter
        signal = None
        if prev_rsi < RSI_LOW and (RSI_LOW < last_rsi < RSI_HIGH) and ema_fast > ema_slow:
            signal = "BUY"
        elif prev_rsi > RSI_HIGH and (RSI_LOW < last_rsi < RSI_HIGH) and ema_fast < ema_slow:
            signal = "SELL"

        if signal:
            print(f"[{now_str()}] 🚀 Signal detected: {signal} - trying safe enter", flush=True)
            try:
                entry, tp, sl, sl_ok, sl_exceptions = place_entry_with_tp_sl(signal, float(next_open))
                # if SL placement failed across all methods
                if not sl_ok:
                    # DON'T force-close immediately. Retry placing SL a couple more times with delays
                    print(f"[{now_str()}] ⚠️ SL initial placement failed, will retry more carefully", flush=True)
                    # small retries
                    retry_ok = False
                    for attempt in range(3):
                        time.sleep(1 + attempt*0.5)
                        o, exs = None, None
                        try:
                            close_side = 'sell' if signal == 'BUY' else 'buy'
                            o, exs = place_sl_fallbacks(close_side, LOT_SIZE, sl)
                        except Exception as e:
                            log.warning("retry SL attempt error: %s", e)
                        if o:
                            retry_ok = True
                            break
                    if not retry_ok:
                        # ALL retries failed -> now as last resort we try to close via market to avoid unprotected position
                        print(f"[{now_str()}] ❗ All SL retries failed. As last resort: will MARKET-CLOSE to avoid unprotected position", flush=True)
                        try:
                            # cancel open TP if present, then close market opposite
                            cancel_all_open_orders()
                            close_side = 'sell' if signal == 'BUY' else 'buy'
                            # market close
                            exchange.create_order(SYMBOL, 'market', close_side, LOT_SIZE)
                            print(f"[{now_str()}] ⚠️ Forced market close executed after SL failed", flush=True)
                            append_csv({
                                "time": now_str(),
                                "side": signal,
                                "entry": entry,
                                "exit": None,
                                "pnl": None,
                                "note": "FORCED_MARKET_CLOSE_SL_FAIL"
                            })
                        except Exception as e:
                            log.critical("Forced market close also failed: %s", e)
                    else:
                        # retry succeeded: mark in_position and current_position
                        in_position = True
                        current_position = {"side": signal, "entry": entry, "time": now_str()}
                        print(f"[{now_str()}] ✅ Enter recorded (after retry) {signal} @ {entry:.4f} TP {tp:.4f} SL {sl:.4f}", flush=True)
                else:
                    # SL ok on first attempt
                    in_position = True
                    current_position = {"side": signal, "entry": entry, "time": now_str()}
                    print(f"[{now_str()}] ✅ ENTERED {signal} @ {entry:.4f} | TP {tp:.4f} SL {sl:.4f}", flush=True)
            except Exception as e:
                print(f"[{now_str()}] ❌ Entry routine failed: {e}", flush=True)
                log.error("Entry routine error: %s", e)

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ Bot stopping by user", flush=True)
        show_balance()
        break
    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(2)
