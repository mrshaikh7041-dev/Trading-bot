# ccxt_polling_multicoin_rsi.py
import ccxt
import pandas as pd
import numpy as np
import time
import json
from datetime import datetime, timedelta, timezone
import os
import csv
import logging
import sys
import traceback

# =================== CONFIG ===================
COINS = {
    "XRP/USDT": {"lot_size": 25, "balance": 4.0},
    "ETH/USDT": {"lot_size": 0.02, "balance": 4.0},  # user requested 0.02 lot for ETH
    # add more symbols here if needed
}

TIMEFRAME = "1m"
RSI_PERIOD = 14
RSI_LOW = 20
RSI_HIGH = 60

# Fixed USDT TP/SL (kept same as your bot)
TARGET_PROFIT_USDT = 0.6
STOP_LOSS_USDT = 0.3

# Derived points per lot (points = price change)
# For each symbol we'll compute TP/SL points using its lot_size when needed

LEVERAGE = 75
SIMULATION_MODE = "on"   # 'on' = paper trading
LIVE_MODE = "off"        # not used for polling version here
PAPER_BALANCE_DEFAULT = 2.0
COOLDOWN_MINUTES = 30
INTRABAR_STEPS = 50

FEE_PER_TRADE = 0.005  # fixed fee per closed trade in USDT (kept same)

CSV_DIR = "trade_logs"
LOG_FILE = "polling_bot.log"

# =================== LOGGING ===================
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

KOLKATA = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    return datetime.now(timezone.utc).astimezone(KOLKATA)


def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S %Z")


# =================== EXCHANGE (CCXT) ===================
exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
# If you want testnet with ccxt REST, you'd set apiKey/apiSecret and exchange.set_sandbox_mode(True)
# exchange.apiKey = "..."
# exchange.secret = "..."
# exchange.set_sandbox_mode(True)

# =================== UTIL: CSV APPEND ===================
os.makedirs(CSV_DIR, exist_ok=True)


def append_trade_csv(symbol, record):
    fn = os.path.join(CSV_DIR, f"{symbol.replace('/', '-')}_trades.csv")
    header = ["time", "dir", "entry", "exit", "outcome", "pnl", "balance", "fees"]
    file_exists = os.path.isfile(fn)
    with open(fn, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)


# =================== STRATEGY: RSI Detection ===================
def detect_rsi_signal_from_df(df):
    """Return 'BUY' / 'SELL' / None for last candle based on RSI(14) 40-60 rule."""
    if len(df) < RSI_PERIOD + 1:
        return None
    tmp = df.copy()
    delta = tmp["close"].diff()
    gain = delta.where(delta > 0, 0).ewm(span=RSI_PERIOD).mean()
    loss = -delta.where(delta < 0, 0).ewm(span=RSI_PERIOD).mean()
    rs = gain / loss
    tmp["rsi"] = 100 - (100 / (1 + rs))
    curr = tmp.iloc[-1]
    if pd.isna(curr["rsi"]):
        return None
    if curr["rsi"] < RSI_LOW:
        return "BUY"
    elif curr["rsi"] > RSI_HIGH:
        return "SELL"
    return None


# =================== SIMULATION / INTRABAR ===================
def simulate_trade(dir_side, entry, tp, sl, candle):
    """
    Simulate within a single candle using INTRABAR_STEPS evenly spaced between low..high.
    Returns (outcome, exit_price) or (None, None) if no TP/SL hit in that candle.
    """
    low, high = candle["low"], candle["high"]
    # ensure low <= high
    if high < low:
        high, low = low, high
    intrabar_prices = np.linspace(low, high, INTRABAR_STEPS)
    outcome, exit_price = None, None
    for p in intrabar_prices:
        if dir_side == "BUY":
            if p >= tp:
                outcome, exit_price = "TP", tp
                break
            if p <= sl:
                outcome, exit_price = "SL", sl
                break
        else:  # SELL
            if p <= tp:
                outcome, exit_price = "TP", tp
                break
            if p >= sl:
                outcome, exit_price = "SL", sl
                break
    return outcome, exit_price


# =================== MAIN POLLING BOT ===================
def polling_bot():
    # per-symbol state
    state = {}
    for symbol, cfg in COINS.items():
        state[symbol] = {
            "lot_size": cfg["lot_size"],
            "balance": cfg.get("balance", PAPER_BALANCE_DEFAULT),
            "in_position": False,
            "position": None,
            "pending_signal": None,  # stored as ('BUY'/'SELL', detected_time)
            "pending_signal_time": None,
            "cooldown_until": None,
            "last_processed_candle_time": None,
            "performance": {"total_trades": 0, "win_trades": 0, "total_pnl": 0.0, "last_hourly_check": now_ist()},
        }

    # seed: fetch history for each symbol
    seed_limit = 300  # get enough history (1m candles)
    symbol_df = {}
    for symbol in COINS.keys():
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=seed_limit)
            df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
            # ccxt returns ms UTC, store tz-aware in Kolkata
            df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata")
            symbol_df[symbol] = df
            if len(df) >= 1:
                state[symbol]["last_processed_candle_time"] = df.iloc[-1]["time"]
            print(f"[{now_str()}] Seeded {symbol} with {len(df)} candles.")
        except Exception as e:
            print(f"[{now_str()}] Error fetching seed for {symbol}: {e}")
            traceback.print_exc()
            symbol_df[symbol] = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    print(f"[{now_str()}] Polling bot started (CCXT polling). Symbols: {', '.join(COINS.keys())}")
    # main loop: wake up near next candle close
    try:
        while True:
            # compute sleep until next minute close (align to timeframe)
            now = datetime.utcnow()
            # for 1m timeframe, next candle close at next minute boundary
            next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
            sleep_seconds = (next_minute - now).total_seconds() + 0.6  # buffer a bit
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

            # After sleep, fetch latest candles for each symbol and process if there's a new closed candle
            for symbol in COINS.keys():
                try:
                    df = symbol_df.get(symbol, pd.DataFrame())
                    # fetch recent candles (limit small, we only need last few)
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=120)
                    new_df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
                    new_df["time"] = pd.to_datetime(new_df["time"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata")
                    # ensure dedup and keep only last 1000
                    if not df.empty:
                        combined = pd.concat([df, new_df], ignore_index=True).drop_duplicates(subset="time").reset_index(drop=True)
                    else:
                        combined = new_df.drop_duplicates(subset="time").reset_index(drop=True)
                    if len(combined) > 1000:
                        combined = combined.iloc[-1000:].reset_index(drop=True)
                    symbol_df[symbol] = combined

                    last_time = state[symbol]["last_processed_candle_time"]
                    latest_time = combined.iloc[-1]["time"] if len(combined) > 0 else None

                    # If there is a new closed candle (latest_time changed), process it
                    if latest_time is not None and (last_time is None or latest_time > last_time):
                        # current_candle is the newly closed candle
                        current_candle = combined.iloc[-1].to_dict()
                        # 1) FIRST: If there was a pending signal for this symbol -> enter at this candle's open
                        if state[symbol]["pending_signal"] and (not state[symbol]["in_position"]) and (not state[symbol]["cooldown_until"] or now_ist() >= state[symbol]["cooldown_until"]):
                            dir_side = state[symbol]["pending_signal"]
                            entry_price = current_candle["open"]
                            lot_size = state[symbol]["lot_size"]
                            tp_points = TARGET_PROFIT_USDT / lot_size
                            sl_points = STOP_LOSS_USDT / lot_size
                            if dir_side == "BUY":
                                tp_price = entry_price + tp_points
                                sl_price = entry_price - sl_points
                            else:
                                tp_price = entry_price - tp_points
                                sl_price = entry_price + sl_points
                            # Create position
                            state[symbol]["position"] = {"dir": dir_side, "entry": entry_price, "tp": tp_price, "sl": sl_price, "entry_time": current_candle["time"]}
                            state[symbol]["in_position"] = True
                            state[symbol]["pending_signal"] = None
                            state[symbol]["pending_signal_time"] = None
                            print(f"[{now_str()}] [{symbol}] ENTRY -> {dir_side} | Entry={entry_price:.6f} TP={tp_price:.6f} SL={sl_price:.6f}")

                        # 2) SECOND: If in_position -> try to simulate exit over this candle (or subsequent candles)
                        if state[symbol]["in_position"] and SIMULATION_MODE == "on":
                            pos = state[symbol]["position"]
                            # only simulate if entry_time < current candle time (we entered at an earlier candle open)
                            if pos and pos.get("entry_time") and pos["entry_time"] <= current_candle["time"]:
                                outcome, exit_price = simulate_trade(pos["dir"], pos["entry"], pos["tp"], pos["sl"], current_candle)
                                if outcome:
                                    # fixed USDT pnl logic
                                    pnl = TARGET_PROFIT_USDT if outcome == "TP" else -STOP_LOSS_USDT
                                    pnl -= FEE_PER_TRADE
                                    state[symbol]["balance"] += pnl
                                    perf = state[symbol]["performance"]
                                    perf["total_trades"] += 1
                                    perf["total_pnl"] += pnl
                                    if outcome == "TP":
                                        perf["win_trades"] += 1

                                    rec = {
                                        "time": pos["entry_time"].isoformat(),
                                        "dir": pos["dir"],
                                        "entry": round(pos["entry"], 6),
                                        "exit": round(exit_price if exit_price is not None else pos["entry"], 6),
                                        "outcome": outcome,
                                        "pnl": round(pnl, 6),
                                        "balance": round(state[symbol]["balance"], 6),
                                        "fees": FEE_PER_TRADE,
                                    }
                                    append_trade_csv(symbol, rec)
                                    print(f"[{now_str()}] [{symbol}] [{outcome}] {pos['dir']} closed. PnL=${round(pnl,6)} | Bal=${round(state[symbol]['balance'],6)}")
                                    # reset position
                                    state[symbol]["in_position"] = False
                                    state[symbol]["position"] = None
                                    if outcome == "SL":
                                        state[symbol]["cooldown_until"] = now_ist() + timedelta(minutes=COOLDOWN_MINUTES)
                                        print(f"[{now_str()}] [{symbol}] SL hit -> cooldown until {state[symbol]['cooldown_until']}")

                        # 3) THIRD: Detect new RSI signal on this newly closed candle and set pending_signal for next candle
                        # detection only if not currently in position and not pending and not in cooldown
                        if (not state[symbol]["in_position"]) and (not state[symbol]["pending_signal"]) and (not state[symbol]["cooldown_until"] or now_ist() >= state[symbol]["cooldown_until"]):
                            # use combined (history) to detect
                            signal = detect_rsi_signal_from_df(combined)
                            if signal:
                                state[symbol]["pending_signal"] = signal
                                state[symbol]["pending_signal_time"] = current_candle["time"]
                                print(f"[{now_str()}] [{symbol}] SIGNAL DETECTED -> {signal} | will enter at next candle open")

                        # update last_processed time
                        state[symbol]["last_processed_candle_time"] = latest_time

                        # hourly summary per symbol
                        perf = state[symbol]["performance"]
                        now_time = now_ist()
                        if (perf["last_hourly_check"] is None) or (now_time - perf["last_hourly_check"] >= timedelta(hours=1)):
                            if perf["total_trades"] > 0:
                                win_rate = (perf["win_trades"] / perf["total_trades"]) * 100
                                avg_pnl = perf["total_pnl"] / perf["total_trades"]
                                print(f"[{now_str()}] [{symbol}] Summary -> Trades: {perf['total_trades']} | Win%: {win_rate:.1f}% | TotalPnL: ${perf['total_pnl']:.4f} | AvgPnL: ${avg_pnl:.4f}")
                            perf["last_hourly_check"] = now_time

                except Exception as e_sym:
                    print(f"[{now_str()}] Error processing {symbol}: {e_sym}")
                    traceback.print_exc()

    except KeyboardInterrupt:
        print("\n[INFO] KeyboardInterrupt - exiting, printing final summaries.")
        for symbol in COINS.keys():
            perf = state[symbol]["performance"]
            if perf["total_trades"] > 0:
                win_rate = (perf["win_trades"] / perf["total_trades"]) * 100
                avg_pnl = perf["total_pnl"] / perf["total_trades"]
                print(f"[{symbol}] Trades: {perf['total_trades']} | Win%: {win_rate:.1f}% | TotalPnL: ${perf['total_pnl']:.4f} | AvgPnL: ${avg_pnl:.4f}")
        sys.exit(0)
    except Exception as e:
        print(f"[{now_str()}] Fatal error in main loop: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    polling_bot()
