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

LOT_SIZE = 10              # qty per trade
RSI_PERIOD = 14
RSI_LOW = 10               # reversal zone low
RSI_HIGH = 37              # reversal zone high

TP_POINTS = 0.032          # TP distance in price
SL_POINTS = 0.016          # SL distance in price
POLL_INTERVAL = 5          # seconds
COOLDOWN_MINUTES = 15      # after SL

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

FUT_SYMBOL = SYMBOL.replace("/", "")  # e.g. XRP/USDT -> XRPUSDT

try:
    exchange.set_leverage(75, SYMBOL)
    log.info("Leverage set to 75 for %s", SYMBOL)
except Exception as e:
    log.warning("set_leverage failed: %s", e)

# ================= STATE =================
in_position = False
current_position = None          # {side, entry, qty, tp_price, sl_price, time}
last_entry_signal_candle = None
cooldown_until_utc = None
entry_in_progress = False
last_position_check = None
position_check_cooldown = 5
last_balance_check = None
balance_check_cooldown = 30
wait_for_zone_exit = False       # TP ke baad RSI zone se bahar aane ka wait

# BALANCE TRACKING
initial_balance = None
current_balance = None
total_profit = 0.0

# ================= HELPERS =================
def fetch_balance_usdt():
    """USDT futures wallet balance"""
    try:
        balance = exchange.fetch_balance()
        usdt = balance['USDT']
        return {
            "free": float(usdt['free']),
            "used": float(usdt['used']),
            "total": float(usdt['total'])
        }
    except Exception as e:
        log.error("Balance fetch error: %s", e)
        return None

def show_balance():
    """Detailed balance print (style B)"""
    global initial_balance, current_balance, total_profit
    
    b = fetch_balance_usdt()
    if not b:
        return False

    current_balance = b['total']
    if initial_balance is None:
        initial_balance = current_balance
        print(f"[{now_str()}] 💰 INITIAL BALANCE: ${initial_balance:.2f}", flush=True)
        log.info("Initial balance: $%.2f", initial_balance)

    total_profit = current_balance - initial_balance
    pct = (total_profit / initial_balance) * 100 if initial_balance > 0 else 0.0

    print(
        f"[{now_str()}] 💰 BALANCE: ${current_balance:.2f} | "
        f"PROFIT: ${total_profit:+.2f} ({pct:+.2f}%) | "
        f"USED: ${b['used']:.2f} | FREE: ${b['free']:.2f}",
        flush=True
    )
    return True

def append_csv(row: dict):
    exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)

def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI exactly like backtest (Wilder EMA style)"""
    delta = series.diff()
    up = delta.where(delta > 0, 0).ewm(span=period, adjust=False).mean()
    down = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100 / (1 + (up / down)))

def fetch_position_size() -> float:
    """
    Binance USDT-M futures actual position size (contracts).
    Positive = long, negative = short.
    """
    try:
        balance = exchange.fetch_balance()
        positions = balance.get("info", {}).get("positions", [])
        for p in positions:
            if p.get("symbol") == FUT_SYMBOL:
                amt = p.get("positionAmt", "0")
                size = float(amt)
                if abs(size) > 1e-8:
                    print(f"[{now_str()}] 📡 Exchange position {FUT_SYMBOL} size={size:.4f}", flush=True)
                return size
        return 0.0
    except Exception as e:
        log.error("fetch_position_size error: %s", e)
        print(f"[{now_str()}] ⚠️ fetch_position_size error: {e}", flush=True)
        return 0.0

def can_enter_trade() -> bool:
    """Trade entry conditions (cooldown + no open position)"""
    global in_position, cooldown_until_utc, current_position
    
    now_utc = datetime.now(timezone.utc)

    # Cooldown after SL
    if cooldown_until_utc and now_utc < cooldown_until_utc:
        remaining = (cooldown_until_utc - now_utc).total_seconds() / 60
        print(f"[{now_str()}] 🧊 Cooldown active: {remaining:.1f} min left", flush=True)
        return False
    
    # Bot state
    if in_position:
        print(f"[{now_str()}] 🟡 Bot thinks in_position=True, skip entry", flush=True)
        return False
        
    # Exchange reality
    ex_size = fetch_position_size()
    if abs(ex_size) > 1e-8:
        side = "BUY" if ex_size > 0 else "SELL"
        print(
            f"[{now_str()}] 🚨 Exchange shows open position size={ex_size:.4f} side={side}, syncing...",
            flush=True
        )
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
        
    print(f"[{now_str()}] ✅ can_enter_trade: all clear", flush=True)
    return True

def place_entry_with_tp_sl(side: str, approx_price: float):
    close_side = "sell" if side == "BUY" else "buy"

    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    entry_price = float(order.get("average") or order.get("price") or approx_price)
    qty = float(order.get("filled") or order.get("amount") or LOT_SIZE)

    if qty <= 0:
        raise Exception(f"Filled qty 0, order={order}")

    if side == "BUY":
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    # TP
    tp_order = exchange.create_order(
        SYMBOL, "limit", close_side, qty, tp_price,
        {"reduceOnly": True, "timeInForce": "GTC"}
    )
    log.info("Placed TP id=%s price=%.4f", tp_order.get("id"), tp_price)

    # SL
    sl_order = exchange.create_order(
        SYMBOL, "stop_market", close_side, qty, None,
        {"stopPrice": sl_price, "reduceOnly": True}
    )
    log.info("Placed SL id=%s price=%.4f", sl_order.get("id"), sl_price)

    return entry_price, qty, tp_price, sl_price

def safe_enter_trade(signal: str, next_open_price: float, candle_time: int):
    global in_position, current_position, entry_in_progress, last_entry_signal_candle
    
    print(f"[{now_str()}] 🔍 Checking entry conditions for {signal}...", flush=True)

    # Pehle check, phir lock
    if not can_enter_trade():
        print(f"[{now_str()}] ❌ Entry conditions not met, skipping", flush=True)
        return False

    entry_in_progress = True
    try:
        print(f"[{now_str()}] 🚀 ENTERING {signal} | approx price {next_open_price:.4f}", flush=True)
        
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
            f"[{now_str()}] ✅ ENTERED {signal} qty={qty:.4f} @ {entry_price:.4f} | "
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
        return False
    finally:
        entry_in_progress = False

def sync_with_exchange():
    """Just to keep internal state aligned (optional safety)"""
    global in_position, current_position
    
    ex_size = fetch_position_size()
    if abs(ex_size) > 1e-8 and not in_position:
        print(f"[{now_str()}] 🔄 Sync: exchange has position size={ex_size:.4f}", flush=True)
        in_position = True
        current_position = {
            "side": "BUY" if ex_size > 0 else "SELL",
            "entry": 0.0,
            "qty": abs(ex_size),
            "time": now_ist().isoformat(),
            "tp_price": None,
            "sl_price": None,
        }
    elif abs(ex_size) < 1e-8 and in_position:
        print(f"[{now_str()}] 🔄 Sync: no position on exchange", flush=True)
        in_position = False
        current_position = None

def check_balance_periodically():
    global last_balance_check
    now = datetime.now(timezone.utc)
    if last_balance_check is None or (now - last_balance_check).total_seconds() >= balance_check_cooldown:
        if show_balance():
            last_balance_check = now

def check_position_closed():
    """Detect position close and log PnL, apply cooldown/zone-wait, cancel TP/SL"""
    global in_position, current_position, cooldown_until_utc, last_position_check, wait_for_zone_exit

    if not in_position or current_position is None:
        return

    now = datetime.now(timezone.utc)
    if last_position_check and (now - last_position_check).total_seconds() < position_check_cooldown:
        return
    last_position_check = now

    size = fetch_position_size()
    if abs(size) > 1e-8:
        return  # still open

    # Position is closed
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
        reason = "SL" if dist_sl < dist_tp else "TP"

    # SL -> time based cooldown, TP/MANUAL -> wait_for_zone_exit
    if reason == "SL":
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        print(f"[{now_str()}] 🧊 SL hit, cooldown for {COOLDOWN_MINUTES} min", flush=True)
    else:
        wait_for_zone_exit = True
        print(f"[{now_str()}] 🧺 Trade closed ({reason}), waiting RSI to exit zone", flush=True)

    # Cancel leftover TP/SL orders (safety)
    try:
        open_orders = exchange.fetch_open_orders(SYMBOL)
        for o in open_orders:
            try:
                exchange.cancel_order(o["id"], SYMBOL)
                print(f"[{now_str()}] ❌ Cancelled leftover order {o['id']}", flush=True)
            except Exception as e:
                print(f"[{now_str()}] ⚠️ Cancel order failed: {e}", flush=True)
    except Exception as e:
        log.error("fetch_open_orders error: %s", e)

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

    print(f"[{now_str()}] 📊 EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f} | {reason}", flush=True)
    log.info("Exit %s @ %.4f | PNL=%.6f | %s", side, exit_price, pnl, reason)

    in_position = False
    current_position = None

# ================= MAIN LOOP =================
print(f"[{now_str()}] 🚀 RSI REVERSAL BOT STARTING...", flush=True)
print(f"[{now_str()}] ⚙️ Config: RSI({RSI_LOW}-{RSI_HIGH}) | TP={TP_POINTS} SL={SL_POINTS}", flush=True)

print(f"[{now_str()}] 🔄 Connecting & fetching initial balance...", flush=True)
show_balance()
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

        # ----- Fetch candles -----
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        if len(df) < 4:
            time.sleep(POLL_INTERVAL)
            continue

        # prev = i-1, closed = i, entry candle = i+1 open
        prev_row = df.iloc[-3]
        last_closed = df.iloc[-2]
        curr_forming = df.iloc[-1]

        prev_rsi = float(prev_row["rsi"])
        last_rsi = float(last_closed["rsi"])
        candle_time = int(last_closed["time"])
        next_open_price = float(curr_forming["open"])

        print(
            f"[{now_str()}] 🕯 RSI prev={prev_rsi:.2f} last={last_rsi:.2f} | "
            f"zone=({RSI_LOW}-{RSI_HIGH})",
            flush=True
        )

        # TP ke baad: jab tak RSI zone (LOW,HIGH) me hai, entry block
        global wait_for_zone_exit
        if wait_for_zone_exit and (RSI_LOW < last_rsi < RSI_HIGH):
            print(f"[{now_str()}] 🚫 Inside zone after last TP/MANUAL - waiting exit", flush=True)
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_rsi <= RSI_LOW or last_rsi >= RSI_HIGH):
            wait_for_zone_exit = False
            print(f"[{now_str()}] ✅ RSI exited zone - new entries allowed", flush=True)

        # -------- BACKTEST STRATEGY --------
        signal = None
        if prev_rsi < RSI_LOW and (RSI_LOW < last_rsi < RSI_HIGH):
            signal = "BUY"
            print(f"[{now_str()}] 📈 BUY signal (cross up into zone)", flush=True)
        elif prev_rsi > RSI_HIGH and (RSI_LOW < last_rsi < RSI_HIGH):
            signal = "SELL"
            print(f"[{now_str()}] 📉 SELL signal (cross down into zone)", flush=True)

        if signal and last_entry_signal_candle == candle_time:
            print(f"[{now_str()}] 🔁 Same candle repeat signal ignored", flush=True)
            signal = None
        # -----------------------------------

        if signal:
            ok = safe_enter_trade(signal, next_open_price, candle_time)
            if not ok:
                print(f"[{now_str()}] ⚠️ safe_enter_trade returned False", flush=True)

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ Bot stopping...", flush=True)
        show_balance()
        log.info("Bot stopped by user")
        break
    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(3)
