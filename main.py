# main.py
import os
import time
import ccxt
import pandas as pd
from datetime import datetime

# ----------------- CONFIG -----------------
API_KEY    = os.getenv("API_KEY")       # Binance API key from env variable
API_SECRET = os.getenv("API_SECRET")    # Binance API secret from env variable

if not API_KEY or not API_SECRET:
    raise SystemExit("Put your live API_KEY and API_SECRET in environment variables!")

SYMBOL          = "ETH/USDT"   # USDT-M futures symbol
LOT_SIZE        = 0.01         # fixed qty in ETH (base asset)
LEVERAGE        = 45           # fixed leverage
TIMEFRAME       = "1m"
EMA_FAST        = 13
EMA_SLOW        = 55
TP_POINTS       = 40.0         # Take Profit in price points
SL_POINTS       = 0.60         # Stop Loss in price points
COOLDOWN_AFTER_1_SL = 60       # cooldown in candles after 1 SL
MAX_POS_CANDLES = 60           # max position hold candles
WORKING_TYPE    = "MARK_PRICE"
LOG_CSV         = "futures_trades.csv"

# ----------------- BUILD EXCHANGE -----------------
exchange = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "options": {
        "defaultType": "future",
        "adjustForTimeDifference": True
    }
})

def log(*args):
    print(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)

# ----------------- HELPERS -----------------
def show_balance():
    try:
        balance = exchange.fetch_balance({"type": "future"})
        usdt = balance['total'].get('USDT', 0)
        log(f"Connected ✅ Futures Account Balance: {usdt} USDT")
    except Exception as e:
        log("Balance fetch error:", repr(e))

def ensure_leverage(sym, lev):
    try:
        exchange.set_leverage(int(lev), sym)
    except Exception:
        mkt = exchange.market(sym)['id']
        exchange.fapiPrivate_post_leverage({"symbol": mkt, "leverage": int(lev)})
    log(f"Leverage ensured: {lev}x for {sym}")

def fetch_ema_df(sym, tf="1m", limit=500):
    ohlc = exchange.fetch_ohlcv(sym, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlc, columns=["ts","open","high","low","close","volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("time", inplace=True)
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    return df

def latest_price(sym):
    return float(exchange.fetch_ticker(sym)["last"])

def place_market(sym, side, qty):
    return exchange.create_order(sym, type="market", side=side, amount=qty)

def place_sl_tp_reduce_only(sym, side, qty, sl_price, tp_price):
    params_common = {
        "reduceOnly": True,
        "positionSide": "BOTH",
        "workingType": WORKING_TYPE,
    }
    close_side = "sell" if side == "buy" else "buy"
    sl_order = exchange.create_order(
        sym, "STOP_MARKET", close_side, qty,
        params={**params_common, "stopPrice": float(sl_price)}
    )
    tp_order = exchange.create_order(
        sym, "TAKE_PROFIT_MARKET", close_side, qty,
        params={**params_common, "stopPrice": float(tp_price)}
    )
    return sl_order, tp_order

def fetch_order_safe(order_id, sym):
    try:
        return exchange.fetch_order(order_id, sym)
    except Exception:
        return None

def position_size(sym):
    try:
        positions = exchange.fetch_positions([sym])
        for p in positions:
            if p.get("symbol") == sym or p.get("info", {}).get("symbol") == sym.replace("/", ""):
                amt = float(p.get("contracts") or p.get("info", {}).get("positionAmt") or 0)
                return abs(amt)
    except Exception:
        pass
    return 0.0

def cancel_open_reduce_only(sym):
    try:
        open_orders = exchange.fetch_open_orders(sym)
        for o in open_orders:
            info = o.get("info", {}) or {}
            if info.get("reduceOnly") or o.get("reduceOnly"):
                try:
                    exchange.cancel_order(o["id"], sym)
                except Exception:
                    pass
    except Exception:
        pass

def save_trade_row(row):
    df = pd.DataFrame([row])
    header = not os.path.exists(LOG_CSV)
    df.to_csv(LOG_CSV, mode="a", header=header, index=False)

# ----------------- MAIN RUN LOOP -----------------
def run():
    show_balance()
    ensure_leverage(SYMBOL, LEVERAGE)
    sl_streak = 0
    cooldown = 0
    log(f"Starting LIVE Futures Bot | Symbol: {SYMBOL} | Leverage: {LEVERAGE}x | Lot: {LOT_SIZE}")

    in_position = False
    entry_side = None
    entry_price = None
    entry_candle_time = None
    tp_order_id = None
    sl_order_id = None

    while True:
        try:
            df = fetch_ema_df(SYMBOL, TIMEFRAME, limit=200)
            last_t = df.index[-1]

            if cooldown > 0:
                log(f"Cooldown active: {cooldown} candles remaining")
                time.sleep(60)
                cooldown -= 1
                continue

            if not in_position:
                ef, es = df["ema_fast"].iloc[-1], df["ema_slow"].iloc[-1]
                ef_prev, es_prev = df["ema_fast"].iloc[-2], df["ema_slow"].iloc[-2]

                side = None
                if ef_prev <= es_prev and ef > es:
                    side = "buy"
                elif ef_prev >= es_prev and ef < es:
                    side = "sell"

                if side is None:
                    time.sleep(2)
                    continue

                ensure_leverage(SYMBOL, LEVERAGE)
                px_now = latest_price(SYMBOL)
                order = place_market(SYMBOL, side, LOT_SIZE)
                avg = order.get("average") or order.get("price") or px_now
                entry_price = float(avg)
                entry_side = side
                entry_candle_time = last_t
                in_position = True
                log(f"ENTRY {side.upper()} @ {entry_price}")

                if side == "buy":
                    tp_price = entry_price + TP_POINTS
                    sl_price = entry_price - SL_POINTS
                else:
                    tp_price = entry_price - TP_POINTS
                    sl_price = entry_price + SL_POINTS

                sl_o, tp_o = place_sl_tp_reduce_only(SYMBOL, side, LOT_SIZE, sl_price, tp_price)
                sl_order_id = sl_o.get("id")
                tp_order_id = tp_o.get("id")
                log(f"Placed exits: TP @ {tp_price} | SL @ {sl_price}")

            else:
                qty = position_size(SYMBOL)
                if qty == 0.0:
                    tp_order = fetch_order_safe(tp_order_id, SYMBOL) if tp_order_id else None
                    sl_order = fetch_order_safe(sl_order_id, SYMBOL) if sl_order_id else None
                    outcome = "UNKNOWN"
                    exit_price = latest_price(SYMBOL)

                    if tp_order and str(tp_order.get("status","")).lower() in ["closed","filled"]:
                        outcome = "TP"
                        exit_price = float(tp_order.get("average") or tp_order.get("price") or exit_price)
                    elif sl_order and str(sl_order.get("status","")).lower() in ["closed","filled"]:
                        outcome = "SL"
                        exit_price = float(sl_order.get("average") or sl_order.get("price") or exit_price)

                    if entry_side == "buy":
                        pnl = (exit_price - entry_price) * LOT_SIZE
                    else:
                        pnl = (entry_price - exit_price) * LOT_SIZE

                    save_trade_row({
                        "time": datetime.utcnow().isoformat(),
                        "side": entry_side.upper(),
                        "entry": round(entry_price,6),
                        "exit": round(exit_price,6),
                        "outcome": outcome,
                        "pnl_base": round(pnl,6)
                    })
                    log(f"EXIT {outcome} @ {exit_price} | PnL (base): {round(pnl,6)}")
                    cancel_open_reduce_only(SYMBOL)

                    # ----------------- COOLDOWN AFTER 1 SL -----------------
                    if outcome == "SL":
                        sl_streak += 1
                        if sl_streak >= 1:   # 1 SL cooldown
                            cooldown = COOLDOWN_AFTER_1_SL
                            sl_streak = 0
                            log(f"1 SL -> cooldown {COOLDOWN_AFTER_1_SL} candles")
                    else:
                        sl_streak = 0

                    in_position = False
                    entry_side = None
                    entry_price = None
                    tp_order_id = None
                    sl_order_id = None

                else:
                    elapsed = max(1,int((df.index[-1]-entry_candle_time).total_seconds()//60))
                    if elapsed >= MAX_POS_CANDLES:
                        log("Max position life reached -> force closing position (market).")
                        close_side = "sell" if entry_side=="buy" else "buy"
                        exchange.create_order(SYMBOL,"market",close_side,LOT_SIZE,params={"reduceOnly": True})
                        cancel_open_reduce_only(SYMBOL)
                        time.sleep(2)
                    else:
                        time.sleep(5)

        except KeyboardInterrupt:
            log("Interrupted by user. Exiting and saving trades.")
            break
        except Exception as e:
            log("ERROR:", repr(e))
            time.sleep(3)
            continue

if __name__ == "__main__":
    run()
