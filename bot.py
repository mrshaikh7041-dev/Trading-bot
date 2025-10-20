# live_binance_futures_bot_clean.py
import ccxt
import pandas as pd
import time
import threading
import traceback
import logging
from datetime import datetime, timedelta

# ================= CONFIG =================
API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"    # 🔒 Hardcoded as requested
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

SYMBOL = "BNBUSDT"               # Binance Futures symbol (no slash)
TIMEFRAME = "1m"
EMA_SET = [10, 20, 50, 100]

LOT_SIZE = 0.01                  # BNB quantity
TP_POINTS = 6.0                  # absolute points
SL_POINTS = 3.0
LEVERAGE = 75
COOLDOWN_MINUTES = 30
POLL_INTERVAL = 5                # seconds between checks

# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)

exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
    'timeout': 30000,
})

def safe_sleep(sec):
    try:
        time.sleep(sec)
    except KeyboardInterrupt:
        raise

def now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

# ================= STRATEGY =================
def fetch_latest_data(symbol, timeframe, limit=200):
    """Fetch recent candles"""
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=["time", "open", "high", "low", "close", "volume"])
        df['time'] = pd.to_datetime(df['time'], unit='ms') + timedelta(hours=5, minutes=30)
        return df
    except Exception as e:
        logging.warning(f"Data fetch error: {e}")
        return pd.DataFrame()

def apply_ema_strategy(df, ema_set):
    """Same logic as backtest, simplified for live"""
    df = df.copy()
    for span in ema_set:
        df[f"ema{span}"] = df["close"].ewm(span=span, adjust=False).mean()
    signal = None
    if len(df) < max(ema_set) + 2:
        return None
    c, h, l = df.iloc[-1][["close", "high", "low"]]
    emas = [df.iloc[-1][f"ema{e}"] for e in ema_set]
    if all(c > e for e in emas):
        signal = "BUY"
    elif all(c < e for e in emas):
        signal = "SELL"
    elif l <= emas[len(emas)//2] <= h:
        if c > emas[len(emas)//2]:
            signal = "BUY"
        elif c < emas[len(emas)//2]:
            signal = "SELL"
    return signal

# ================= MONITOR =================
class PositionMonitor:
    def __init__(self):
        self.in_position = False
        self.side = None
        self.entry_price = None
        self.cooldown_until = None

    def set_position(self, side, entry_price):
        self.in_position = True
        self.side = side
        self.entry_price = entry_price
        logging.info(f"✅ Position OPENED | {side} @ {entry_price}")

    def clear_position(self, reason="Closed"):
        logging.info(f"❌ Position CLOSED ({reason})")
        self.in_position = False
        self.side = None
        self.entry_price = None
        self.cooldown_until = now() + timedelta(minutes=COOLDOWN_MINUTES)
        logging.info(f"🕒 Cooldown active until {self.cooldown_until}")

    def can_trade(self):
        if self.cooldown_until and now() < self.cooldown_until:
            return False
        return not self.in_position

monitor = PositionMonitor()

# ================= BINANCE HELPERS =================
def set_leverage(symbol, leverage):
    try:
        exchange.set_leverage(leverage, symbol)
        logging.info(f"Leverage set to {leverage}x for {symbol}")
    except Exception as e:
        logging.warning(f"Leverage set error: {e}")

def place_market_entry(symbol, side, qty):
    return exchange.create_order(symbol, 'MARKET', side, qty)

def place_reduce_only_orders(symbol, side, qty, entry_price):
    """Set TP/SL reduce-only"""
    tp = entry_price + TP_POINTS if side == "BUY" else entry_price - TP_POINTS
    sl = entry_price - SL_POINTS if side == "BUY" else entry_price + SL_POINTS

    params_tp = {'stopPrice': tp, 'reduceOnly': True}
    params_sl = {'stopPrice': sl, 'reduceOnly': True}
    tp_side = "SELL" if side == "BUY" else "BUY"
    sl_side = "SELL" if side == "BUY" else "BUY"

    try:
        exchange.create_order(symbol, 'TAKE_PROFIT_MARKET', tp_side, qty, None, params_tp)
        exchange.create_order(symbol, 'STOP_MARKET', sl_side, qty, None, params_sl)
        logging.info(f"🎯 TP: {tp} | 🛑 SL: {sl}")
    except Exception as e:
        logging.error(f"TP/SL placement error: {e}")

# ================= MAIN LOOP =================
def run_bot():
    set_leverage(SYMBOL, LEVERAGE)
    logging.info("🚀 Live bot started. Waiting for signals...")

    while True:
        try:
            if not monitor.can_trade():
                logging.info("⏸ In cooldown or open position. Waiting...")
                safe_sleep(POLL_INTERVAL)
                continue

            df = fetch_latest_data(SYMBOL, TIMEFRAME)
            if df.empty:
                safe_sleep(POLL_INTERVAL)
                continue

            signal = apply_ema_strategy(df, EMA_SET)
            if not signal:
                safe_sleep(POLL_INTERVAL)
                continue

            ticker = exchange.fetch_ticker(SYMBOL)
            price = float(ticker['last'])
            logging.info(f"📊 Signal: {signal} | Price: {price}")

            # Place market entry
            order = place_market_entry(SYMBOL, signal, LOT_SIZE)
            entry_price = price
            monitor.set_position(signal, entry_price)

            # Place TP/SL reduceOnly
            place_reduce_only_orders(SYMBOL, signal, LOT_SIZE, entry_price)

            # Monitor position until closed
            while monitor.in_position:
                try:
                    pos = exchange.fetch_positions([SYMBOL])
                    found = next((p for p in pos if p['symbol'] == SYMBOL and abs(float(p['contracts'])) > 0), None)
                    if not found:
                        monitor.clear_position("TP/SL hit")
                        break
                except Exception as e:
                    logging.warning(f"Position check error: {e}")
                safe_sleep(10)

        except KeyboardInterrupt:
            logging.info("🛑 Bot stopped manually.")
            break
        except Exception as e:
            logging.error(f"Main loop error: {e}\n{traceback.format_exc()}")
            safe_sleep(5)

if __name__ == "__main__":
    run_bot()
