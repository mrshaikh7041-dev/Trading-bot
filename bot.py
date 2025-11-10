#!/usr/bin/env python3
"""
hybrid_rsi_bot.py
Refactored & arranged version of user's hybrid simulation/live RSI bot.
Keep SIMULATION=True for testing. Set LIVE=True and provide API keys to trade (use cautiously).
"""

import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta, timezone
import sys
import traceback
import logging

# 🔹 Log setup (added lines)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/ubuntu/Trading-bot/bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
print = lambda *args, **kwargs: logging.info(" ".join(map(str, args)))
# 🔹 End log setup

# ================== USER CONFIG ==================
SIMULATION = True
LIVE = False

API_KEY = ""     # <-- paste your API key here if you intend to run LIVE
API_SECRET = ""  # <-- paste your API secret here

COINS = {
    "BNB/USDT": {"lot_size": 0.10, "tp": 6.0, "sl": 3.0, "starting_balance": 4.0},
    "ETH/USDT": {"lot_size": 0.02, "tp": 30.0, "sl": 15.0, "starting_balance": 4.0},
    "XRP/USDT": {"lot_size": 25, "tp": 0.024, "sl": 0.012, "starting_balance": 4.0},
}

TIMEFRAME = "1m"
RSI_PERIOD = 14
RSI_LOW = 40
RSI_HIGH = 60

INTRABAR_STEPS = 50
SIM_FEE_PER_TRADE = 0.005  # flat per trade in simulation

COOLDOWN_MINUTES = 30
LEVERAGE = 75  # currently informational only

# ----------------- CCXT exchange setup -----------------
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# Timezone helper (IST)
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

# ----------------- HISTORICAL FETCH -----------------
def fetch_history(symbol, timeframe, days=1):
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
    all_bars = []
    fetch_since = since
    limit = 1000
    while True:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=fetch_since, limit=limit)
        except Exception as e:
            print(f"[{now_ist()}][FETCH_HISTORY][ERROR] {symbol}: {e}")
            break
        if not bars:
            break
        all_bars += bars
        fetch_since = bars[-1][0] + 1
        if len(bars) < limit:
            break
    if not all_bars:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(all_bars, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert(IST)
    return df.drop_duplicates(subset="time").reset_index(drop=True)

# ----------------- RSI (Wilder / EWMA) -----------------
def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).ewm(span=period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ----------------- INTRABAR OUTCOME -----------------
def intrabar_outcome(side, o, h, l, tp_lvl, sl_lvl):
    half = max(2, INTRABAR_STEPS // 2)
    up = np.linspace(o, h, half, endpoint=False)
    down = np.linspace(o, l, half, endpoint=True)
    path = np.concatenate([up, down])
    for p in path:
        if side == 'BUY':
            if p >= tp_lvl:
                return 'TP', tp_lvl
            if p <= sl_lvl:
                return 'SL', sl_lvl
        else:
            if p <= tp_lvl:
                return 'TP', tp_lvl
            if p >= sl_lvl:
                return 'SL', sl_lvl
    return None, None

# ----------------- HYBRID ENGINE -----------------
class Position:
    def __init__(self, side, entry, tp_raw, sl_raw, entry_time):
        self.side = side
        self.entry = entry
        self.tp_raw = tp_raw
        self.sl_raw = sl_raw
        self.entry_time = entry_time

class SymbolState:
    def __init__(self, symbol: str, cfg: dict):
        self.symbol = symbol
        self.cfg = cfg
        self.df = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        self.in_position = False
        self.position = None
        self.cooldown_until = None
        self.pending_signal = None
        self.pending_time = None
        self.sim_balance = cfg.get('starting_balance', 0.0)

    def seed(self, days=1):
        self.df = fetch_history(self.symbol, TIMEFRAME, days)

    def add_closed(self, k):
        row = {
            'open': float(k['o']), 'high': float(k['h']), 'low': float(k['l']), 'close': float(k['c']),
            'volume': float(k.get('v', 0)), 'time': pd.to_datetime(k['t'], unit='ms', utc=True).tz_convert(IST)
        }
        self.df = pd.concat([self.df, pd.DataFrame([row])], ignore_index=True)
        if len(self.df) > 2000:
            self.df = self.df.iloc[-1200:].reset_index(drop=True)

    def calc_signal(self):
        if len(self.df) < RSI_PERIOD + 2:
            return None
        close = self.df['close']
        rsi = rsi_wilder(close, RSI_PERIOD)
        r = rsi.iloc[-1]
        if pd.isna(r):
            return None
        if r < RSI_LOW:
            return 'BUY'
        if r > RSI_HIGH:
            return 'SELL'
        return None

states = {s: SymbolState(s, cfg) for s, cfg in COINS.items()}

# ----------------- SIMULATION -----------------
def sim_try_enter(state: SymbolState):
    if state.in_position:
        return
    if state.cooldown_until and now_ist() < state.cooldown_until:
        return
    sig = state.calc_signal()
    if not sig:
        return
    state.pending_signal = sig
    state.pending_time = state.df.iloc[-1]['time']

def sim_handle_entry_and_exit(state: SymbolState):
    if state.pending_signal and not state.in_position:
        next_open = float(state.df.iloc[-1]['open'])
        side = state.pending_signal
        entry = next_open
        if side == 'BUY':
            tp_raw = entry + state.cfg['tp']
            sl_raw = entry - state.cfg['sl']
        else:
            tp_raw = entry - state.cfg['tp']
            sl_raw = entry + state.cfg['sl']
        state.position = Position(side, entry, tp_raw, sl_raw, state.pending_time)
        state.in_position = True
        state.pending_signal = None
        state.pending_time = None
        print(f"[{now_ist()}][SIM][ENTER] {state.symbol} {side} @ {entry:.8f}")

    if state.in_position and state.position:
        pos = state.position
        o = float(state.df.iloc[-1]['open'])
        h = float(state.df.iloc[-1]['high'])
        l = float(state.df.iloc[-1]['low'])
        outcome, exit_px = intrabar_outcome(pos.side, o, h, l, pos.tp_raw, pos.sl_raw)
        if outcome:
            lot = state.cfg['lot_size']
            pnl = (exit_px - pos.entry) * lot if pos.side == 'BUY' else (pos.entry - exit_px) * lot
            pnl -= SIM_FEE_PER_TRADE
            state.sim_balance += pnl
            print(f"[{now_ist()}][SIM][EXIT] {state.symbol} {outcome} @ {exit_px:.8f} pnl={pnl:.8f} bal={state.sim_balance:.6f}")
            state.in_position = False
            state.position = None
            if outcome == 'SL':
                state.cooldown_until = state.df.iloc[-1]['time'] + timedelta(minutes=COOLDOWN_MINUTES)

# ----------------- MAIN LOOP -----------------
def seed_all(days=1):
    for s, st in states.items():
        st.seed(days=days)
        print(f"[{now_ist()}][SEED] {s}: {len(st.df)} candles")

def tick_loop(poll_interval=60):
    seed_all(days=1)
    print(f"[{now_ist()}] Starting main loop | SIMULATION={SIMULATION} LIVE={LIVE}")
    while True:
        try:
            for sym, st in states.items():
                try:
                    raw = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=2)
                except Exception as e:
                    print(f"[{now_ist()}][FETCH][ERROR] {sym}: {e}")
                    continue
                if not raw:
                    continue
                last = raw[-1]
                k = {'t': int(last[0]), 'o': last[1], 'h': last[2], 'l': last[3], 'c': last[4], 'v': last[5]}
                st.add_closed(k)
                if SIMULATION:
                    sim_try_enter(st)
                    sim_handle_entry_and_exit(st)
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("Interrupted by user. Exiting.")
            break
        except Exception as e:
            print(f"[{now_ist()}][MAIN_LOOP][ERROR] {e}")
            traceback.print_exc()
            time.sleep(5)

# ----------------- RUN -----------------
if __name__ == '__main__':
    if SIMULATION == LIVE:
        raise SystemExit("Set exactly one of SIMULATION or LIVE to True (not both).")
    print(f"Starting hybrid bot @ {now_ist()} | SIMULATION={SIMULATION} LIVE={LIVE}")
    tick_loop(poll_interval=60)
