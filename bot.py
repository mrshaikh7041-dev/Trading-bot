#!/usr/bin/env python3
"""
hybrid_rsi_bot_live_per_symbol.py
LIVE-ready (Binance USDT-M futures/perpetual) with:
- Per-symbol single active trade (each symbol can have 0 or 1 position independently)
- Market entry + TP (limit) and SL (stop market) as reduceOnly orders
- Attempts to set leverage, rounds qty to step/precision
- Testnet support (USE_TESTNET=True) — test thoroughly before real funds
"""

import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta, timezone
import sys
import traceback
import logging
import math

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/ubuntu/Trading-bot/bot_live_per_symbol.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.info

# ---------- USER CONFIG ----------
SIMULATION = False
LIVE = True
USE_TESTNET = False   # True -> Binance futures testnet (recommended initially)

API_KEY = "czpG6usnSKOVK5WHcW71y9ldXpDkBGvotp1omrydhsxegPDossHMklFLeiEEZtcJ"
API_SECRET = "cZuTDhXFMxqOc18OmMKhn4WizIjC8csrDZkfpuUUyASDXwk4l5o3FV36HBz5u2rO"

COINS = {
    # lot_size interpreted as base-asset quantity when LIVE (e.g., BNB units).
    "BNB/USDT": {"lot_size": 0.02, "tp": 6.0, "sl": 3.0, "starting_balance": 4.0},
}

TIMEFRAME = "1m"
RSI_PERIOD = 14
RSI_LOW = 20
RSI_HIGH = 60

INTRABAR_STEPS = 50
SIM_FEE_PER_TRADE = 0.005

COOLDOWN_MINUTES = 20
LEVERAGE = 75  # target leverage (best-effort)

POLL_INTERVAL = 5  # seconds between loop iterations (lower for live)

# ---------- Exchange init ----------
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}  # USDT-M futures
})

if USE_TESTNET:
    exchange.set_sandbox_mode(True)

# ---------- time helper ----------
IST = timezone(timedelta(hours=5, minutes=30))
def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)

# ---------- Fetch history ----------
def fetch_history(symbol, timeframe, days=1):
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
    all_bars = []
    fetch_since = since
    limit = 1000
    while True:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=fetch_since, limit=limit)
        except Exception as e:
            log(f"[{now_ist()}][FETCH_HISTORY][ERROR] {symbol}: {e}")
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

# ---------- RSI ----------
def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).ewm(span=period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(span=period, adjust=False).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ---------- Intrabar outcome ----------
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

# ---------- State classes ----------
class Position:
    def __init__(self, side, entry, tp_raw, sl_raw, entry_time, live_ids=None):
        self.side = side
        self.entry = entry
        self.tp_raw = tp_raw
        self.sl_raw = sl_raw
        self.entry_time = entry_time
        self.live_ids = live_ids or {}  # store order ids

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

# ---------- Exchange helpers ----------
def load_markets_safe():
    try:
        exchange.load_markets()
    except Exception as e:
        log(f"[{now_ist()}][LOAD_MARKETS][ERROR] {e}")

def get_market_info(symbol):
    try:
        return exchange.markets[symbol]
    except Exception:
        load_markets_safe()
        return exchange.markets.get(symbol)

def round_quantity(symbol, qty):
    info = get_market_info(symbol)
    if not info:
        return float(qty)
    try:
        prec_amount = info.get('precision', {}).get('amount', None)
        if prec_amount is not None:
            # ccxt precision.amount often int representing decimal places
            if isinstance(prec_amount, int):
                return float(round(qty, prec_amount))
            else:
                return float(round(qty, int(prec_amount)))
    except Exception:
        pass
    try:
        limits = info.get('limits', {}).get('amount', {})
        step_val = limits.get('step') or limits.get('min')
        if step_val:
            # determine decimals from step_val
            if step_val < 1:
                decimals = abs(int(math.floor(math.log10(step_val))))
                return float(round(qty, decimals))
    except Exception:
        pass
    return float(qty)

def attempt_set_leverage(symbol, leverage):
    try:
        if hasattr(exchange, 'set_leverage'):
            exchange.set_leverage(leverage, symbol)
            log(f"[{now_ist()}][LEVERAGE] set_leverage({symbol}) -> {leverage}")
            return True
    except Exception as e:
        log(f"[{now_ist()}][LEVERAGE][WARN] set_leverage failed: {e}")
    try:
        sym = symbol.replace('/', '')
        res = exchange.fapiPrivate_post_leverage({'symbol': sym, 'leverage': int(leverage)})
        log(f"[{now_ist()}][LEVERAGE] fapiPrivate_post_leverage result: {res}")
        return True
    except Exception as e:
        log(f"[{now_ist()}][LEVERAGE][WARN] fapiPrivate_post_leverage failed: {e}")
    log(f"[{now_ist()}][LEVERAGE][ERROR] Could not set leverage programmatically for {symbol}. Please set manually.")
    return False

# ---------- Live order helpers ----------
def place_market_entry_and_tp_sl_live(symbol, side, qty_raw, tp_price, sl_price):
    order_ids = {}
    qty = round_quantity(symbol, qty_raw)
    if qty <= 0:
        raise ValueError("Quantity after rounding <= 0")
    # 1) Market entry
    try:
        entry_order = exchange.create_order(symbol, 'market', side, qty)
        order_ids['entry'] = entry_order.get('id')
        log(f"[{now_ist()}][LIVE][ENTRY] {symbol} {side} qty={qty} entry_order_id={order_ids['entry']}")
    except Exception as e:
        log(f"[{now_ist()}][LIVE][ENTRY][ERROR] {e}")
        raise

    # 2) TP (limit) with reduceOnly
    try:
        tp_side = 'SELL' if side == 'BUY' else 'BUY'
        params_tp = {'reduceOnly': True}
        tp_order = exchange.create_order(symbol, 'limit', tp_side, qty, tp_price, params_tp)
        order_ids['tp'] = tp_order.get('id')
        log(f"[{now_ist()}][LIVE][TP] {symbol} {tp_side} qty={qty} price={tp_price} tp_order_id={order_ids['tp']}")
    except Exception as e:
        log(f"[{now_ist()}][LIVE][TP][ERROR] {e}")
        try:
            params_tp_alt = {'stopPrice': None, 'reduceOnly': True}
            tp_order = exchange.create_order(symbol, 'TAKE_PROFIT_LIMIT', tp_side, qty, tp_price, params_tp_alt)
            order_ids['tp'] = tp_order.get('id')
            log(f"[{now_ist()}][LIVE][TP][ALT] placed tp id {order_ids['tp']}")
        except Exception as e2:
            log(f"[{now_ist()}][LIVE][TP][FAIL] {e2}")

    # 3) SL (stop market) with reduceOnly
    try:
        sl_side = 'SELL' if side == 'BUY' else 'BUY'
        params_sl = {'stopPrice': sl_price, 'reduceOnly': True}
        sl_order = exchange.create_order(symbol, 'STOP_MARKET', sl_side, qty, None, params_sl)
        order_ids['sl'] = sl_order.get('id')
        log(f"[{now_ist()}][LIVE][SL] {symbol} {sl_side} qty={qty} stopPrice={sl_price} sl_order_id={order_ids['sl']}")
    except Exception as e:
        log(f"[{now_ist()}][LIVE][SL][ERROR] {e}")
        try:
            params_sl_alt = {'stopPrice': sl_price, 'reduceOnly': True, 'type': 'STOP_MARKET'}
            sl_order = exchange.create_order(symbol, 'market', sl_side, qty, None, params_sl_alt)
            order_ids['sl'] = sl_order.get('id')
            log(f"[{now_ist()}][LIVE][SL][ALT] placed sl id {order_ids['sl']}")
        except Exception as e2:
            log(f"[{now_ist()}][LIVE][SL][FAIL] {e2}")

    return order_ids

# ---------- SIMULATION functions ----------
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
        log(f"[{now_ist()}][SIM][ENTER] {state.symbol} {side} @ {entry:.8f}")

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
            log(f"[{now_ist()}][SIM][EXIT] {state.symbol} {outcome} @ {exit_px:.8f} pnl={pnl:.8f} bal={state.sim_balance:.6f}")
            state.in_position = False
            state.position = None
            if outcome == 'SL':
                state.cooldown_until = state.df.iloc[-1]['time'] + timedelta(minutes=COOLDOWN_MINUTES)

# ---------- LIVE handlers ----------
def live_try_enter(state: SymbolState):
    if state.in_position:
        return
    if state.cooldown_until and now_ist() < state.cooldown_until:
        return
    sig = state.calc_signal()
    if not sig:
        return
    state.pending_signal = sig
    state.pending_time = state.df.iloc[-1]['time']

def live_handle_entry_and_manage(state: SymbolState):
    """
    If pending_signal exists -> place market entry, set TP/SL reduceOnly.
    Track live order ids in state.position.live_ids.
    Reconcile local state by checking exchange positions (best-effort).
    """
    if state.pending_signal and not state.in_position:
        sig = state.pending_signal
        entry_px_est = float(state.df.iloc[-1]['open'])
        side = sig  # BUY or SELL
        qty = state.cfg['lot_size']
        qty_rounded = round_quantity(state.symbol, qty)
        if qty_rounded <= 0:
            log(f"[{now_ist()}][LIVE][ERROR] rounded qty <=0 for {state.symbol}, skipping")
            state.pending_signal = None
            state.pending_time = None
            return

        if side == 'BUY':
            tp_price = entry_px_est + state.cfg['tp']
            sl_price = entry_px_est - state.cfg['sl']
        else:
            tp_price = entry_px_est - state.cfg['tp']
            sl_price = entry_px_est + state.cfg['sl']

        attempt_set_leverage(state.symbol, LEVERAGE)

        try:
            live_ids = place_market_entry_and_tp_sl_live(state.symbol, side, qty_rounded, tp_price, sl_price)
            state.position = Position(side, entry_px_est, tp_price, sl_price, state.pending_time, live_ids=live_ids)
            state.in_position = True
            state.pending_signal = None
            state.pending_time = None
            log(f"[{now_ist()}][LIVE][ENTER] {state.symbol} {side} qty={qty_rounded} est_entry={entry_px_est:.8f} tp={tp_price:.8f} sl={sl_price:.8f}")
        except Exception as e:
            log(f"[{now_ist()}][LIVE][ENTER][ERROR] {e}")
            state.pending_signal = None
            state.pending_time = None
            return

    # Reconcile: if exchange shows position closed, clear local state
    if state.in_position:
        try:
            if hasattr(exchange, 'fetch_positions'):
                positions = exchange.fetch_positions([state.symbol])
                for p in positions:
                    if p.get('symbol') == state.symbol:
                        size = float(p.get('contracts') or p.get('positionAmt') or 0)
                        if abs(size) < 1e-8:
                            log(f"[{now_ist()}][LIVE][RECONCILE] {state.symbol} position closed on exchange -> clearing local state")
                            state.in_position = False
                            state.position = None
            else:
                # other ccxt variants: try to check open positions differently if needed
                pass
        except Exception as e:
            log(f"[{now_ist()}][LIVE][RECONCILE][WARN] could not reconcile position for {state.symbol}: {e}")

# ---------- Seeding ----------
def seed_all(days=1):
    load_markets_safe()
    for s, st in states.items():
        st.seed(days=days)
        log(f"[{now_ist()}][SEED] {s}: {len(st.df)} candles")

# ---------- MAIN LOOP ----------
def tick_loop(poll_interval=POLL_INTERVAL):
    seed_all(days=1)
    log(f"[{now_ist()}] Starting main loop | SIMULATION={SIMULATION} LIVE={LIVE} TESTNET={USE_TESTNET}")
    while True:
        try:
            for sym, st in states.items():
                try:
                    raw = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=2)
                except Exception as e:
                    log(f"[{now_ist()}][FETCH][ERROR] {sym}: {e}")
                    continue
                if not raw:
                    continue
                last = raw[-1]
                k = {'t': int(last[0]), 'o': last[1], 'h': last[2], 'l': last[3], 'c': last[4], 'v': last[5]}
                st.add_closed(k)
                if SIMULATION:
                    sim_try_enter(st)
                    sim_handle_entry_and_exit(st)
                else:
                    live_try_enter(st)
                    live_handle_entry_and_manage(st)
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            log("Interrupted by user. Exiting.")
            break
        except Exception as e:
            log(f"[{now_ist()}][MAIN_LOOP][ERROR] {e}")
            traceback.print_exc()
            time.sleep(5)

# ---------- RUN ----------
if __name__ == '__main__':
    if SIMULATION == LIVE:
        raise SystemExit("Set exactly one of SIMULATION or LIVE to True (not both).")
    if LIVE:
        if not API_KEY or not API_SECRET:
            raise SystemExit("For LIVE mode, set API_KEY and API_SECRET.")
        log(f"*** LIVE MODE ENABLED - TESTNET={USE_TESTNET} - DOUBLE CHECK BEFORE REAL FUNDS ***")
        load_markets_safe()
    print(f"Starting hybrid bot @ {now_ist()} | SIMULATION={SIMULATION} LIVE={LIVE}")
    tick_loop(poll_interval=POLL_INTERVAL)
