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

# =================== STRATEGY ===================
def is_inside_bar(prev, curr):
    return curr['high'] < prev['high'] and curr['low'] > prev['low']

def compute_ema(df, period=100):
    return df['close'].ewm(span=period, adjust=False).mean().iloc[-1]

def generate_signal(prev_candle, last_candle, ema100):
    if not is_inside_bar(prev_candle, last_candle):
        return None
    close = last_candle['close']
    if close > ema100:
        return 'BUY'
    elif close < ema100:
        return 'SELL'
    else:
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
    global in_position, position, balance, cooldown_until, last_processed_candle_time

    # Seed last 2 candles
    df = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=101)
    df = pd.DataFrame(df, columns=['time','open','high','low','close','volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')

    prev_candle = df.iloc[-3].to_dict()
    last_candle = df.iloc[-2].to_dict()
    ema100 = compute_ema(df, period=100)

    # Binance WebSocket URL for 1m kline
    stream = f"wss://stream.binance.com:9443/ws/{SYMBOL.replace('/','').lower()}@kline_1m"
    async with websockets.connect(stream) as ws:
        print(f"[{now_str()}] Hybrid EMA100 Inside-Bar bot started | LIVE: {LIVE_MODE} | SIM: {SIMULATION_MODE}", flush=True)
        async for msg in ws:
            data = json.loads(msg)
            k = data['k']
            live_candle = {
                'open': float(k['o']),
                'high': float(k['h']),
                'low': float(k['l']),
                'close': float(k['c']),
                'time': pd.to_datetime(k['t'], unit='ms', utc=True).tz_convert('Asia/Kolkata')
            }

            # Only process closed candles
            if not k['x']:
                continue

            # Skip cooldown
            if cooldown_until and now_ist() < cooldown_until:
                continue

            # Generate signal
            signal = generate_signal(prev_candle, last_candle, ema100)
            entry_price = live_candle['open']
            if signal and not in_position:
                if signal=='BUY':
                    tp_price = entry_price + TP_POINTS
                    sl_price = entry_price - SL_POINTS
                else:
                    tp_price = entry_price - TP_POINTS
                    sl_price = entry_price + SL_POINTS

                position = {
                    'dir': signal,
                    'entry': entry_price,
                    'tp': tp_price,
                    'sl': sl_price,
                    'entry_time': now_ist()
                }
                in_position = True
                print(f"[{now_str()}] Signal: {signal} | Entry={entry_price} TP={tp_price} SL={sl_price}", flush=True)

                if LIVE_MODE=='on' and SIMULATION_MODE=='off':
                    # Place live market order
                    try:
                        qty = LOT_SIZE
                        order = exchange.create_order(SYMBOL, 'market', signal.lower(), qty)
                        print(f"[LIVE] Order placed: {order['id']}")
                        # Here you can add TP/SL reduce-only orders if needed
                    except Exception as e:
                        print(f"[LIVE] Order failed: {e}")

            # Handle open position (simulation)
            if in_position and SIMULATION_MODE=='on':
                outcome, exit_price = simulate_trade(position['dir'], position['entry'], position['tp'], position['sl'], live_candle)
                if outcome:
                    pnl = (exit_price-position['entry'])*LOT_SIZE if position['dir']=='BUY' else (position['entry']-exit_price)*LOT_SIZE
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
                    print(f"[SIM] [{outcome}] {position['dir']} trade closed. PnL={round(pnl,6)} | Balance={round(balance,6)}", flush=True)
                    in_position=False
                    position=None
                    if outcome=='SL':
                        cooldown_until = now_ist()+timedelta(minutes=COOLDOWN_MINUTES)

            # Shift candles
            prev_candle = last_candle
            last_candle = live_candle
            # Update EMA100
            df = df.append(pd.DataFrame([live_candle]))
            if len(df)>100: df=df.iloc[-100:]
            ema100 = compute_ema(df, period=100)

# =================== RUN BOT ===================
try:
    asyncio.run(main_loop())
except KeyboardInterrupt:
    print("\n[INFO] KeyboardInterrupt received. Exiting gracefully...", flush=True)
    sys.exit(0)
except Exception as e:
    print(f"[ERROR] {e}", flush=True)
    traceback.print_exc()
