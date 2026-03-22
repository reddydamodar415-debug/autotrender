import requests, json, os, time
from datetime import datetime, date
import pytz
from auto_token import load_token, refresh_token, send_telegram

IST = pytz.timezone("Asia/Kolkata")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "1739813994")

# ── FUTURES CONFIG ──────────────────────────────────────────────
MAX_TRADES_PER_DAY = 3
LOT_SIZE = {"NIFTY": 25, "BANKNIFTY": 15, "SENSEX": 10}
LOTS_PER_TRADE     = 1
POINTS_TARGET      = 50    # Exit when profit >= 50 points
POINTS_SL          = 30    # Exit when loss   >= 30 points
MAX_DAILY_LOSS_RS  = 5000
RSI_OVERBOUGHT     = 70
RSI_OVERSOLD       = 30
PCR_BULLISH        = 1.3
PCR_BEARISH        = 0.8
SCAN_INTERVAL      = 180   # seconds

# ── STATE ───────────────────────────────────────────────────────
price_history  = {"NIFTY": [], "BANKNIFTY": [], "SENSEX": []}
vwap_data      = {"NIFTY": [], "BANKNIFTY": [], "SENSEX": []}
last_signal    = {"NIFTY": None, "BANKNIFTY": None, "SENSEX": None}
open_positions = {}
daily_pnl      = 0.0
trades_today   = {"NIFTY": 0, "BANKNIFTY": 0, "SENSEX": 0}
trade_log      = []

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/"
}

# ── NSE SESSION ─────────────────────────────────────────────────
def get_nse_session():
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    try:
        s.get("https://www.nseindia.com", timeout=10)
        s.get("https://www.nseindia.com/option-chain", timeout=10)
    except:
        pass
    return s

nse = get_nse_session()

# ── FUTURES PRICE from NSE ───────────────────────────────────────
def get_futures_price(index):
    """Get near-month futures price for the index."""
    global nse
    symbol_map = {
        "NIFTY":     "NIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "SENSEX":    "SENSEX"
    }
    symbol = symbol_map[index]
    # Use NSE futures API
    try:
        if index == "SENSEX":
            # BSE SENSEX futures via NSE or BSE endpoint
            url = f"https://www.nseindia.com/api/quote-derivative?symbol={symbol}"
        else:
            url = f"https://www.nseindia.com/api/quote-derivative?symbol={symbol}"
        r = nse.get(url, timeout=10)
        if r.status_code == 401:
            nse = get_nse_session()
            r = nse.get(url, timeout=10)
        data = r.json()
        # Find near-month FUTIDX
        for item in data.get("stocks", []):
            meta = item.get("metadata", {})
            if meta.get("instrumentType") == "FUTIDX":
                return float(item["marketDeptOrderBook"]["tradeInfo"]["lastPrice"])
    except Exception as e:
        print(f"[FUTURES PRICE ERROR {index}] {e}")
    return 0.0

# ── PCR from NSE option chain ─────────────────────────────────
def get_pcr_and_price(index):
    """Returns (pcr, spot_price) from NSE option chain."""
    global nse
    try:
        r = nse.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={index}", timeout=10)
        if r.status_code == 401:
            nse = get_nse_session()
            r = nse.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={index}", timeout=10)
        oc = r.json()
        records = oc.get("filtered", {}).get("data", [])
        ce_oi = sum(r["CE"]["openInterest"] for r in records if "CE" in r)
        pe_oi = sum(r["PE"]["openInterest"] for r in records if "PE" in r)
        pcr   = round(pe_oi / ce_oi, 2) if ce_oi > 0 else 1.0
        spot  = float(oc["records"]["underlyingValue"])
        return pcr, spot
    except Exception as e:
        print(f"[PCR ERROR {index}] {e}")
    return 1.0, 0.0

# ── RSI CALCULATION ───────────────────────────────────────────
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas  = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains   = [d for d in deltas[-period:] if d > 0]
    losses  = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

# ── VWAP ──────────────────────────────────────────────────────
def update_vwap(index, price):
    vwap_data[index].append(price)
    return round(sum(vwap_data[index]) / len(vwap_data[index]), 2)

# ── SIGNAL ────────────────────────────────────────────────────
def get_signal(index):
    pcr, spot = get_pcr_and_price(index)
    fut_price = get_futures_price(index)
    # Use futures price if available, else fall back to spot
    price = fut_price if fut_price > 0 else spot
    if price == 0:
        return None

    price_history[index].append(price)
    if len(price_history[index]) > 50:
        price_history[index].pop(0)

    rsi  = calculate_rsi(price_history[index])
    vwap = update_vwap(index, price)

    ls = ss = 0
    reasons = []

    # RSI
    if rsi < RSI_OVERSOLD:
        ls += 1; reasons.append(f"RSI={rsi}(oversold→BUY)")
    elif rsi > RSI_OVERBOUGHT:
        ss += 1; reasons.append(f"RSI={rsi}(overbought→SELL)")

    # VWAP
    if price > vwap * 1.001:
        ls += 1; reasons.append("Above VWAP→BUY")
    elif price < vwap * 0.999:
        ss += 1; reasons.append("Below VWAP→SELL")

    # PCR
    if pcr > PCR_BULLISH:
        ls += 1; reasons.append(f"PCR={pcr}(bullish→BUY)")
    elif pcr < PCR_BEARISH:
        ss += 1; reasons.append(f"PCR={pcr}(bearish→SELL)")

    direction = "NEUTRAL"
    strength  = 0
    if ls == 3:
        direction, strength = "BUY", 3
    elif ss == 3:
        direction, strength = "SELL", 3

    return {
        "index":     index,
        "direction": direction,
        "strength":  strength,
        "price":     price,
        "fut_price": fut_price,
        "spot":      spot,
        "rsi":       rsi,
        "pcr":       pcr,
        "vwap":      vwap,
        "reasons":   reasons
    }

# ── UPSTOX FUTURES ORDER ──────────────────────────────────────
def get_upstox_headers():
    token = load_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

def get_futures_expiry_key(index):
    """Returns Upstox instrument key for near-month futures."""
    now = datetime.now(IST)
    # Near-month expiry (last Thursday of current month)
    # Format: NSE_FO|NIFTY25MARFUT or BSE_FO|SENSEX25MARFUT
    month_str = now.strftime("%y%b").upper()  # e.g. 25MAR
    if index == "SENSEX":
        return f"BSE_FO|SENSEX{month_str}FUT"
    elif index == "BANKNIFTY":
        return f"NSE_FO|BANKNIFTY{month_str}FUT"
    else:
        return f"NSE_FO|NIFTY{month_str}FUT"

def place_futures_order(index, transaction, qty):
    instrument_key = get_futures_expiry_key(index)
    lot_qty = qty * LOT_SIZE[index]
    payload = {
        "quantity":          lot_qty,
        "product":           "D",
        "validity":          "DAY",
        "tag":               "AutoTrenderNXT",
        "instrument_token":  instrument_key,
        "price":             0,
        "order_type":        "MARKET",
        "transaction_type":  transaction,
        "disclosed_quantity": 0,
        "trigger_price":     0,
        "is_amo":            False
    }
    try:
        r = requests.post("https://api.upstox.com/v2/order/place",
                          headers=get_upstox_headers(), json=payload, timeout=15)
        result = r.json()
        print(f"[ORDER] {transaction} {index} FUTURES: {result}")
        return result
    except Exception as e:
        print(f"[ORDER ERROR] {e}")
        return {"status": "error", "message": str(e)}

# ── ENTER FUTURES TRADE ───────────────────────────────────────
def enter_trade(sig):
    global daily_pnl
    index     = sig["index"]
    direction = sig["direction"]

    if trades_today[index] >= MAX_TRADES_PER_DAY:
        return
    if daily_pnl <= -MAX_DAILY_LOSS_RS:
        send_telegram(f"🛑 <b>DAILY LOSS LIMIT</b>\nNo more trades. Loss: ₹{abs(daily_pnl):.0f}")
        return

    transaction = "BUY" if direction == "BUY" else "SELL"
    result = place_futures_order(index, transaction, LOTS_PER_TRADE)

    if result.get("status") == "success":
        entry_price = sig["fut_price"] if sig["fut_price"] > 0 else sig["spot"]
        lot_qty     = LOTS_PER_TRADE * LOT_SIZE[index]
        target_price = round(entry_price + POINTS_TARGET, 2) if direction == "BUY" else round(entry_price - POINTS_TARGET, 2)
        sl_price     = round(entry_price - POINTS_SL, 2)     if direction == "BUY" else round(entry_price + POINTS_SL, 2)

        open_positions[index] = {
            "direction":   direction,
            "entry_price": entry_price,
            "target":      target_price,
            "sl":          sl_price,
            "qty":         lot_qty,
            "entry_time":  datetime.now(IST).strftime("%H:%M:%S")
        }
        trades_today[index] += 1

        msg = (f"✅ <b>FUTURES ORDER — {index}</b>\n\n"
               f"{'🟢 BUY' if direction == 'BUY' else '🔴 SELL'} {index} FUTURES\n"
               f"📈 Entry: ₹{entry_price:,.0f}\n"
               f"🎯 Target: ₹{target_price:,.0f} (+{POINTS_TARGET} pts)\n"
               f"🛑 SL: ₹{sl_price:,.0f} (-{POINTS_SL} pts)\n"
               f"🔢 Qty: {lot_qty} ({LOTS_PER_TRADE} lot)\n"
               f"📊 {' | '.join(sig['reasons'])}\n"
               f"⏰ {datetime.now(IST).strftime('%H:%M:%S')} IST")
        send_telegram(msg)
    else:
        send_telegram(f"❌ <b>ORDER FAILED — {index} FUTURES</b>\n{result.get('errors', result.get('message','?'))}")

# ── CHECK & EXIT OPEN POSITIONS ───────────────────────────────
def check_open_positions():
    global daily_pnl
    for index, pos in list(open_positions.items()):
        try:
            fut_price = get_futures_price(index)
            if fut_price <= 0:
                continue
            current = fut_price
            direction = pos["direction"]

            if direction == "BUY":
                pnl = (current - pos["entry_price"]) * pos["qty"]
            else:
                pnl = (pos["entry_price"] - current) * pos["qty"]

            print(f"  [{index} FUTURES {direction}] Entry:{pos['entry_price']:,.0f} Now:{current:,.0f} P&L:₹{pnl:.0f}")

            exit_reason = None
            if direction == "BUY":
                if current >= pos["target"]:
                    exit_reason = f"🎯 TARGET HIT (+₹{pnl:.0f})"
                elif current <= pos["sl"]:
                    exit_reason = f"🛑 STOP LOSS (-₹{abs(pnl):.0f})"
            else:
                if current <= pos["target"]:
                    exit_reason = f"🎯 TARGET HIT (+₹{pnl:.0f})"
                elif current >= pos["sl"]:
                    exit_reason = f"🛑 STOP LOSS (-₹{abs(pnl):.0f})"

            if exit_reason:
                close_txn = "SELL" if direction == "BUY" else "BUY"
                result = place_futures_order(index, close_txn, LOTS_PER_TRADE)
                if result.get("status") == "success":
                    daily_pnl += pnl
                    trade_log.append({"index": index, "direction": direction,
                                      "entry": pos["entry_price"], "exit": current, "pnl": round(pnl, 2)})
                    icon = "🟢" if pnl > 0 else "🔴"
                    msg = (f"{icon} <b>CLOSED — {index} FUTURES</b>\n\n"
                           f"{exit_reason}\n"
                           f"{'🟢 BUY' if direction == 'BUY' else '🔴 SELL'} | Entry:₹{pos['entry_price']:,.0f} → Exit:₹{current:,.0f}\n"
                           f"💰 P&L: <b>₹{pnl:+.0f}</b>\n"
                           f"📊 Daily P&L: ₹{daily_pnl:+.0f}\n"
                           f"⏰ {datetime.now(IST).strftime('%H:%M:%S')} IST")
                    send_telegram(msg)
                    del open_positions[index]
                    last_signal[index] = None
        except Exception as e:
            print(f"  [EXIT ERROR {index}] {e}")

# ── SQUARE OFF ALL ────────────────────────────────────────────
def square_off_all():
    if not open_positions:
        return
    send_telegram("⏰ <b>Market closing — squaring off all FUTURES positions</b>")
    for index, pos in list(open_positions.items()):
        close_txn = "SELL" if pos["direction"] == "BUY" else "BUY"
        place_futures_order(index, close_txn, LOTS_PER_TRADE)
        del open_positions[index]

# ── DAILY SUMMARY ─────────────────────────────────────────────
def send_daily_summary():
    if not trade_log:
        send_telegram(f"📋 <b>Daily Summary — {date.today().strftime('%d %b %Y')}</b>\n\nNo trades today (no 3/3 signals).")
        return
    wins   = [t for t in trade_log if t["pnl"] > 0]
    losses = [t for t in trade_log if t["pnl"] <= 0]
    log_text = "\n".join(f"• {t['index']} {t['direction']}: ₹{t['pnl']:+.0f}" for t in trade_log)
    send_telegram(f"📊 <b>DAILY SUMMARY — {date.today().strftime('%d %b %Y')}</b>\n\n"
                  f"Trades: {len(trade_log)} | ✅ {len(wins)} wins | ❌ {len(losses)} losses\n"
                  f"💰 Net P&L: <b>₹{daily_pnl:+.0f}</b>\n\n{log_text}\n\n📈 AutoTrender NXT")

# ── RESET DAILY ───────────────────────────────────────────────
def reset_daily():
    global daily_pnl
    for idx in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        price_history[idx].clear(); vwap_data[idx].clear()
        last_signal[idx] = None; trades_today[idx] = 0
    trade_log.clear(); daily_pnl = 0.0

# ── TIME HELPER ───────────────────────────────────────────────
def is_between(h1, m1, h2, m2):
    from datetime import time as dtime
    now = datetime.now(IST).time()
    return dtime(h1, m1) <= now <= dtime(h2, m2)

# ── MAIN LOOP ─────────────────────────────────────────────────
def main():
    send_telegram("🤖 <b>AutoTrender NXT — FUTURES MODE</b>\n\n"
                  "✅ Scanning: NIFTY FUT | BANKNIFTY FUT | SENSEX FUT\n"
                  "✅ Auto token refresh 9:00 AM\n"
                  "✅ Signal scan every 3 min\n"
                  "✅ Auto BUY/SELL futures orders via Upstox\n"
                  "✅ Target: +50 pts | SL: -30 pts\n"
                  "✅ Auto square-off 3:25 PM\n"
                  "✅ Daily summary 3:35 PM\n"
                  "☁️ Running 24/7 on cloud")

    last_token_date = None
    scan_count = 0

    while True:
        now   = datetime.now(IST)
        today = now.date()

        # Daily token refresh at 9:00 AM
        if now.hour == 9 and now.minute == 0 and last_token_date != today:
            refresh_token(); reset_daily(); last_token_date = today

        # Square off at 3:25 PM
        if now.hour == 15 and now.minute == 25:
            square_off_all()

        # Daily summary at 3:35 PM
        if now.hour == 15 and now.minute == 35 and last_token_date == today:
            send_daily_summary(); last_token_date = None

        # Sleep outside market hours (9:15 AM – 3:25 PM)
        if not is_between(9, 15, 15, 25):
            time.sleep(30)
            continue

        no_trade = not is_between(9, 45, 15, 20)
        scan_count += 1
        print(f"\nSCAN #{scan_count} {now.strftime('%H:%M:%S')}")

        for index in ["NIFTY", "BANKNIFTY", "SENSEX"]:
            try:
                sig = get_signal(index)
                if not sig:
                    continue
                print(f"  {index}: {sig['direction']} ({sig['strength']}/3) FUT:₹{sig['fut_price']:,.0f} RSI={sig['rsi']} PCR={sig['pcr']}")
                time.sleep(2)

                if index in open_positions:
                    check_open_positions()
                elif sig["strength"] == 3 and not no_trade and last_signal[index] != sig["direction"]:
                    enter_trade(sig)
                    last_signal[index] = sig["direction"]
            except Exception as e:
                print(f"  [ERROR {index}] {e}")
                time.sleep(2)

        if open_positions:
            check_open_positions()

        print(f"  P&L: ₹{daily_pnl:+.0f} | Open: {list(open_positions.keys()) or 'None'}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
