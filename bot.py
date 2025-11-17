#!/usr/bin/env python3
import ccxt
import pandas as pd
import time
import csv
import os
import logging
from datetime import datetime, timezone, timedelta

# =================== CONFIG ===================
SYMBOL = "BNB/USDT"
TIMEFRAME = "1m"
LOT_SIZE = 0.01   # Aap jo bhi set karoge same order me jayega
RSI_PERIOD = 14
RSI_LOW = 20
RSI_HIGH = 60
TP_POINTS = 6.0
SL_POINTS = 3.0
POLL_INTERVAL = 2  # seconds

LOG_FILE = "bot.log"
CSV_FILE = f"{SYMBOL.replace('/', '-')}_trades.csv"

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

# =================== LOGGING ===================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# =================== TIME HELPERS ===================
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

# =================== EXCHANGE SETUP ===================
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "future"},
})

exchange.load_markets()

# =================== STATE ===================
in_position = False
current_position = None
last_signal_time = None


# =================== UTILITIES ===================
def append_csv(data):
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=data.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(data)


def rsi_wilder(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def place_market_entry(side, entry_price):
    params = {"reduceOnly": False}
    order = exchange.create_order(
        SYMBOL, "market", side.lower(), LOT_SIZE, None, params
    )
    price = order.get("average") or entry_price
    return float(price)


def place_tp_sl(dir_side, entry):
    close_side = "sell" if dir_side == "BUY" else "buy"
    tp_price = entry + TP_POINTS if dir_side == "BUY" else entry - TP_POINTS
    sl_price = entry - SL_POINTS if dir_side == "BUY" else entry + SL_POINTS

    tp_order = exchange.create_order(
        SYMBOL, "limit", close_side, LOT_SIZE, tp_price,
        {"reduceOnly": True}
    )
    sl_order = exchange.create_order(
        SYMBOL, "STOP_MARKET", close_side, LOT_SIZE, None,
        {"stopPrice": sl_price, "reduceOnly": True}
    )

    return tp_order["id"], sl_order["id"]


def check_position_closed():
    global in_position, current_position

    pos = exchange.fapiPrivate_get_positionrisk()
    for p in pos:
        if p["symbol"] == SYMBOL.replace("/", ""):
            amt = float(p["positionAmt"])
            if abs(amt) < 1e-8:
                # Position closed
                exit_price = float(exchange.fetch_ticker(SYMBOL)["last"])
                entry = current_position["entry"]
                side = current_position["side"]
                pnl = (exit_price - entry) * LOT_SIZE if side == "BUY" else (entry - exit_price) * LOT_SIZE

                append_csv({
                    "time": current_position["time"],
                    "entry": entry,
                    "exit": exit_price,
                    "side": side,
                    "pnl": round(pnl, 6),
                    "balance": None
                })
                print(f"📌 EXIT: {side} @ {exit_price:.2f} | PNL: {pnl:.4f}")
                logging.info(f"Position closed | PNL: {pnl}")

                in_position = False
                current_position = None


# =================== MAIN LOOP ===================
print("🚀 RSI LIVE BOT STARTED | MARKET ONLY\n")
logging.info("Bot Started")

while True:
    try:
        ohlc = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
        df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","vol"])
        df["rsi"] = rsi_wilder(df["close"], RSI_PERIOD)
        rsi = df["rsi"].iloc[-2]  # Only closed candle

        if not in_position:
            if rsi < RSI_LOW:
                signal = "BUY"
            elif rsi > RSI_HIGH:
                signal = "SELL"
            else:
                signal = None

            if signal:
                price = float(df["close"].iloc[-2])
                print(f"📍 SIGNAL: {signal} @ {price:.2f} | RSI: {rsi:.2f}")
                logging.info(f"Signal {signal}")

                entry = place_market_entry(signal, price)
                tp_id, sl_id = place_tp_sl(signal, entry)

                current_position = {
                    "side": signal,
                    "entry": entry,
                    "tp_id": tp_id,
                    "sl_id": sl_id,
                    "time": now_ist().isoformat()
                }
                in_position = True

        else:
            check_position_closed()

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("🛑 BOT STOPPED BY USER")
        break

    except Exception as e:
        print(f"⚠️ ERROR: {e}")
        logging.error(f"Loop error: {e}")
        time.sleep(3)
