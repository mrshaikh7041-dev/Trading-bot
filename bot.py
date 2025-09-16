import ccxt
import pandas as pd
import time
from datetime import datetime

# ---------------- CONFIG ----------------
SYMBOL = "ETH/USDT"
TIMEFRAME = "1m"
EMA_FAST = 13
EMA_SLOW = 55
LOT_SIZE = 0.01
TP_POINTS = 30
SL_POINTS = 40
FEE_RATE = 0.0006        # 0.06%
START_BALANCE = 4.0      # starting balance in USDT
LEVERAGE = 100
CANDLE_LIMIT = 50
SLIPPAGE_RATE = 0.0002    # 0.02% simulated slippage
COOLDOWN_AFTER_1_SL = 30  # candles
POLL_SLEEP = 1            # seconds between polls

# ---------------- EXCHANGE ----------------
exchange = ccxt.binance({
    "enableRateLimit": True,
    "options": {"defaultType": "future", "adjustForTimeDifference": True}
})

# ---------------- HELPERS ----------------
def nowstr():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(*args):
    print(nowstr(), *args, flush=True)

def fetch_candles(symbol, tf=TIMEFRAME, limit=CANDLE_LIMIT):
    ohlc = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlc, columns=["ts","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("time", inplace=True)
    return df

# ---------------- MAIN BOT ----------------
def run_paper_bot():
    balance = START_BALANCE
    sl_count = 0
    cooldown = 0

    active_trades = []   # open virtual trades
    trade_history = []   # closed trades summary
    prev_candle = None

    log(f"🚀 Starting PAPER BOT | Balance: {balance} USDT | Lot: {LOT_SIZE} | Leverage: {LEVERAGE}x")

    while True:
        try:
            df = fetch_candles(SYMBOL, TIMEFRAME, limit=CANDLE_LIMIT)
            df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
            df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
            df["ema_fast_window"] = df["ema_fast"].rolling(window=CANDLE_LIMIT).mean()
            df["ema_slow_window"] = df["ema_slow"].rolling(window=CANDLE_LIMIT).mean()

            current_candle = df.index[-1]
            ef_win = df["ema_fast_window"].iloc[-1]
            es_win = df["ema_slow_window"].iloc[-1]
            price = df["close"].iloc[-1]

            # cooldown decrement
            if prev_candle is None:
                prev_candle = current_candle
            elif current_candle != prev_candle:
                if cooldown > 0:
                    cooldown -= 1
                    log(f"Cooldown active -> {cooldown} candles remaining")
                prev_candle = current_candle

            # ---------- ENTRY ----------
            if cooldown == 0:
                side = None
                if ef_win > es_win:
                    side = "buy"
                elif ef_win < es_win:
                    side = "sell"

                if side:
                    # simulate slippage
                    if side == "buy":
                        entry_price = price * (1 + SLIPPAGE_RATE)
                        tp_price = entry_price + TP_POINTS
                        sl_price = entry_price - SL_POINTS
                    else:
                        entry_price = price * (1 - SLIPPAGE_RATE)
                        tp_price = entry_price - TP_POINTS
                        sl_price = entry_price + SL_POINTS

                    trade = {
                        "entry_time": current_candle,
                        "side": side,
                        "entry_price": entry_price,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "closed": False
                    }
                    active_trades.append(trade)
                    log(f"[ENTRY {side.upper()}] @ {round(entry_price,6)} | TP: {round(tp_price,6)} | SL: {round(sl_price,6)} | Balance: {round(balance,6)}")

            # ---------- MONITOR / EXIT ----------
            if active_trades:
                to_remove = []
                for t in active_trades:
                    h, l = df["high"].iloc[-1], df["low"].iloc[-1]
                    hit = None
                    if t["side"] == "buy":
                        if h >= t["tp_price"]:
                            hit = ("TP", t["tp_price"])
                        elif l <= t["sl_price"]:
                            hit = ("SL", t["sl_price"])
                    else:
                        if l <= t["tp_price"]:
                            hit = ("TP", t["tp_price"])
                        elif h >= t["sl_price"]:
                            hit = ("SL", t["sl_price"])

                    if hit:
                        outcome, exit_price = hit
                        pnl = (exit_price - t["entry_price"]) * LOT_SIZE if t["side"]=="buy" else (t["entry_price"] - exit_price) * LOT_SIZE
                        fee = t["entry_price"] * LOT_SIZE * FEE_RATE * 2
                        pnl -= fee
                        balance += pnl

                        trade_history.append({
                            "entry_time": t["entry_time"],
                            "side": t["side"],
                            "entry": t["entry_price"],
                            "exit": exit_price,
                            "outcome": outcome,
                            "pnl": round(pnl,6),
                            "balance": round(balance,6)
                        })

                        log(f"[EXIT {outcome}] {t['side'].upper()} | Entry: {round(t['entry_price'],6)} Exit: {round(exit_price,6)} | PnL: {round(pnl,6)} | Balance: {round(balance,6)}")

                        if outcome=="SL":
                            sl_count += 1
                            if sl_count >= 1:
                                cooldown = COOLDOWN_AFTER_1_SL
                                sl_count = 0
                                log(f"SL hit -> cooldown {COOLDOWN_AFTER_1_SL} candles")
                        else:
                            sl_count = 0

                        to_remove.append(t)

                # remove closed trades
                for r in to_remove:
                    try:
                        active_trades.remove(r)
                    except ValueError:
                        pass

            time.sleep(POLL_SLEEP)

        except KeyboardInterrupt:
            log("Interrupted by user. Final summary:")
            total = len(trade_history)
            wins = sum(1 for x in trade_history if x["outcome"]=="TP")
            total_pnl = sum(x["pnl"] for x in trade_history)
            win_rate = (wins/total*100) if total>0 else 0
            log(f"Trades: {total} | Wins: {wins} | Win%: {round(win_rate,2)} | Net PnL: {round(total_pnl,6)} | Final Balance: {round(balance,6)}")
            break

        except Exception as e:
            log("ERROR:", repr(e))
            time.sleep(2)
            continue

# ---------------- RUN ----------------
if __name__ == "__main__":
    run_paper_bot()
