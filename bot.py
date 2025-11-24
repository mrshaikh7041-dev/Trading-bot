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

LOT_SIZE = 10           # qty send in market order
RSI_PERIOD = 14
RSI_LOW = 10              # reversal zone low
RSI_HIGH = 37             # reversal zone high

TP_POINTS = 0.032           # +8$ TP
SL_POINTS = 0.016           # -4$ SL
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
entry_in_progress = False        # 🆕 NEW: Entry lock to prevent double entry
last_position_check = None       # 🆕 NEW: Last position check time

# ================= HELPERS =================
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

def fetch_position_size() -> float:
    """
    MONITOR: exchange par actual position size kya hai.
    Positive = long, negative = short.
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
                    size = 0.0
            return float(size or 0.0)
    return 0.0

def can_enter_trade() -> bool:
    """🆕 NEW: Atomic check for trade entry conditions"""
    global in_position, entry_in_progress, cooldown_until_utc
    
    # Check if entry already in progress
    if entry_in_progress:
        log.warning("Entry already in progress, skipping...")
        return False
    
    # Check cooldown
    if cooldown_until_utc and datetime.now(timezone.utc) < cooldown_until_utc:
        return False
    
    # Check if already in position
    if in_position:
        return False
        
    # Final check with exchange
    ex_size = fetch_position_size()
    if abs(ex_size) > 1e-8:
        log.warning("Exchange shows open position (size=%.4f), skipping entry", ex_size)
        return False
        
    return True

def place_entry_with_tp_sl(side: str, approx_price: float):
    """
    1) Market entry
    2) Ussi filled qty se TP/SL reduceOnly order place

    Return: entry_price, qty, tp_price, sl_price
    """
    close_side = "sell" if side == "BUY" else "buy"

    # ---- market entry ----
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

    # ---- tp/sl prices ----
    if side == "BUY":
        tp_price = entry_price + TP_POINTS
        sl_price = entry_price - SL_POINTS
    else:
        tp_price = entry_price - TP_POINTS
        sl_price = entry_price + SL_POINTS

    # ---- TP (limit reduceOnly) ----
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

    # ---- SL (stop_market reduceOnly) ----
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

    log.info("Placed TP id=%s price=%.4f, SL id=%s price=%.4f",
             tp_order.get("id"), tp_price,
             sl_order.get("id"), sl_price)

    return entry_price, qty, tp_price, sl_price

def safe_enter_trade(signal: str, next_open_price: float, candle_time: int):
    """🆕 NEW: Safe entry with proper locking"""
    global in_position, current_position, entry_in_progress, last_entry_signal_candle
    
    entry_in_progress = True  # Lock entry
    try:
        # Final atomic check
        if not can_enter_trade():
            return
            
        # Monitor se final permission
        ex_size = fetch_position_size()
        if abs(ex_size) > 1e-8:
            log.warning("Final check: position exists (size=%.4f), skipping", ex_size)
            return

        print(
            f"[{now_str()}] 🚀 ENTERING {signal} | RSI Signal | "
            f"Price ~ {next_open_price:.4f}",
            flush=True
        )
        
        # Place entry with TP/SL
        entry_price, qty, tp_price, sl_price = place_entry_with_tp_sl(signal, next_open_price)

        # Update state after successful entry
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

    except Exception as e:
        log.error("Entry failed: %s", e)
        print(f"[{now_str()}] ❌ Entry failed: {e}", flush=True)
        
        # Check if position was partially opened
        size_after_fail = fetch_position_size()
        if abs(size_after_fail) > 1e-8:
            # Entry ho chuki hai, TP/SL fail hua
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
                f"[{now_str()}] ⚠️ TP/SL failed but entry EXISTS "
                f"(size={size_after_fail:.4f}). Monitor lock ON.",
                flush=True
            )
            log.warning(
                "TP/SL failed but entry exists (size=%.4f). Locked state.",
                size_after_fail
            )
    finally:
        entry_in_progress = False  # Release lock

def check_position_closed():
    """
    MONITOR:
      - position close detect kare
      - TP/SL approx identify kare
      - SL ho to cooldown lagaye
      - CSV + print logs
      - Bot ko inform kare: ab entry allowed / cooldown
    """
    global in_position, current_position, cooldown_until_utc, last_position_check

    if not in_position or current_position is None:
        return

    # 🆕 NEW: Rate limit position checks
    current_time = datetime.now(timezone.utc)
    if last_position_check and (current_time - last_position_check).total_seconds() < 5:
        return
        
    last_position_check = current_time

    size = fetch_position_size()
    if abs(size) > 1e-8:
        # still open
        return

    # yaha matlab position close ho chuki
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

    reason = "UNKNOWN"
    if tp_price is not None and sl_price is not None:
        # TP hit ya SL hit approx (jo price ke jyada close hai)
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

    print(f"[{now_str()}] EXIT {side} @ {exit_price:.4f} | PNL={pnl:.4f} | {reason}",
          flush=True)
    log.info("Exit %s @ %.4f | PNL=%.6f | %s",
             side, exit_price, pnl, reason)

    in_position = False
    current_position = None

# ================= MAIN LOOP =================
print(f"[{now_str()}] 🚀 RSI REVERSAL LIVE BOT STARTED | {SYMBOL} | FUTURES", flush=True)
log.info("Bot started for %s", SYMBOL)

while True:
    try:
        now_utc = datetime.now(timezone.utc)

        # ---------- MONITOR: EXCHANGE STATE = TRUTH ----------
        ex_size = fetch_position_size()

        # Agar exchange par position hai → bot ko in_position TRUE rakhna hi hai
        if abs(ex_size) > 1e-8 and not in_position:
            in_position = True
            log.warning("Monitor: found open position on exchange (size=%.4f). Entries locked.", ex_size)

        # Agar exchange par position nahi, par in_position True & current_position None
        # => cleanup
        if abs(ex_size) < 1e-8 and in_position and current_position is None:
            in_position = False

        # agar position open hai -> BOT KO SIRF MONITOR MODE PAR RAKHO
        if in_position:
            check_position_closed()
            time.sleep(POLL_INTERVAL)
            continue

        # ---------- COOLDOWN AFTER SL ----------
        if cooldown_until_utc and now_utc < cooldown_until_utc:
            # SL ke baad wait
            time.sleep(POLL_INTERVAL)
            continue

        # ---------- CANDLES + RSI (signal part) ----------
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD + 5)
        df = pd.DataFrame(ohlc, columns=["time", "open", "high", "low", "close", "volume"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)

        if len(df) < 4:
            time.sleep(POLL_INTERVAL)
            continue

        # index: -3 = prev closed, -2 = last closed, -1 = current forming
        prev_row = df.iloc[-3]
        last_closed = df.iloc[-2]
        curr_forming = df.iloc[-1]   # jiska open ~ next candle open hai

        prev_rsi = float(prev_row["rsi"])
        last_rsi = float(last_closed["rsi"])
        candle_time = int(last_closed["time"])
        next_open_price = float(curr_forming["open"])

        # ---------- ENTRY SIGNAL (RSI exit->reenter confirmation) ----------
        signal = None

        # BUY: pehle <10 tha, ab >10 & <37 (zone ke andar close)
        if prev_rsi < RSI_LOW and last_rsi > RSI_LOW and last_rsi < RSI_HIGH:
            signal = "BUY"

        # SELL: pehle >37 tha, ab <37 & >10 (zone ke andar close)
        elif prev_rsi > RSI_HIGH and last_rsi < RSI_HIGH and last_rsi > RSI_LOW:
            signal = "SELL"

        # Same candle par double signal se bachne ke liye
        if signal and last_entry_signal_candle == candle_time:
            signal = None

        # ---------- ENTRY EXECUTION ON NEXT OPEN (BOT PART) ----------
        if signal:
            # 🆕 NEW: Use safe entry function with locking
            safe_enter_trade(signal, next_open_price, candle_time)

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"[{now_str()}] ⛔ KeyboardInterrupt, exiting...", flush=True)
        log.info("Bot stopped by user")
        break

    except Exception as e:
        print(f"[{now_str()}] ⚠️ Loop error: {e}", flush=True)
        log.error("Loop error: %s", e)
        time.sleep(3)
