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
SYMBOL = 'ETH/USDT'
TIMEFRAME = '1m'
LOT_SIZE = 0.01
TP_POINTS = 30
SL_POINTS = 15
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

# =================== STRATEGY: EMA + Volume Confirmation ===================
EMA_SHORT = 10
EMA_LONG = 50
VOL_WINDOW = 20
MIN_HISTORY = max(EMA_LONG, VOL_WINDOW) + 5

def compute_ema(df, period):
    return df['close'].ewm(span=period, adjust=False).mean()

def detect_crossover(df):
    """
    EMA + Volume Confirmation Strategy:
    - BUY: Short EMA crosses above Long EMA and Volume > 20-bar average.
    - SELL: Short EMA crosses below Long EMA and Volume > 20-bar average.
    """
    if len(df) < MIN_HISTORY:
        return None

    df['ema_short'] = compute_ema(df, EMA_SHORT)
    df['ema_long'] = compute_ema(df, EMA_LONG)
    df['vol_ma'] = df['volume'].rolling(VOL_WINDOW).mean()

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    # Volume confirmation
    high_vol = curr['volume'] > curr['vol_ma']

    # Check crossover + volume
    if high_vol:
        if prev['ema_short'] <= prev['ema_long'] and curr['ema_short'] > curr['ema_long']:
            return 'BUY'
        elif prev['ema_short'] >= prev['ema_long'] and curr['ema_short'] < curr['ema_long']:
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

    seed_limit = max(200, MIN_HISTORY + 10)
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=seed_limit)
    df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')

    print(f"[{now_str()}] Bot started | LIVE: {LIVE_MODE} | SIM: {SIMULATION_MODE}", flush=True)

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
                    'volume': float(k['v']),
                    'time': candle_start
                }

                if k['x']:
                    df = pd.concat([df, pd.DataFrame([current_candle])], ignore_index=True)
                    if len(df) > 1000:
                        df = df.iloc[-1000:].reset_index(drop=True)

                    if cooldown_until and now_ist() < cooldown_until:
                        pending_signal = None
                    else:
                        signal = detect_crossover(df)
                        if signal and not in_position:
                            pending_signal = {'dir': signal, 'created_at': now_ist()}
                            print(f"[{now_str()}] Signal detected -> {signal}. Pending entry on next candle open.", flush=True)

                    if in_position and SIMULATION_MODE == 'on':
                        outcome, exit_price = simulate_trade(position['dir'], position['entry'], position['tp'], position['sl'], current_candle)
                        if outcome:
                            pnl = (exit_price-position['entry'])*LOT_SIZE if position['dir']=='BUY' else (position['entry']-exit_price)*LOT_SIZE
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

                    last_processed_candle_time = current_candle['time']

                else:
                    if pending_signal and not in_position:
                        if last_entry_candle_time is None or candle_start != last_entry_candle_time:
                            if cooldown_until and now_ist() < cooldown_until:
                                pending_signal = None
                                print(f"[{now_str()}] Pending signal cleared due to active cooldown.", flush=True)
                            else:
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
                                print(f"[{now_str()}] ENTRY executed -> {dir_side} | Entry={entry_price} TP={tp_price} SL={sl_price}", flush=True)

                                if LIVE_MODE=='on' and SIMULATION_MODE=='off':
                                    try:
                                        qty = LOT_SIZE
                                        order = exchange.create_order(SYMBOL, 'market', dir_side.lower(), qty)
                                        print(f"[LIVE] Order placed: {order.get('id','(no id)')}", flush=True)
                                    except Exception as e:
                                        print(f"[LIVE] Order failed: {e}", flush=True)

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
