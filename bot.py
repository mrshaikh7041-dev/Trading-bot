#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# ================= USER CONFIG =================
SYMBOL = "XRP/USDT"
TIMEFRAME = "1m"

LOT_SIZE = 10             # qty send in market order
RSI_PERIOD = 14
RSI_LOW = 10              # reversal zone low
RSI_HIGH = 37             # reversal zone high

TP_POINTS = 0.032         # +TP in price
SL_POINTS = 0.016         # -SL in price
POLL_INTERVAL = 5         # seconds
COOLDOWN_MINUTES = 15     # only after SL

LOG_FILE = "/home/ubuntu/Trading-bot/bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# ================= LOGGING =================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ================= TIME HELPERS =================
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")

# ================= EXCHANGE SETUP =================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.options["adjustForTimeDifference"] = True
exchange.load_markets()

# BALANCE TRACKING VARIABLES
initial_balance = None
current_balance = None
total_profit = 0.0

try:
    exchange.set_leverage(75, SYMBOL)
    log.info("Leverage set to 75 for %s", SYMBOL)
except Exception as e:
    log.warning("set_leverage failed: %s", e)

# ================= STATE =================
in_position = False
current_position = None          # {side, entry, qty, tp_price, sl_price, time}
last_entry_signal_candle = None  # jis candle pe signal confirm hua
cooldown_until_utc = None        # SL ke baad next allowed entry time (UTC)
entry_in_progress = False        # Entry lock to prevent double entry
last_position_check = None       # Last position check time
position_check_cooldown = 5      # seconds between position checks
last_balance_check = None        # Last balance check time
balance_check_cooldown = 30      # seconds between balance checks

# ================= HELPERS =================
def fetch_balance() -> dict:
    try:
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']
        return {
            'free': float(usdt_balance['free']),
            'used': float(usdt_balance['used']),
            'total': float(usdt_balance['total'])
        }
    except Exception as e:
        log.error("Balance fetch error: %s", e)
        return None

def show_balance():
    global initial_balance, current_balance, total_profit
    
    balance = fetch_balance()
    if balance:
        current_balance = balance['total']
        if initial_balance is None:
            initial_balance = current_balance
            print(f"[{now_str()}] 💰 INITIAL BALANCE: ${initial_balance:.2f}", flush=True)
            log.info("Initial balance: $%.2f", initial_balance)
        
        total_profit = current_balance - initial_balance
        profit_percentage = (total_profit / initial_balance) * 100 if initial_balance > 0 else 0
        
        print(
            f"[{now_str()}] 💰 BALANCE: ${current_balance:.2f} | "
            f"PROFIT: ${total_profit:+.2f} ({profit_percentage:+.2f}%) | "
            f"USED: ${balance['used']:.2f} | FREE: ${balance['free']:.2f}",
            flush=True
        )
        return True
    return False

def append_csv(row: dict):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)

# 🔁 RSI EXACTLY LIKE BACKTEST
def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = -delta.where(delta < 0, 0).ewm(span=period, adjust=False).mean()
    rsi = 100 - (100 / (1 + (up / down)))
    return rsi

def fetch_position_size() -> float:
    try:
        positions = exchange.fetch_positions([SYMBOL])
        for p in positions:
            if p.get("symbol") == SYMBOL:
                size = p.get("contracts")
                if size is None:
                    try:
                        size = float(p.get("info", {}).get("positionAmt", 0))
                    except Exception:
                        size = 0.0
                return float(size or 0.0)
        return 0.0
    except Exception as e:
        log.error("fetch_positions error: %s", e)
        return 0.0

def can_enter_trade() -> bool:
    """Atomic check for trade entry conditions (no entry_in_progress check now)"""
    global in_position, cooldown_until_utc, current_position
    
    now_utc = datetime.now(timezone.utc)

    # Cooldown
    if cooldown_until_utc and now_utc < cooldown_until_utc:
        remaining = (cooldown_until_utc - now_utc).total_seconds() / 60
        print(
            f"[{now_str()}] 🧊 can_enter_trade: cooldown active, {remaining:.1f} min left",
            flush=True
        )
        log.info("can_enter_trade blocked: cooldown active (%.1f min left)", remaining)
        return False
    
    # Bot state
    if in_position:
        print(f"[{now_str()}] 🟡 can_enter_trade: in_position=True (bot state), skip entry", flush=True)
        log.warning("can_enter_trade blocked: in_position=True (bot state)")
        return False
        
    # Exchange reality
    ex_size = fetch_position_size()
    if abs(ex_size) > 1e-8:
        side = "BUY" if ex_size > 0 else "SELL"
        print(
            f"[{now_str()}] 🚨 can_enter_trade: exchange shows open position "
            f"size={ex_size:.6f} side={side}, bot will sync & skip entry",
            flush=True
        )
        log.warning("can_enter_trade blocked: exchange position exists size=%.6f", ex_size)
        
        in_position = True
        current_position = {
            "side": side,
            "entry": 0.0,
            "qty": abs(ex_size),
            "time": now_ist().isoformat(),
            "tp_price": None,
            "sl_price": None,
        }
        return False
        
    print(f"[{now_str()}] ✅ can_enter_trade: all clear, entry allowed", flush=True)
    return True

def place_entry_with_tp_sl(side: str, approx_price: float):
    close_side = "sell" if side == "BUY" else "buy"

    params = {"reduceOnly": False}
    order = exchange.create_order(
        SYMBOL, "market", side.lower(), LOT_SIZE, None, params
    )

    entry_price = order.get("average") or order.get("price") or approx_price
    entry_price = float(entry_price)

    qty = order.get("filled") or order.get("amount") or LOT_SIZE
    qty = float(qty)
    if qty <= 0:
        raise Exception(f"Filled qty 0, order={order}")

    if side == "BUY":
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    try:
        tp_order = exchange.create_order(
            SYMBOL,
            "limit",
            close_side,
            qty,
            tp_price,
            {
                "reduceOnly": True,
                "timeInForce": "GTC",
            },
        )
        log.info("Placed TP id=%s price=%.4f", tp_order.get("id"), tp_price)
    except Exception as e:
        log.error("TP order failed: %s", e)
        raise e

    try:
        sl_order = exchange.create_order(
            SYMBOL,
            "stop_market",
            close_side,
            qty,
            None,
            {
                "stopPrice": sl_price,
                "reduceOnly": True,
            },
        )
        log.info("Placed SL id=%s price=%.4f", sl_order.get("id"), sl_price)
    except Exception as e:
        log.error("SL order failed: %s", e)
        try:
            exchange.cancel_order(tp_order.get("id"), SYMBOL)
        except Exception:
            pass
        raise e

    return entry_price, qty, tp_price, sl_price

def safe_enter_trade(signal: str, next_open_price: float, candle_time: int):
    global in_position, current_position, entry_in_progress, last_entry_signal_candle
    
    print(f"[{now_str()}] 🔍 Checking entry conditions for {signal}...", flush=True)

    # PEHLE check, phir lock
    if not can_enter_trade():
        print(f"[{now_str()}] ❌ Entry conditions not met, skipping", flush=True)
        return False

    entry_in_progress = True
    try:
        print(f"[{now_str()}] 🚀 ENTERING {signal} | Price ~ {next_open_price:.4f}", flush=True)
        
        entry_price, qty, tp_price, sl_price = place_entry_with_tp_sl(signal, next_open_price)

        current_position = {
            "side": signal,
            "entry": entry_price,
            "qty": qty,
            "time": now_ist().isoformat(),
            "tp_price": tp_price,
            "sl_price": sl_price,
        }
        in_position = True
        last_entry_signal_candle = candle_time

        print(
            f"[{now_str()}] ✅ SUCCESS: ENTERED {signal} qty={qty:.4f} @ {entry_price:.4f} | "
            f"TP={tp_price:.4f} SL={sl_price:.4f}",
            flush=True
        )
        log.info(
            "Entered %s qty=%.4f @ %.4f | TP=%.4f SL=%.4f",
            signal, qty, entry_price, tp_price, sl_price
        )
        return True

    except Exception as e:
        log.error("Entry failed: %s", e)
        print(f"[{now_str()}] ❌ Entry failed: {e}", flush=True)
        
        time.sleep(1)
        size_after_fail = fetch_position_size()
        if abs(size_after_fail) > 1e-8:
            print(f"[{now_str()}] ⚠️ Partial entry detected (size={size_after_fail:.6f})", flush=True)
            in_position = True
            try:
                t = exchange.fetch_ticker(SYMBOL)
                entry_price_guess = float(t.get("last") or t.get("close"))
            except Exception:
                entry_price_guess = next_open_price

            current_position = {
                "side": signal,
                "entry": entry_price_guess,
                "qty": size_after_fail,
                "time": now_ist().isoformat(),
                "tp_price": None,
                "sl_price": None,
            }
            print(
                f"[{now_str()}] 🔒 Monitor locked - Manual intervention needed",
                flush=True
            )
            log.warning(
                "TP/SL failed but entry exists (size=%.6f). Manual close needed.",
                size_after_fail
            )
        return False
    finally:
        entry_in_progress = False

def sync_with_exchange():
    global in_position, current_position
    
    ex_size = fetch_position_size()
    
    if abs(ex_size) > 1e-8 and not in_position:
        print(f"[{now_str()}] 🔄 Syncing: Exchange has position (size={ex_size:.6f})", flush=True)
        in_position = True
        current_position = {
            "side": "BUY" if ex_size > 0 else "SELL",
            "entry": 0.0,
            "qty": abs(ex_size),
            "time": now_ist().isoformat(),
            "tp_price": None,
            "sl_price": None,
        }
        log.info("Synced with exchange: position exists (size=%.6f)", ex_size)
    
    elif abs(ex_size) < 1e-8 and in_position:
        print(f"[{now_str()}] 🔄 Syncing: No position on exchange", flush=True)
        in_position = False
        current_position = None
        log.info("Synced with exchange: no position")

def check_balance_periodically():
    global last_balance_check
    
    current_time = datetime.now(timezone.utc)
    if last_balance_check is None or (current_time - last_balance_check).total_seconds() >= balance_check_cooldown:
        if show_balance():
            last_balance_check = current_time

def check_position_closed():
    global in_position, current_position, cooldown_until_utc, last_position_check

    if not in_position or current_position is None:
        return

    current_time = datetime.now(timezone.utc)
    if last_position_check and (current_time - last_position_check).total_seconds() < position_check_cooldown:
        return
        
    last_position_check = current_time

    size = fetch_position_size()
    if abs(size) > 1e-8:
        return

    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        exit_price = float(ticker.get("last") or ticker.get("close"))
    except Exception:
        exit_price = current_position["entry"]

    entry = current_position["entry"]
    side = current_position["side"]
    qty = current_position["qty"]
    tp_price = current_position.get("tp_price")
    sl_price = current_position.get("sl_price")

    pnl = (exit_price - entry) * qty if side == "BUY" else (entry - exit_price) * qty

    reason = "MANUAL"
    if tp_price is not None and sl_price is not None:
        dist_tp = abs(exit_price - tp_price)
        dist_sl = abs(exit_price - sl_price)
        if dist_sl < dist_tp:
            reason = "SL"
        else:
            reason = "TP"

    if reason == "SL":
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        log.info("SL hit, cooldown until %s UTC", cooldown_until_utc.isoformat())

    row = {
        "time": current_position["time"],
        "side": side,
        "qty": qty,
        "entry": entry,
        "exit": exit_price,
        "pnl": round(pnl, 6),
        "reason": reason,
    }
    append_csv(row)

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f} | {reason}",
          flush=True)
    log.info("Exit %s @ %.4f | PNL=%.6f | %s", side, exit_price, pnl, reason)

    in_position = False
    current_position = None

# ================= MAIN LOOP =================
print(f"[{now_str()}] 🚀 RSI REVERSAL BOT STARTING...", flush=True)
print(f"[{now_str()}] ⚙️  Config: RSI({RSI_LOW}-{RSI_HIGH}) | TP: {TP_POINTS} | SL: {SL_POINTS}", flush=True)
log.info("Bot started for %s", SYMBOL)

print(f"[{now_str()}] 🔄 Connecting to exchange...", flush=True)
if show_balance():
    print(f"[{now_str()}] ✅ Connected successfully!", flush=True)
else:
    print(f"[{now_str()}] ❌ Failed to fetch initial balance", flush=True)

print(f"[{now_str()}] 🔄 Initial sync with exchange...", flush=True)
sync_with_exchange()

while True:
    try:
        now_utc = datetime.now(timezone.utc)

        check_balance_periodically()
        sync_with_exchange()

        if in_position:
            check_position_closed()
            time.sleep(POLL_INTERVAL)
            continue

        if cooldown_until_utc and now_utc < cooldown_until_utc:
            time.sleep(POLL_INTERVAL)
            continue

        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        if len(df) < 4:
            time.sleep(POLL_INTERVAL)
            continue

        # prev = i-1, closed = i, next entry candle = i+1
        prev_row = df.iloc[-3]       # i-1
        last_closed = df.iloc[-2]    # i
        curr_forming = df.iloc[-1]   # i+1 (entry candle)

        prev_rsi = float(prev_row["rsi"])
        last_rsi = float(last_closed["rsi"])
        candle_time = int(last_closed["time"])
        next_open_price = float(curr_forming["open"])

        print(
            f"[{now_str()}] 🕯 RSI prev={prev_rsi:.2f} last={last_rsi:.2f} | "
            f"zone=({RSI_LOW}-{RSI_HIGH})",
            flush=True
        )

        # -------- BACKTEST STRATEGY (COPIED) --------
        signal = None

        # BUY: previous RSI below low, current closed RSI enters (low, high)
        if prev_rsi < RSI_LOW and (RSI_LOW < last_rsi < RSI_HIGH):
            signal = "BUY"
            print(f"[{now_str()}] 📈 BUY signal (cross up into zone)", flush=True)

        # SELL: previous RSI above high, current closed RSI enters (low, high)
        elif prev_rsi > RSI_HIGH and (RSI_LOW < last_rsi < RSI_HIGH):
            signal = "SELL"
            print(f"[{now_str()}] 📉 SELL signal (cross down into zone)", flush=True)

        # same-candle double entry avoid
        if signal and last_entry_signal_candle == candle_time:
            print(f"[{now_str()}] 🔁 Same candle repeat signal ignored", flush=True)
            signal = None
        # --------------------------------------------

        if signal:
            ok = safe_enter_trade(signal, next_open_price, candle_time)
            if not ok:
                print(f"[{now_str()}] ⚠️ safe_enter_trade returned False", flush=True)

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ Bot stopping...", flush=True)
        show_balance()
        print(f"[{now_str()}] 👋 Bot stopped by user", flush=True)
        log.info("Bot stopped by user")
        break

    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(3)
