import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import traceback
import os
import csv
import logging
import sys
import numpy as np

# =================== CONFIG ===================
SYMBOL = 'BNB/USDT'
TIMEFRAME = '1m'
LOT_SIZE = 0.10
TP_POINTS = 6.0
SL_POINTS = 3.0
LEVERAGE = 75

# Hybrid toggle system
LIVE_MODE = 'off'          # 'on' = live trading, 'off' = simulation
SIMULATION_MODE = 'on'     # 'on' = paper trading, 'off' = real trading
PAPER_BALANCE = 5.0        # starting balance for simulation

COOLDOWN_MINUTES = 30
POLL_INTERVAL_SECONDS = 5
INTRABAR_STEPS = 30        # intrabar mini-simulation count
CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'
FEE_RATE = 0.0006          # optional fee per side (0.06%)

# =================== LOGGING ===================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# =================== EXCHANGE ===================
exchange = ccxt.binance({'enableRateLimit': True})

# =================== STATE ===================
balance = PAPER_BALANCE if SIMULATION_MODE == 'on' else 0.0
in_position = False
cooldown_until = None
position = None
last_processed_candle_time = None

KOLKATA = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)

def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== UTILS ===================
def fetch_latest_candles(symbol, timeframe, limit=200):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not bars or len(bars) < 10:
            return None
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
        return df
    except Exception as e:
        msg = f'Fetch candles failed: {e}'
        print(f'[ERROR] {msg}', flush=True)
        log.error(msg)
        return None

def compute_emas(df):
    df = df.copy()
    df['ema10'] = df['close'].ewm(span=10, adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema100'] = df['close'].ewm(span=100, adjust=False).mean()
    return df

def check_signal(candle):
    try:
        c = float(candle['close'])
        l = float(candle['low'])
        h = float(candle['high'])
        ema10 = float(candle['ema10'])
        ema20 = float(candle['ema20'])
        ema50 = float(candle['ema50'])
        ema100 = float(candle['ema100'])
    except Exception:
        return None

    # Trend filter
    if c >= ema10 and c >= ema20 and c >= ema50 and c > ema100:
        return 'BUY'
    if c <= ema10 and c <= ema20 and c <= ema50 and c < ema100:
        return 'SELL'

    # EMA15 intrabar touch
    if l <= ema50 <= h and c > ema10:
        return 'BUY'
    if l <= ema50 <= h and c < ema10:
        return 'SELL'

    return None

def append_trade_csv(record):
    header = ['time', 'dir', 'entry', 'exit', 'outcome', 'pnl', 'balance']
    file_exists = os.path.isfile(CSV_FN)
    with open(CSV_FN, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)

def fetch_balance():
    if SIMULATION_MODE == 'on':
        return balance
    elif LIVE_MODE == 'on':
        b = exchange.fetch_balance()
        return float(b['total'].get('USDT', 0.0))
    else:
        return balance

# =================== STARTUP MESSAGE ===================
STARTUP_MSG = f"Starting EMA Bot ({SYMBOL}) | LIVE: {LIVE_MODE} | SIMULATION: {SIMULATION_MODE} | Starting..."
print(f"[{now_str()}] {STARTUP_MSG}", flush=True)
log.info(STARTUP_MSG)

# =================== MAIN LOOP ===================
while True:
    try:
        df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
        if df is None or len(df) < 101:
            print(f"[{now_str()}] Not enough candles yet ➡ sleeping...", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        df = compute_emas(df)

        # Cooldown check
        if cooldown_until is not None and now_ist() < cooldown_until:
            print(f"[{now_str()}] In cooldown until {cooldown_until} ➡ sleeping...", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        last_closed = df.iloc[-2]
        live_candle = df.iloc[-1]
        next_open = float(live_candle['open'])
        live_candle_time_iso = str(live_candle['time'].isoformat())

        # =================== IN POSITION ===================
        if in_position and position:
            dir_side = position['dir']
            entry_price = float(position['entry'])
            tp_price = float(position['tp_price'])
            sl_price = float(position['sl_price'])

            if last_processed_candle_time == live_candle_time_iso:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            low = float(live_candle['low'])
            high = float(live_candle['high'])
            if high < low:
                high, low = low, high
            intrabar_prices = np.linspace(low, high, INTRABAR_STEPS)

            outcome = None
            exit_price = None

            for p in intrabar_prices:
                if dir_side == 'BUY':
                    if p >= tp_price: outcome, exit_price = 'TP', tp_price; break
                    if p <= sl_price: outcome, exit_price = 'SL', sl_price; break
                else:
                    if p <= tp_price: outcome, exit_price = 'TP', tp_price; break
                    if p >= sl_price: outcome, exit_price = 'SL', sl_price; break

            if outcome is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # PnL calculation
            pnl = (exit_price - entry_price) * LOT_SIZE if dir_side == 'BUY' else (entry_price - exit_price) * LOT_SIZE
            if SIMULATION_MODE == 'on':
                balance += pnl

            rec = {
                'time': position['entry_time'].isoformat(),
                'dir': dir_side,
                'entry': entry_price,
                'exit': exit_price,
                'outcome': outcome,
                'pnl': round(pnl, 6),
                'balance': round(balance, 6)
            }
            append_trade_csv(rec)
            print(f"[{now_str()}] [{outcome}] {dir_side} trade closed. PnL: {round(pnl,6)} | Balance: {round(balance,6)}", flush=True)

            last_processed_candle_time = live_candle_time_iso
            in_position = False
            position = None

            if outcome == 'SL':
                cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
                print(f'[{now_str()}] SL hit → cooldown until {cooldown_until}', flush=True)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # =================== NOT IN POSITION: new signal ===================
        signal = check_signal(last_closed)
        last_closed_time_iso = str(last_closed['time'].isoformat())

        if signal and not in_position:
            if last_processed_candle_time == last_closed_time_iso:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            entry_price = float(next_open)
            if signal == 'BUY':
                tp_price = entry_price + TP_POINTS
                sl_price = entry_price - SL_POINTS
            else:
                tp_price = entry_price - TP_POINTS
                sl_price = entry_price + SL_POINTS

            position = {
                'dir': signal,
                'entry': entry_price,
                'tp_price': tp_price,
                'sl_price': sl_price,
                'entry_time': now_ist()
            }
            in_position = True
            print(f'[{now_str()}] Opening new trade {signal} @ {entry_price} | TP={tp_price} SL={sl_price}', flush=True)

        time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt received. Exiting gracefully...", flush=True)
        sys.exit(0)
    except Exception as e:
        print(f'[{now_str()}] [ERROR] {e}', flush=True)
        traceback.print_exc()
        time.sleep(10)
