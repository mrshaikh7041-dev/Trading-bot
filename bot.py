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
SL_POINTS = 3.0
TP_POINTS = 6.0
LEVERAGE = 100
BALANCE = 2.0
COOLDOWN_MINUTES = 30
POLL_INTERVAL_SECONDS = 5
INTRABAR_STEPS = 30  # intrabar mini-simulation count
CSV_FN = f'{SYMBOL.replace("/", "-")}_paper_trades.csv'
LOG_FILE = 'bot.log'
FEE_RATE = 0.0006  # optional fee rate per side (0.06%)

# =================== LOGGING SETUP ===================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# =================== EXCHANGE ===================
exchange = ccxt.binance({'enableRateLimit': True})

# =================== STATE ===================
balance = BALANCE
in_position = False
cooldown_until = None  # datetime in IST or None
position = None

# Track last candle (ISO string) where we processed a close (TP/SL)
last_processed_candle_time = None

# =================== UTILS ===================
KOLKATA = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)


def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')


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
    df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema15'] = df['close'].ewm(span=15, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    return df


def check_signal(candle):
    """
    Return 'BUY' or 'SELL' or None based on EMA rules.
    Expects candle to contain close, low, high, ema5, ema9, ema15, ema21.
    """
    try:
        c = float(candle['close'])
        l = float(candle['low'])
        h = float(candle['high'])
        ema5 = float(candle['ema5'])
        ema9 = float(candle['ema9'])
        ema15 = float(candle['ema15'])
        ema21 = float(candle['ema21'])
    except Exception:
        return None

    # Strong trend: all EMAs under/above close
    if c >= ema5 and c >= ema9 and c >= ema15 and c > ema21:
        return 'BUY'
    if c <= ema5 and c <= ema9 and c <= ema15 and c < ema21:
        return 'SELL'

    # Middle-EMA touch (price touched ema15 intrabar)
    if l <= ema15 <= h and c > ema5:
        return 'BUY'
    if l <= ema15 <= h and c < ema5:
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


# =================== STARTUP MESSAGE ===================
STARTUP_MSG = f"Starting EMA Bot ({SYMBOL}), Paper Trading (Intrabar x{INTRABAR_STEPS}) | Starting..."
print(f"[{now_str()}] {STARTUP_MSG}", flush=True)
log.info(STARTUP_MSG)

# =================== MAIN LOOP ===================
while True:
    try:
        df = fetch_latest_candles(SYMBOL, TIMEFRAME, 200)
        if df is None or len(df) < 12:
            print(f"[{now_str()}] Not enough candles yet ➡ sleeping...", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        df = compute_emas(df)

        # Check cooldown
        if cooldown_until is not None and now_ist() < cooldown_until:
            print(f"[{now_str()}] In cooldown until {cooldown_until} ➡ sleeping...", flush=True)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        last_closed = df.iloc[-2]  # the last fully closed candle
        live_candle = df.iloc[-1]  # current live candle
        next_open = float(live_candle['open'])

        # canonical ISO id for the live candle (string) to compare across loop iterations
        live_candle_time_iso = str(live_candle['time'].isoformat())

        # =================== IN POSITION HANDLING ===================
        if in_position and position:
            dir_side = position['dir']
            entry_price = float(position['entry'])
            tp_price = float(position['tp_price'])
            sl_price = float(position['sl_price'])

            # Skip processing again for the same live candle if already processed
            if last_processed_candle_time == live_candle_time_iso:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # --- Intrabar Simulation ---
            low = float(live_candle['low'])
            high = float(live_candle['high'])
            if high < low:
                high, low = low, high  # safety
            intrabar_prices = np.linspace(low, high, INTRABAR_STEPS)

            outcome = None
            exit_price = None

            for p in intrabar_prices:
                if dir_side == 'BUY':
                    if p >= tp_price:
                        outcome = 'TP'
                        exit_price = tp_price
                        break
                    if p <= sl_price:
                        outcome = 'SL'
                        exit_price = sl_price
                        break
                else:  # SELL
                    if p <= tp_price:
                        outcome = 'TP'
                        exit_price = tp_price
                        break
                    if p >= sl_price:
                        outcome = 'SL'
                        exit_price = sl_price
                        break

            # If neither TP nor SL hit intrabar, we wait for next candle
            if outcome is None:
                # Do nothing this cycle (position still open)
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # Process outcome (ensure we haven't double-processed)
            if last_processed_candle_time == live_candle_time_iso:
                log.info(f'Duplicate close skipped for candle {live_candle_time_iso}')
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # PnL calculation (simple: price difference * lot size)
            if dir_side == 'BUY':
                pnl = (exit_price - entry_price) * LOT_SIZE
            else:
                pnl = (entry_price - exit_price) * LOT_SIZE

            # Optionally subtract fees (roundtrip)
            # fee = entry_price * LOT_SIZE * FEE_RATE * 2
            # pnl -= fee

            balance += pnl
            rec = {
                'time': position['entry_time'].isoformat() if hasattr(position['entry_time'], 'isoformat') else str(position['entry_time']),
                'dir': dir_side,
                'entry': entry_price,
                'exit': exit_price,
                'outcome': outcome,
                'pnl': round(pnl, 6),
                'balance': round(balance, 6),
            }
            append_trade_csv(rec)
            msg = f'[{outcome}] {dir_side} trade closed (Intrabar). PnL: {round(pnl,6)} | Balance: {round(balance,6)}'
            print(f'[{now_str()}] {msg}', flush=True)
            log.info(msg)

            # mark this candle as processed so duplicates and same-candle re-entry are blocked
            last_processed_candle_time = live_candle_time_iso

            in_position = False
            position = None

            if outcome == 'SL':
                cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
                print(f'[{now_str()}] SL hit → cooldown until {cooldown_until}', flush=True)
                log.info(f'SL cooldown until {cooldown_until}')
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            # After TP, do not re-enter on the same live candle (block until next candle)
            print(f'[{now_str()}] TP occurred this candle → re-entry blocked until next candle.', flush=True)
            log.info('Re-entry blocked on same candle after TP.')
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # ------------------- NOT IN POSITION: check for new signal -------------------
        signal = check_signal(last_closed)

        # Prevent opening more than one entry from the same candle close:
        last_closed_time_iso = str(last_closed['time'].isoformat())

        if signal and not in_position:
            if last_processed_candle_time == last_closed_time_iso:
                # we've already handled this candle's close earlier -> skip opening
                print(f'[{now_str()}] Skipping entry: last_closed {last_closed_time_iso} already processed.', flush=True)
                log.info("Skipping entry because last_closed was already processed.")
            else:
                entry_price = float(next_open)
                if signal == "BUY":
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
                msg = f'Opening new trade {signal} @ entry={entry_price} | TP={tp_price} SL={sl_price}'
                print(f'[{now_str()}] {msg}', flush=True)
                log.info(msg)
        else:
            print(f'[{now_str()}] No valid signal or cooldown active.', flush=True)

        time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt received. Exiting gracefully...", flush=True)
        log.info("KeyboardInterrupt received. Bot stopped by user.")
        sys.exit(0)

    except Exception as e:
        msg = f"[FATAL ERROR] {e}"
        print(f'[{now_str()}] {msg}', flush=True)
        log.error(msg)
        traceback.print_exc()
        time.sleep(10)
