import websocket
import json
import pandas as pd
import csv
from datetime import datetime

# ---------------- CONFIG ----------------
PAIR = 'bnbusdt'
TIMEFRAME = '1m'
EMAS = [10, 20, 50, 100]
LOT_SIZE = 0.10
TP_POINTS = 6
SL_POINTS = 3
COOLDOWN_CANDLES = 30
INITIAL_BALANCE = 5.0  # virtual balance
INTRABAR_STEPS = 20
LEVERAGE = 100  # virtual leverage for margin
CSV_FILE = 'trades_log.csv'

# ---------------- STATE ----------------
balance = INITIAL_BALANCE
active_trade = None
cooldown_until = None
candles = pd.DataFrame()

# ---------------- INITIALIZE CSV ----------------
with open(CSV_FILE, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['timestamp', 'side', 'entry', 'exit', 'result', 'balance', 'reason'])

# ---------------- EMA FUNCTIONS ----------------
def calculate_emas(df):
    for period in EMAS:
        df[f'EMA{period}'] = df['close'].ewm(span=period, adjust=False).mean()
    return df

# ---------------- LOG TRADE ----------------
def log_trade(side, entry, exit_price, reason):
    global balance
    with open(CSV_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now(), side, entry, exit_price, reason, balance, reason])
    print(f'{datetime.now()} | {side} | Entry: {entry:.4f} | Exit: {exit_price:.4f} | Reason: {reason} | Balance: {balance:.4f}')

# ---------------- INTRABAR CHECK ----------------
def intrabar_check(candle):
    global active_trade, balance, cooldown_until
    high, low = candle['high'], candle['low']
    step_size = (high - low) / INTRABAR_STEPS

    for i in range(1, INTRABAR_STEPS + 1):
        price = low + step_size * i
        if not active_trade:
            break

        # TP
        if active_trade['side'] == 'LONG' and price >= active_trade['tp']:
            balance += LOT_SIZE * TP_POINTS
            log_trade('LONG', active_trade['entry'], active_trade['tp'], 'TP')
            active_trade = None
            cooldown_until = None
            break
        elif active_trade['side'] == 'SHORT' and price <= active_trade['tp']:
            balance += LOT_SIZE * TP_POINTS
            log_trade('SHORT', active_trade['entry'], active_trade['tp'], 'TP')
            active_trade = None
            cooldown_until = None
            break

        # SL
        elif active_trade['side'] == 'LONG' and price <= active_trade['sl']:
            balance -= LOT_SIZE * SL_POINTS
            log_trade('LONG', active_trade['entry'], active_trade['sl'], 'SL')
            active_trade = None
            cooldown_until = len(candles) + COOLDOWN_CANDLES
            break
        elif active_trade['side'] == 'SHORT' and price >= active_trade['sl']:
            balance -= LOT_SIZE * SL_POINTS
            log_trade('SHORT', active_trade['entry'], active_trade['sl'], 'SL')
            active_trade = None
            cooldown_until = len(candles) + COOLDOWN_CANDLES
            break

# ---------------- CHECK ENTRY ----------------
def check_entry():
    global active_trade, cooldown_until, candles, balance
    if active_trade:
        return
    if cooldown_until and len(candles) < cooldown_until:
        return

    last = candles.iloc[-1]
    close = last['close']
    entry_price = close
    position_value = LOT_SIZE * entry_price
    required_margin = position_value / LEVERAGE

    print(f"[DEBUG] Checking margin: Balance={balance:.4f}, Position Value={position_value:.4f}, Required Margin={required_margin:.4f}")

    if balance < required_margin:
        print(f"Insufficient balance for virtual leverage. Needed: {required_margin:.4f}, Available: {balance:.4f}")
        return

    # EMA breakout strategy
    if close > last[[f'EMA{p}' for p in EMAS]].max():
        # LONG setup
        active_trade = {
            'side': 'LONG',
            'entry': entry_price,
            'tp': entry_price + TP_POINTS,
            'sl': entry_price - SL_POINTS,
            'timestamp': datetime.now()
        }
        print(f'LONG signal generated at {entry_price:.4f}')
    elif close < last[[f'EMA{p}' for p in EMAS]].min():
        # SHORT setup
        active_trade = {
            'side': 'SHORT',
            'entry': entry_price,
            'tp': entry_price - TP_POINTS,
            'sl': entry_price + SL_POINTS,
            'timestamp': datetime.now()
        }
        print(f'SHORT signal generated at {entry_price:.4f}')

# ---------------- WEBSOCKET ----------------
def on_message(ws, message):
    global candles
    data = json.loads(message)
    k = data['k']
    candle = {
        'open': float(k['o']),
        'high': float(k['h']),
        'low': float(k['l']),
        'close': float(k['c']),
        'timestamp': k['t']
    }

    if len(candles) == 0 or candle['timestamp'] != candles.iloc[-1]['timestamp']:
        candles = pd.concat([candles, pd.DataFrame([candle])], ignore_index=True)
        candles = calculate_emas(candles)

        if len(candles) > max(EMAS) + 1:
            intrabar_check(candle)
            check_entry()

def on_error(ws, error):
    print('Error:', error)

def on_close(ws):
    print('Connection closed')

def on_open(ws):
    print('WebSocket connection opened')

# ---------------- START ----------------
if __name__ == "__main__":
    ws_url = f'wss://fstream.binance.com/ws/{PAIR}@kline_{TIMEFRAME}'
    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()
