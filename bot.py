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

# Fixed USDT values (aapki requirement)
TARGET_PROFIT_USDT = 0.6
STOP_LOSS_USDT = 0.3

# Auto-calculate points
TP_POINTS = TARGET_PROFIT_USDT / LOT_SIZE  # 0.024
SL_POINTS = STOP_LOSS_USDT / LOT_SIZE      # 0.012

LEVERAGE = 75
LIVE_MODE = 'off'          # 'on' = live trading
SIMULATION_MODE = 'on'     # 'on' = paper trading
PAPER_BALANCE = 2.0       # simulation balance
COOLDOWN_MINUTES = 0
INTRABAR_STEPS = 50
CSV_FN = f'{SYMBOL.replace("/", "-")}_trades.csv'
LOG_FILE = 'bot.log'
FEE_PER_TRADE = 0.005      # 0.005 USDT one time per closed trade

# Strategy Parameters
BB_PERIOD = 20
BB_STD = 1.5

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

# NEW: Pending signal for next candle entry
pending_signal = None
pending_signal_time = None

# Performance tracking
performance = {
    'total_trades': 0,
    'win_trades': 0,
    'total_pnl': 0.0,
    'last_hourly_check': None
}

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

# =================== STRATEGY: BOLLINGER BANDS ===================
def detect_bb_signal(df):
    """
    Bollinger Bands Strategy (Backtest mein profitable thi)
    BUY: Price touches lower band
    SELL: Price touches upper band
    """
    if len(df) < BB_PERIOD:
        return None
    
    tmp = df.copy()
    tmp['bb_middle'] = tmp['close'].rolling(BB_PERIOD).mean()
    tmp['bb_std'] = tmp['close'].rolling(BB_PERIOD).std()
    tmp['bb_upper'] = tmp['bb_middle'] + (tmp['bb_std'] * BB_STD)
    tmp['bb_lower'] = tmp['bb_middle'] - (tmp['bb_std'] * BB_STD)
    
    curr = tmp.iloc[-1]
    
    # BUY when price touches lower band
    if curr['low'] <= curr['bb_lower']:
        return 'BUY'
    # SELL when price touches upper band
    elif curr['high'] >= curr['bb_upper']:
        return 'SELL'
    
    return None

# =================== SIMULATION FUNCTIONS ===================
def simulate_trade(dir_side, entry, tp, sl, candle):
    """
    Intrabar simulation across the candle from low->high evenly spaced.
    """
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
    """Print performance summary"""
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

    # seed history (get enough candles)
    seed_limit = max(200, BB_PERIOD + 10)
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=seed_limit)
    df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')

    print(f"[{now_str()}] Bot started | LIVE: {LIVE_MODE} | SIM: {SIMULATION_MODE}", flush=True)
    print(f"[CONFIG] Symbol: {SYMBOL} | Lot Size: {LOT_SIZE} | TP: ${TARGET_PROFIT_USDT} | SL: ${STOP_LOSS_USDT}", flush=True)
    print(f"[CONFIG] TP Points: {TP_POINTS:.6f} | SL Points: {SL_POINTS:.6f}", flush=True)

    if len(df) >= 2:
        last_processed_candle_time = df.iloc[-1]['time']
    else:
        last_processed_candle_time = None

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
                    # Append the closed candle to history
                    df = pd.concat([df, pd.DataFrame([current_candle])], ignore_index=True)
                    if len(df) > 1000:
                        df = df.iloc[-1000:].reset_index(drop=True)

                    # --- 1. FIRST: Check for pending signal from previous candle ---
                    if pending_signal and not in_position and (not cooldown_until or now_ist() >= cooldown_until):
                        # Next candle entry at OPEN price (Backtest jaisa)
                        entry_price = current_candle['open']
                        dir_side = pending_signal
                        
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
                            'entry_time': current_candle['time']
                        }
                        in_position = True
                        last_entry_candle_time = current_candle['time']
                        
                        print(f"[{now_str()}] ENTRY (Next Candle) -> {dir_side} | Entry=${entry_price:.6f} TP=${tp_price:.6f} SL=${sl_price:.6f}", flush=True)
                        
                        # Clear pending signal
                        pending_signal = None
                        pending_signal_time = None

                    # --- 2. SECOND: Check for position exit ---
                    if in_position and SIMULATION_MODE == 'on':
                        if position and position.get('entry_time') and position['entry_time'] < current_candle['time']:
                            outcome, exit_price = simulate_trade(
                                position['dir'], 
                                position['entry'], 
                                position['tp'], 
                                position['sl'], 
                                current_candle
                            )
                            
                            if outcome:
                                # Calculate PnL - FIXED USDT VALUES (Backtest jaisa)
                                if outcome == "TP":
                                    pnl = TARGET_PROFIT_USDT  # Fixed 0.6 USDT profit
                                elif outcome == "SL":
                                    pnl = -STOP_LOSS_USDT     # Fixed 0.3 USDT loss  
                                else:
                                    # NO_EXIT case only
                                    if position['dir'] == 'BUY':
                                        pnl = (exit_price - position['entry']) * LOT_SIZE
                                    else:
                                        pnl = (position['entry'] - exit_price) * LOT_SIZE
                                
                                # Apply fixed fee (0.005 USDT per trade)
                                pnl -= FEE_PER_TRADE
                                balance += pnl
                                
                                # Update performance tracking
                                performance['total_trades'] += 1
                                performance['total_pnl'] += pnl
                                if outcome == 'TP':
                                    performance['win_trades'] += 1
                                
                                # Create trade record
                                rec = {
                                    'time': position['entry_time'].isoformat(),
                                    'dir': position['dir'],
                                    'entry': round(position['entry'], 6),
                                    'exit': round(exit_price, 6),
                                    'outcome': outcome,
                                    'pnl': round(pnl, 6),
                                    'balance': round(balance, 6),
                                    'fees': FEE_PER_TRADE
                                }
                                
                                append_trade_csv(rec)
                                print(f"[SIM] [{outcome}] {position['dir']} closed. PnL=${round(pnl,6)} | Balance=${round(balance,6)} | Fee=${FEE_PER_TRADE}", flush=True)
                                
                                in_position = False
                                position = None
                                
                                if outcome == 'SL':
                                    cooldown_until = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
                                    print(f"[{now_str()}] SL hit -> cooldown until {cooldown_until}", flush=True)

                    # --- 3. THIRD: Detect new signal for NEXT candle ---
                    if (not in_position) and (not pending_signal) and (not cooldown_until or now_ist() >= cooldown_until):
                        signal = detect_bb_signal(df)
                        if signal:
                            # Store signal for next candle entry
                            pending_signal = signal
                            pending_signal_time = current_candle['time']
                            print(f"[{now_str()}] SIGNAL DETECTED -> {signal} | Will enter at next candle open", flush=True)

                    # Update last processed candle time
                    last_processed_candle_time = current_candle['time']
                    
                    # Hourly performance summary
                    current_time = now_ist()
                    if (performance['last_hourly_check'] is None or 
                        current_time - performance['last_hourly_check'] >= timedelta(hours=1)):
                        print_performance_summary()
                        performance['last_hourly_check'] = current_time

                # Keep df size reasonable
                if len(df) > 1000:
                    df = df.iloc[-1000:].reset_index(drop=True)

            except Exception as e:
                print(f"[ERROR] Exception in websocket loop: {e}", flush=True)
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
