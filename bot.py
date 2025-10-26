import ccxt
import pandas as pd
import numpy as np
import asyncio
import websockets
import json
from datetime import datetime, timedelta, timezone
import os
import csv
import logging
import sys
import traceback

# =================== CONFIG ===================
SYMBOL = 'BNB/USDT'
TIMEFRAME = '1m'
LOT_SIZE = 0.10
TP_POINTS = 6.0
SL_POINTS = 3.0
LEVERAGE = 75

LIVE_MODE = 'off'          # 'on' = live trading
SIMULATION_MODE = 'on'     # 'on' = paper trading
PAPER_BALANCE = 2.0        # simulation balance
COOLDOWN_MINUTES = 30
INTRABAR_STEPS = 30
CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'
FEE_RATE = 0.0006

# ======= LIVE API KEYS (only needed if LIVE_MODE=='on') =======
API_KEY = ''       # put your key here
API_SECRET = ''    # put your secret here
USE_TESTNET = True  # True = Binance testnet for live trades

# =================== LOGGING ===================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# =================== STATE ===================
balance = PAPER_BALANCE if SIMULATION_MODE == 'on' else 0.0
in_position = False
position = None
cooldown_until = None
last_processed_candle_time = None
last_entry_candle_time = None
pending_signal = None  # holds {'dir': 'BUY'/'SELL', 'created_at': datetime}

KOLKATA = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)

def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== CSV Logging ===================
def append_trade_csv(record):
    header = ['time','dir','entry','exit','outcome','pnl','balance']
    file_exists = os.path.isfile(CSV_FN)
    with open(CSV_FN,'a',newline='',encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)

# =================== EXCHANGE ===================
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

if USE_TESTNET:
    exchange.set_sandbox_mode(True)

# =================== STRATEGY: EMA Crossover Confirmation (single set) ===================
EMA_SET = [10, 20, 50, 100]
SHORT_EMAS = EMA_SET[:len(EMA_SET)//2]  # [10,20]
LONG_EMAS = EMA_SET[len(EMA_SET)//2:]   # [50,100]
MIN_HISTORY = max(EMA_SET) + 2

def compute_ema_series(df, periods):
    """Return a dict period -> pandas Series (same index as df)."""
    out = {}
    for p in periods:
        out[p] = df['close'].ewm(span=p, adjust=False).mean()
    return out

def detect_crossover(df):
    """
    Detect crossover on the last closed candle in df.
    Returns 'BUY' or 'SELL' if crossover happened on last candle relative to previous candle,
    else None.
    """
    if len(df) < MIN_HISTORY:
        return None

    ema_series = compute_ema_series(df, EMA_SET)
    # build short and long average series
    short_cols = pd.DataFrame({f'ema{s}': ema_series[s] for s in SHORT_EMAS})
    long_cols = pd.DataFrame({f'ema{l}': ema_series[l] for l in LONG_EMAS})

    short_avg = short_cols.mean(axis=1)
    long_avg = long_cols.mean(axis=1)

    # previous and current (last index)
    prev_idx = len(df) - 2
    curr_idx = len(df) - 1

    short_prev = short_avg.iloc[prev_idx]
    long_prev = long_avg.iloc[prev_idx]
    short_curr = short_avg.iloc[curr_idx]
    long_curr = long_avg.iloc[curr_idx]

    # Crossover detection (require a change)
    if short_prev <= long_prev and short_curr > long_curr:
        return 'BUY'
    if short_prev >= long_prev and short_curr < long_curr:
        return 'SELL'
    return None

# =================== SIMULATION FUNCTIONS ===================
def simulate_trade(dir_side, entry, tp, sl, candle):
    low, high = candle['low'], candle['high']
    if high < low: high, low = low, high
    intrabar_prices = np.linspace(low, high, INTRABAR_STEPS)
    outcome, exit_price = None, None
    for p in intrabar_prices:
        if dir_side=='BUY':
            if p >= tp: outcome, exit_price = 'TP', tp; break
            if p <= sl: outcome, exit_price = 'SL', sl; break
        else:
            if p <= tp: outcome, exit_price = 'TP', tp; break
            if p >= sl: outcome, exit_price = 'SL', sl; break
    return outcome, exit_price

# =================== MAIN LOOP ===================
async def main_loop():
    global in_position, position, balance, cooldown_until
    global last_processed_candle_time, last_entry_candle_time, pending_signal

    # Seed history (get at least 200 candles to be safe)
    seed_limit = max(200, MIN_HISTORY + 10)
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=seed_limit)
    df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')

    print(f"[{now_str()}] Bot started | LIVE: {LIVE_MODE} | SIM: {SIMULATION_MODE}", flush=True)

    # initialize last_processed times
    if len(df) >= 2:
        last_processed_candle_time = df.iloc[-1]['time']
    else:
        last_processed_candle_time = None

    stream = f"wss://stream.binance.com:9443/ws/{SYMBOL.replace('/','').lower()}@kline_1m"

    async with websockets.connect(stream) as ws:
        async for msg in ws:
            try:
                data = json.loads(msg)
                k = data['k']
                candle_start = pd.to_datetime(k['t'], unit='ms', utc=True).tz_convert('Asia/Kolkata')
                current_candle = {
                    'open': float(k['o']),
                    'high': float(k['h']),
                    'low': float(k['l']),
                    'close': float(k['c']),
                    'time': candle_start
                }

                # If candle closed -> this is a closed candle we can analyze for crossover
                if k['x']:
                    # Append closed candle to df (this is the candle we detect crossover on)
                    df = pd.concat([df, pd.DataFrame([current_candle])], ignore_index=True)
                    # keep history reasonable
                    if len(df) > 1000:
                        df = df.iloc[-1000:].reset_index(drop=True)

                    # Skip if cooldown or already in position
                    if cooldown_until and now_ist() < cooldown_until:
                        # shift and continue; clear pending if any
                        pending_signal = None
                    else:
                        # detect crossover on this newly-closed candle
                        signal = detect_crossover(df)
                        if signal and not in_position:
                            # set pending signal which will be executed on next candle open
                            pending_signal = {'dir': signal, 'created_at': now_ist()}
                            print(f"[{now_str()}] Crossover detected -> {signal}. Pending entry on next candle open.", flush=True)

                    # If we have an open position (entered earlier), simulate using this closed candle
                    if in_position and SIMULATION_MODE == 'on':
                        # simulate using this closed candle (this is the candle after entry or subsequent candles)
                        outcome, exit_price = simulate_trade(position['dir'], position['entry'], position['tp'], position['sl'], current_candle)
                        if outcome:
                            pnl = (exit_price-position['entry'])*LOT_SIZE if position['dir']=='BUY' else (position['entry']-exit_price)*LOT_SIZE
                            # subtract fees (entry+exit)
                            fee = position['entry'] * LOT_SIZE * FEE_RATE * 2
                            pnl -= fee
                            balance += pnl
                            rec = {
                                'time': position['entry_time'].isoformat(),
                                'dir': position['dir'],
                                'entry': position['entry'],
                                'exit': exit_price,
                                'outcome': outcome,
                                'pnl': round(pnl,6),
                                'balance': round(balance,6)
                            }
                            append_trade_csv(rec)
                            print(f"[SIM] [{outcome}] {position['dir']} closed. PnL={round(pnl,6)} | Balance={round(balance,6)}", flush=True)
                            in_position=False
                            position=None
                            if outcome=='SL':
                                cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
                                print(f"[{now_str()}] SL hit -> cooldown until {cooldown_until}", flush=True)

                    # update last_processed time
                    last_processed_candle_time = current_candle['time']

                else:
                    # This is an updating (forming) candle. We will use the candle's open to execute pending entry (next candle open).
                    # Execute entry at the first update of that candle (avoid re-executing)
                    if pending_signal and not in_position:
                        # ensure we don't execute multiple times for same candle
                        if last_entry_candle_time is None or candle_start != last_entry_candle_time:
                            # Check cooldown again before entry
                            if cooldown_until and now_ist() < cooldown_until:
                                pending_signal = None
                                print(f"[{now_str()}] Pending signal cleared due to active cooldown.", flush=True)
                            else:
                                # Execute entry at this candle's open price
                                entry_price = float(k['o'])
                                dir_side = pending_signal['dir']
                                if dir_side == 'BUY':
                                    tp_price = entry_price + TP_POINTS
                                    sl_price = entry_price - SL_POINTS
                                else:
                                    tp_price = entry_price - TP_POINTS
                                    sl_price = entry_price + SL_POINTS

                                position = {
                                    'dir': dir_side,
                                    'entry': entry_price,
                                    'tp': tp_price,
                                    'sl': sl_price,
                                    'entry_time': now_ist()
                                }
                                in_position = True
                                last_entry_candle_time = candle_start
                                pending_signal = None
                                print(f"[{now_str()}] ENTRY executed on next open -> {dir_side} | Entry={entry_price} TP={tp_price} SL={sl_price}", flush=True)

                                # Live mode: place market order
                                if LIVE_MODE=='on' and SIMULATION_MODE=='off':
                                    try:
                                        qty = LOT_SIZE
                                        order = exchange.create_order(SYMBOL, 'market', dir_side.lower(), qty)
                                        print(f"[LIVE] Order placed: {order.get('id','(no id)')}", flush=True)
                                    except Exception as e:
                                        print(f"[LIVE] Order failed: {e}", flush=True)

                    # No simulation on forming candle. We'll wait until it closes to simulate entries/exits.

                # keep df size reasonable (we added closed candle above when k['x'])
                if len(df) > 1000:
                    df = df.iloc[-1000:].reset_index(drop=True)

            except Exception as e:
                print(f"[ERROR] Exception in websocket loop: {e}", flush=True)
                traceback.print_exc()

# =================== RUN BOT ===================
try:
    asyncio.run(main_loop())
except KeyboardInterrupt:
    print("\n[INFO] KeyboardInterrupt received. Exiting gracefully...", flush=True)
    sys.exit(0)
except Exception as e:
    print(f"[ERROR] {e}", flush=True)
    traceback.print_exc()
