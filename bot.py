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
SYMBOL = 'XRP/USDT'
TIMEFRAME = '1m'
LOT_SIZE = 25

# Fixed USDT values
TARGET_PROFIT_USDT = 0.6
STOP_LOSS_USDT = 0.3

TP_POINTS = TARGET_PROFIT_USDT / LOT_SIZE
SL_POINTS = STOP_LOSS_USDT / LOT_SIZE

LEVERAGE = 75
LIVE_MODE = 'off'          
SIMULATION_MODE = 'on'     
PAPER_BALANCE = 4.0       
COOLDOWN_MINUTES = 30
INTRABAR_STEPS = 50
CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'
FEE_PER_TRADE = 0.005      

# Strategy parameters
RSI_PERIOD = 14
RSI_LOW = 20
RSI_HIGH = 60

# ======= LIVE API KEYS =======
API_KEY = ''      
API_SECRET = ''   
USE_TESTNET = True  

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
pending_signal = None
pending_signal_time = None

performance = {'total_trades': 0, 'win_trades': 0, 'total_pnl': 0.0, 'last_hourly_check': None}

KOLKATA = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)

def now_str():
    return now_ist().strftime('%Y-%m-%d %H:%M:%S %Z')

# =================== CSV Logging ===================
def append_trade_csv(record):
    header = ['time','dir','entry','exit','outcome','pnl','balance','fees']
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

# =================== STRATEGY: RSI (40–60) ===================
def detect_rsi_signal(df):
    """
    RSI strategy:
    BUY when RSI < 40
    SELL when RSI > 60
    """
    if len(df) < RSI_PERIOD:
        return None

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(RSI_PERIOD).mean()
    loss = -delta.where(delta < 0, 0).rolling(RSI_PERIOD).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    curr = df.iloc[-1]
    if curr['rsi'] < RSI_LOW:
        return 'BUY'
    elif curr['rsi'] > RSI_HIGH:
        return 'SELL'
    return None

# =================== SIMULATION FUNCTIONS ===================
def simulate_trade(dir_side, entry, tp, sl, candle):
    low, high = candle['low'], candle['high']
    if high < low: 
        high, low = low, high
    intrabar_prices = np.linspace(low, high, INTRABAR_STEPS)
    outcome, exit_price = None, None
    
    for p in intrabar_prices:
        if dir_side == 'BUY':
            if p >= tp: 
                outcome, exit_price = 'TP', tp
                break
            if p <= sl: 
                outcome, exit_price = 'SL', sl
                break
        else:  # SELL
            if p <= tp: 
                outcome, exit_price = 'TP', tp
                break
            if p >= sl: 
                outcome, exit_price = 'SL', sl
                break
    return outcome, exit_price

def print_performance_summary():
    if performance['total_trades'] > 0:
        win_rate = (performance['win_trades'] / performance['total_trades']) * 100
        avg_pnl = performance['total_pnl'] / performance['total_trades']
        print(f"[PERFORMANCE] Trades: {performance['total_trades']} | Win Rate: {win_rate:.1f}% | Total PnL: ${performance['total_pnl']:.4f} | Avg PnL: ${avg_pnl:.4f}")

# =================== MAIN LOOP ===================
async def main_loop():
    global in_position, position, balance, cooldown_until
    global last_processed_candle_time, last_entry_candle_time
    global pending_signal, pending_signal_time
    global performance

    seed_limit = max(200, RSI_PERIOD + 10)
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=seed_limit)
    df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')

    print(f"[{now_str()}] Bot started | LIVE: {LIVE_MODE} | SIM: {SIMULATION_MODE}", flush=True)
    print(f"[CONFIG] Symbol: {SYMBOL} | Lot Size: {LOT_SIZE} | TP: ${TARGET_PROFIT_USDT} | SL: ${STOP_LOSS_USDT}", flush=True)
    print(f"[CONFIG] RSI Period: {RSI_PERIOD} | Levels: {RSI_LOW}-{RSI_HIGH}", flush=True)

    if len(df) >= 2:
        last_processed_candle_time = df.iloc[-1]['time']

    performance['last_hourly_check'] = now_ist()
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

                # --- Closed candle processing ---
                if k['x']:
                    df = pd.concat([df, pd.DataFrame([current_candle])], ignore_index=True)
                    if len(df) > 1000:
                        df = df.iloc[-1000:].reset_index(drop=True)

                    # 1️⃣ Check pending signal for entry
                    if pending_signal and not in_position and (not cooldown_until or now_ist() >= cooldown_until):
                        entry_price = current_candle['open']
                        dir_side = pending_signal
                        tp_price = entry_price + TP_POINTS if dir_side == 'BUY' else entry_price - TP_POINTS
                        sl_price = entry_price - SL_POINTS if dir_side == 'BUY' else entry_price + SL_POINTS

                        position = {'dir': dir_side, 'entry': entry_price, 'tp': tp_price, 'sl': sl_price, 'entry_time': current_candle['time']}
                        in_position = True
                        last_entry_candle_time = current_candle['time']

                        print(f"[{now_str()}] ENTRY -> {dir_side} | Entry=${entry_price:.6f} TP=${tp_price:.6f} SL=${sl_price:.6f}", flush=True)
                        pending_signal = None
                        pending_signal_time = None

                    # 2️⃣ Check exit
                    if in_position and SIMULATION_MODE == 'on':
                        if position and position['entry_time'] < current_candle['time']:
                            outcome, exit_price = simulate_trade(position['dir'], position['entry'], position['tp'], position['sl'], current_candle)
                            if outcome:
                                pnl = TARGET_PROFIT_USDT if outcome == "TP" else -STOP_LOSS_USDT
                                pnl -= FEE_PER_TRADE
                                balance += pnl

                                performance['total_trades'] += 1
                                performance['total_pnl'] += pnl
                                if outcome == 'TP': performance['win_trades'] += 1

                                rec = {'time': position['entry_time'].isoformat(),'dir': position['dir'],'entry': round(position['entry'], 6),
                                       'exit': round(exit_price, 6),'outcome': outcome,'pnl': round(pnl, 6),
                                       'balance': round(balance, 6),'fees': FEE_PER_TRADE}
                                append_trade_csv(rec)
                                print(f"[SIM] [{outcome}] {position['dir']} closed. PnL=${round(pnl,6)} | Balance=${round(balance,6)}", flush=True)

                                in_position = False
                                position = None
                                if outcome == 'SL':
                                    cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
                                    print(f"[{now_str()}] SL hit -> cooldown until {cooldown_until}", flush=True)

                    # 3️⃣ Detect RSI signal for next candle
                    if (not in_position) and (not pending_signal) and (not cooldown_until or now_ist() >= cooldown_until):
                        signal = detect_rsi_signal(df)
                        if signal:
                            pending_signal = signal
                            pending_signal_time = current_candle['time']
                            print(f"[{now_str()}] SIGNAL -> {signal} | Next candle entry", flush=True)

                    last_processed_candle_time = current_candle['time']

                    current_time = now_ist()
                    if (performance['last_hourly_check'] is None or 
                        current_time - performance['last_hourly_check'] >= timedelta(hours=1)):
                        print_performance_summary()
                        performance['last_hourly_check'] = current_time

            except Exception as e:
                print(f"[ERROR] {e}", flush=True)
                traceback.print_exc()

# =================== RUN BOT ===================
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt received. Exiting gracefully...", flush=True)
        print_performance_summary()
        sys.exit(0)
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        traceback.print_exc()
