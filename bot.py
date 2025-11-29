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
def now_ist(): return datetime.now(timezone.utc).astimezone(IST)
def now_str(): return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")

# ========== EXCHANGE ==========
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})
exchange.load_markets()
FUT_SYMBOL = SYMBOL.replace("/", "")

try: exchange.set_leverage(75, SYMBOL)
except: pass

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
def fetch_position_size():
    try:
        bal = exchange.fetch_balance()
        for p in bal["info"]["positions"]:
            if p["symbol"] == FUT_SYMBOL:
                return float(p["positionAmt"])
    except: pass
    return 0.0


def fetch_balance():
    try:
        bal = exchange.fetch_balance()
        usdt = bal["USDT"]
        return float(usdt["free"]) + float(usdt["used"])
    except:
        return None


def show_balance():
    global initial_balance, current_balance, total_profit

    bal = fetch_balance()
    if bal is None: return

    current_balance = bal
    if initial_balance is None:
        initial_balance = current_balance

    total_profit = current_balance - initial_balance
    pct = (total_profit/initial_balance)*100 if initial_balance > 0 else 0

    print(f"[{now_str()}] 💰 BALANCE: ${current_balance:.2f} | PNL: ${total_profit:+.2f} ({pct:+.2f}%)",
          flush=True)


def append_csv(row: dict):
    exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)


def rsi_wilder(s: pd.Series, period=14):
    d = s.diff()
    up = d.where(d > 0, 0).ewm(span=period, adjust=False).mean()
    dn = (-d.where(d < 0, 0)).ewm(span=period, adjust=False).mean()
    return 100 - (100/(1 + (up/dn)))


# ========== TRADING ==========
def can_enter():
    global in_position

    if cooldown_until_utc and datetime.now(timezone.utc) < cooldown_until_utc:
        return False

    size = fetch_position_size()
    if abs(size) > 0:
        in_position = True
        return False

    return not in_position


def place_entry(side, px):
    close_side = "sell" if side == "BUY" else "buy"
    order = exchange.create_order(SYMBOL, "market", side.lower(), LOT_SIZE)
    entry = float(order["average"])

    tp = entry + TP_POINTS if side=="BUY" else entry - TP_POINTS
    sl = entry - SL_POINTS if side=="BUY" else entry + SL_POINTS

    exchange.create_order(SYMBOL, "limit", close_side, LOT_SIZE, tp, {"reduceOnly": True})
    exchange.create_order(SYMBOL, "stop_market", close_side, LOT_SIZE, None,
                          {"stopPrice": sl, "reduceOnly": True})

    return entry, tp, sl


def close_position(reason):
    global in_position, current_position, cooldown_until_utc, wait_for_zone_exit

    price = float(exchange.fetch_ticker(SYMBOL)["last"])
    entry = current_position["entry"]
    side = current_position["side"]

    pnl = (price-entry)*LOT_SIZE if side=="BUY" else (entry-price)*LOT_SIZE

    append_csv({
        "time": current_position["time"],
        "side": side,
        "entry": entry,
        "exit": price,
        "pnl": round(pnl,4),
        "reason": reason
    })

    for o in exchange.fetch_open_orders(SYMBOL):
        try: exchange.cancel_order(o["id"], SYMBOL)
        except: pass

    if reason == "SL":
        cooldown_until_utc = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
    else:
        wait_for_zone_exit = True

    in_position = False
    current_position = None


# ========== MAIN LOOP ==========
print(f"[{now_str()}] 🚀 Bot started RSI({RSI_LOW}-{RSI_HIGH})")
show_balance()

while True:
    try:
        # Balance update
        if not last_balance_check or (time.time()-last_balance_check > balance_check_interval):
            show_balance()
            last_balance_check = time.time()

        # If in trade → just monitor for close
        if in_position:
            if abs(fetch_position_size()) == 0:
                close_position("AUTO")
            time.sleep(POLL_INTERVAL)
            continue

        candles = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=RSI_PERIOD+5)
        df = pd.DataFrame(candles, columns=["t","o","h","l","c","v"])
        df["rsi"] = rsi_wilder(df["c"], RSI_PERIOD)

        prev = df.iloc[-3]; last = df.iloc[-2]
        next_open = df.iloc[-1]["o"]
        prev_r = float(prev["rsi"]); last_r = float(last["rsi"])

        global wait_for_zone_exit
        if wait_for_zone_exit and (RSI_LOW < last_r < RSI_HIGH):
            time.sleep(POLL_INTERVAL)
            continue
        if wait_for_zone_exit and (last_r <= RSI_LOW or last_r >= RSI_HIGH):
            wait_for_zone_exit = False

        signal = None
        if prev_r < RSI_LOW and RSI_LOW < last_r < RSI_HIGH:
            signal = "BUY"
        elif prev_r > RSI_HIGH and RSI_LOW < last_r < RSI_HIGH:
            signal = "SELL"

        if signal and can_enter():
            entry, tp, sl = place_entry(signal, next_open)
            in_position = True
            current_position = {"side":signal,"entry":entry,"time":now_ist().isoformat()}
            print(f"[{now_str()}] 🚀 ENTER {signal} @ {entry:.4f} | TP {tp:.4f} SL {sl:.4f}", flush=True)
            log.info(f"Entered {signal}")

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("Bot stopped.")
        break
    except Exception as e:
        print(f"⚠️ Error: {e}")
        time.sleep(3)
