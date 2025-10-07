# ready_to_run_bot.py
import json
import csv
import time
from datetime import datetime
import traceback

import pandas as pd

# try to import correct websocket class
try:
    import websocket  # websocket-client
    WebSocketApp = websocket.WebSocketApp
except Exception as e:
    raise RuntimeError("websocket-client not found or wrong websocket package installed. Run: pip install websocket-client") from e

# ---------------- CONFIG ----------------
PAIR = 'bnbusdt'
TIMEFRAME = '1m'
EMAS = [10, 20, 50, 100]
LOT_SIZE = 0.10
TP_POINTS = 6.0
SL_POINTS = 3.0
COOLDOWN_CANDLES = 30
INITIAL_BALANCE = 5.0  # virtual balance
INTRABAR_STEPS = 20
LEVERAGE = 100  # virtual leverage for margin
CSV_FILE = 'trades_log.csv'

# ---------------- STATE ----------------
balance = float(INITIAL_BALANCE)
active_trade = None
cooldown_until_index = None  # index until which no entries allowed
candles = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'timestamp'])

# ---------------- INITIALIZE CSV ----------------
with open(CSV_FILE, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['timestamp', 'side', 'entry', 'exit', 'result', 'balance', 'reason'])

# ---------------- EMA FUNCTIONS ----------------
def calculate_emas(df):
    for period in EMAS:
        # compute EMA only if we have data
        if len(df) >= 1:
            df[f'EMA{period}'] = df['close'].ewm(span=period, adjust=False).mean()
        else:
            df[f'EMA{period}'] = pd.Series(dtype='float64')
    return df

# ---------------- LOG TRADE ----------------
def log_trade(side, entry, exit_price, result, reason):
    global balance
    ts = datetime.now().isoformat()
    with open(CSV_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([ts, side, entry, exit_price, result, balance, reason])
    print(f'{ts} | {side} | Entry: {entry:.6f} | Exit: {exit_price:.6f} | Result: {result} | Reason: {reason} | Balance: {balance:.4f}')

# ---------------- INTRABAR CHECK ----------------
def intrabar_check(candle):
    global active_trade, balance, cooldown_until_index, candles
    high, low = float(candle['high']), float(candle['low'])
    if high <= low:
        return
    step_size = (high - low) / INTRABAR_STEPS

    # simulate tick through the candle
    for i in range(1, INTRABAR_STEPS + 1):
        price = low + step_size * i
        if not active_trade:
            break

        # TP
        if active_trade['side'] == 'LONG' and price >= active_trade['tp']:
            pnl = LOT_SIZE * TP_POINTS
            balance += pnl
            log_trade('LONG', active_trade['entry'], active_trade['tp'], f'+{pnl:.4f}', 'TP')
            active_trade = None
            cooldown_until_index = None
            break
        elif active_trade['side'] == 'SHORT' and price <= active_trade['tp']:
            pnl = LOT_SIZE * TP_POINTS
            balance += pnl
            log_trade('SHORT', active_trade['entry'], active_trade['tp'], f'+{pnl:.4f}', 'TP')
            active_trade = None
            cooldown_until_index = None
            break

        # SL
        if active_trade['side'] == 'LONG' and price <= active_trade['sl']:
            pnl = -LOT_SIZE * SL_POINTS
            balance += pnl
            log_trade('LONG', active_trade['entry'], active_trade['sl'], f'{pnl:.4f}', 'SL')
            active_trade = None
            cooldown_until_index = len(candles) + COOLDOWN_CANDLES
            break
        elif active_trade['side'] == 'SHORT' and price >= active_trade['sl']:
            pnl = -LOT_SIZE * SL_POINTS
            balance += pnl
            log_trade('SHORT', active_trade['entry'], active_trade['sl'], f'{pnl:.4f}', 'SL')
            active_trade = None
            cooldown_until_index = len(candles) + COOLDOWN_CANDLES
            break

# ---------------- CHECK ENTRY ----------------
def check_entry():
    global active_trade, cooldown_until_index, candles, balance
    if active_trade:
        return
    if cooldown_until_index is not None and len(candles) < cooldown_until_index:
        # still in cooldown
        return
    if len(candles) < max(EMAS) + 1:
        # not enough data for EMAs
        return

    last = candles.iloc[-1]
    close = float(last['close'])
    entry_price = close
    position_value = LOT_SIZE * entry_price
    required_margin = position_value / LEVERAGE

    print(f"[DEBUG] Checking margin: Balance={balance:.4f}, Position Value={position_value:.4f}, Required Margin={required_margin:.6f}")

    if balance < required_margin:
        print(f"Insufficient virtual balance for required margin: Needed {required_margin:.6f}, Available {balance:.6f}")
        return

    ema_cols = [f'EMA{p}' for p in EMAS]
    ema_max = last[ema_cols].max()
    ema_min = last[ema_cols].min()

    # EMA breakout strategy
    if close > ema_max:
        # LONG setup
        active_trade = {
            'side': 'LONG',
            'entry': entry_price,
            'tp': entry_price + TP_POINTS,
            'sl': entry_price - SL_POINTS,
            'timestamp': datetime.now().isoformat()
        }
        print(f'LONG signal generated at {entry_price:.6f}')
    elif close < ema_min:
        # SHORT setup
        active_trade = {
            'side': 'SHORT',
            'entry': entry_price,
            'tp': entry_price - TP_POINTS,
            'sl': entry_price + SL_POINTS,
            'timestamp': datetime.now().isoformat()
        }
        print(f'SHORT signal generated at {entry_price:.6f}')

# ---------------- WEBSOCKET CALLBACKS ----------------
def on_message(ws, message):
    global candles
    try:
        data = json.loads(message)
        if 'k' not in data:
            return
        k = data['k']
        candle = {
            'open': float(k['o']),
            'high': float(k['h']),
            'low': float(k['l']),
            'close': float(k['c']),
            'timestamp': int(k['t'])
        }

        # new candle arrives only when timestamp differs
        if len(candles) == 0 or candle['timestamp'] != int(candles.iloc[-1]['timestamp']):
            candles = pd.concat([candles, pd.DataFrame([candle])], ignore_index=True)
            candles = calculate_emas(candles)

            # keep dataframe reasonably small
            if len(candles) > 1000:
                candles = candles.iloc[-1000:].reset_index(drop=True)

            # intrabar on the current candle and check entry
            intrabar_check(candle)
            check_entry()
    except Exception as e:
        print("on_message error:", e)
        traceback.print_exc()

def on_error(ws, error):
    print('WebSocket error:', error)

# on_close signature may receive (ws, close_status_code, close_msg)
def on_close(ws, close_status_code=None, close_msg=None):
    print('Connection closed', close_status_code, close_msg)

def on_open(ws):
    print('WebSocket connection opened')

# ---------------- START / RECONNECT LOOP ----------------
def start_ws():
    ws_url = f'wss://fstream.binance.com/ws/{PAIR}@kline_{TIMEFRAME}'
    while True:
        try:
            ws = WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            # run_forever with ping to keep connection healthy
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except KeyboardInterrupt:
            print("Interrupted by user, exiting.")
            break
        except Exception as e:
            print("WebSocket crashed, reconnecting in 5s...", e)
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    print("Starting bot. Make sure websocket-client & pandas are installed.")
    start_ws()
