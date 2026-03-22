import requests, json, os, time
from datetime import datetime, date
import pytz
from auto_token import load_token, refresh_token, send_telegram

IST = pytz.timezone("Asia/Kolkata")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "1739813994")

MAX_TRADES_PER_DAY = 3
LOT_SIZE = {"NIFTY": 25, "BANKNIFTY": 15, "SENSEX": 10}
LOTS_PER_TRADE = 1
PREMIUM_TARGET_PCT = 0.40
PREMIUM_SL_PCT     = 0.35
MAX_DAILY_LOSS_RS  = 5000
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30
PCR_BULLISH    = 1.3
PCR_BEARISH    = 0.8
SCAN_INTERVAL  = 180

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

def get_option_chain(index):
    global nse
    try:
        r = nse.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={index}", timeout=10)
        if r.status_code == 401:
            nse = get_nse_session()
            r = nse.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={index}", timeout=10)
        return r.json()
    except Exception as e:
        print(f"[OC ERROR {index}] {e}")
        return {}

def get_expiry_date(oc_data):
    try:
        return oc_data["records"]["expiryDates"][0]
    except:
        return ""

def get_atm_strike(price, step=50):
    return round(price / step) * step

def get_option_ltp(oc_data, strike, opt_type, expiry):
    try:
        for record in oc_data["records"]["data"]:
            if record["strikePrice"] == strike and record["expiryDate"] == expiry:
                return float(record[opt_type]["lastPrice"])
    except:
        pass
    return 0.0

def calculate_pcr(oc_data):
    try:
        records = oc_data.get("filtered", {}).get("data", [])
        ce_oi = sum(r["CE"]["openInterest"] for r in records if "CE" in r)
        pe_oi = sum(r["PE"]["openInterest"] for r in records if "PE" in r)
        return round(pe_oi / ce_oi, 2) if ce_oi > 0 else 1.0
    except:
        return 1.0

def get_underlying_price(oc_data):
    try:
        return float(oc_data["records"]["underlyingValue"])
    except:
        return 0.0

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.001
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def update_vwap(index, price):
    vwap_data[index].append(price)
    return round(sum(vwap_data[index]) / len(vwap_data[index]), 2)

def get_upstox_headers():
    token = load_token()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

def place_order(index, strike, opt_type, expiry, transaction, qty):
    exp = datetime.strptime(expiry, "%d-%b-%Y")
    exp_str = exp.strftime("%y%b").upper() + str(strike) + opt_type
    exchange = "BSE_FO" if index == "SENSEX" else "NSE_FO"
    instrument_key = f"{exchange}|{index}{exp_str}"
    lot_qty = qty * LOT_SIZE[index]
    payload = {
        "quantity": lot_qty, "product": "D", "validity": "DAY",
        "price": 0, "tag": "AutoTrenderNXT", "instrument_token": instrument_key,
        "order_type": "MARKET", "transaction_type": transaction,
        "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False
    }
    try:
        r = requests.post("https://api.upstox.com/v2/order/place",
                          headers=get_upstox_headers(), json=payload, timeout=15)
        result = r.json()
        print(f"[ORDER] {transaction} {index} {strike}{opt_type}: {result}")
        return result
    except Exception as e:
        print(f"[ORDER ERROR] {e}")
        return {"status": "error", "message": str(e)}

def get_signal(index, oc_data):
    price = get_underlying_price(oc_data)
    pcr   = calculate_pcr(oc_data)
    price_history[index].append(price)
    if len(price_history[index]) > 50:
        price_history[index].pop(0)
    rsi  = calculate_rsi(price_history[index])
    vwap = update_vwap(index, price)
    ls = ss = 0
    reasons = []
    if rsi < RSI_OVERSOLD:
        ls += 1; reasons.append(f"RSI={rsi}(oversold)")
    elif rsi > RSI_OVERBOUGHT:
        ss += 1; reasons.append(f"RSI={rsi}(overbought)")
    if price > vwap * 1.001:
        ls += 1; reasons.append("Above VWAP")
    elif price < vwap * 0.999:
        ss += 1; reasons.append("Below VWAP")
    if pcr > PCR_BULLISH:
        ls += 1; reasons.append(f"PCR={pcr}(bullish)")
    elif pcr < PCR_BEARISH:
        ss += 1; reasons.append(f"PCR={pcr}(bearish)")
    direction = "NEUTRAL"
    strength  = 0
    if ls == 3:
        direction, strength = "LONG", 3
    elif ss == 3:
        direction, strength = "SHORT", 3
    return {"index": index, "direction": direction, "strength": strength,
            "price": price, "rsi": rsi, "pcr": pcr, "vwap": vwap,
            "reasons": reasons, "expiry": get_expiry_date(oc_data)}

def enter_trade(sig, oc_data):
    global daily_pnl
    index = sig["index"]
    if trades_today[index] >= MAX_TRADES_PER_DAY:
        return
    if daily_pnl <= -MAX_DAILY_LOSS_RS:
        send_telegram(f"🛑 <b>DAILY LOSS LIMIT</b>\nNo more trades. Loss: ₹{abs(daily_pnl):.0f}")
        return
    price  = sig["price"]
    expiry = sig["expiry"]
    step   = 100 if index == "BANKNIFTY" else 50
    atm    = get_atm_strike(price, step)
    if sig["direction"] == "LONG":
        strike, opt_type = atm + step, "CE"
    else:
        strike, opt_type = atm - step, "PE"
    entry_premium = get_option_ltp(oc_data, strike, opt_type, expiry)
    if entry_premium <= 0:
        return
    result = place_order(index, strike, opt_type, expiry, "SELL", LOTS_PER_TRADE)
    if result.get("status") == "success":
        lot_qty = LOTS_PER_TRADE * LOT_SIZE[index]
        open_positions[index] = {
            "entry_premium": entry_premium, "strike": strike,
            "opt_type": opt_type, "expiry": expiry, "qty": lot_qty,
            "direction": sig["direction"],
            "target": round(entry_premium * (1 - PREMIUM_TARGET_PCT), 2),
            "sl":     round(entry_premium * (1 + PREMIUM_SL_PCT), 2),
            "entry_time": datetime.now(IST).strftime("%H:%M:%S")
        }
        trades_today[index] += 1
        msg = (f"✅ <b>ORDER PLACED — {index}</b>\n\n"
               f"📌 SELL {index} {strike}{opt_type}\n"
               f"📅 Expiry: {expiry}\n"
               f"💰 Entry: ₹{entry_premium}\n"
               f"🎯 Target: ₹{open_positions[index]['target']} (−40%)\n"
               f"⛔ SL: ₹{open_positions[index]['sl']} (+35%)\n"
               f"📦 Qty: {lot_qty} ({LOTS_PER_TRADE} lot)\n"
               f"📊 {' | '.join(sig['reasons'])}\n"
               f"⏰ {datetime.now(IST).strftime('%H:%M:%S')} IST")
        send_telegram(msg)
    else:
        send_telegram(f"❌ <b>ORDER FAILED — {index}</b>\n{result.get('errors', result.get('message','?'))}")

def check_open_positions(oc_data_all):
    global daily_pnl
    for index, pos in list(open_positions.items()):
        oc = oc_data_all.get(index, {})
        current = get_option_ltp(oc, pos["strike"], pos["opt_type"], pos["expiry"])
        if current <= 0:
            continue
        entry = pos["entry_premium"]
        pnl   = (entry - current) * pos["qty"]
        print(f"  [{index}] {pos['strike']}{pos['opt_type']} Entry:₹{entry} Now:₹{current:.2f} P&L:₹{pnl:.0f}")
        exit_reason = None
        if current <= pos["target"]:
            exit_reason = f"🎯 TARGET HIT (+₹{pnl:.0f})"
        elif current >= pos["sl"]:
            exit_reason = f"⛔ STOP LOSS (−₹{abs(pnl):.0f})"
        if exit_reason:
            result = place_order(index, pos["strike"], pos["opt_type"], pos["expiry"], "BUY", LOTS_PER_TRADE)
            if result.get("status") == "success":
                daily_pnl += pnl
                trade_log.append({"index": index, "strike": pos["strike"],
                                   "type": pos["opt_type"], "entry": entry,
                                   "exit": current, "pnl": round(pnl, 2)})
                msg = (f"{'🟢' if pnl > 0 else '🔴'} <b>CLOSED — {index}</b>\n\n"
                       f"{exit_reason}\n"
                       f"📌 {pos['strike']}{pos['opt_type']}\n"
                       f"💰 ₹{entry} → ₹{current:.2f}\n"
                       f"📈 P&L: <b>₹{pnl:+.0f}</b>\n"
                       f"📊 Daily P&L: ₹{daily_pnl:+.0f}\n"
                       f"⏰ {datetime.now(IST).strftime('%H:%M:%S')} IST")
                send_telegram(msg)
                del open_positions[index]
                last_signal[index] = None

def square_off_all():
    if not open_positions:
        return
    send_telegram("⏰ <b>Market closing — squaring off all positions</b>")
    for index, pos in list(open_positions.items()):
        place_order(index, pos["strike"], pos["opt_type"], pos["expiry"], "BUY", LOTS_PER_TRADE)
        del open_positions[index]

def send_daily_summary():
    if not trade_log:
        send_telegram(f"📋 <b>Daily Summary — {date.today().strftime('%d %b %Y')}</b>\n\nNo trades today (no 3/3 signals).")
        return
    wins = [t for t in trade_log if t["pnl"] > 0]
    losses = [t for t in trade_log if t["pnl"] <= 0]
    log_text = "\n".join(f"• {t['index']} {t['strike']}{t['type']}: ₹{t['pnl']:+.0f}" for t in trade_log)
    send_telegram(f"📊 <b>DAILY SUMMARY — {date.today().strftime('%d %b %Y')}</b>\n\n"
                  f"Trades: {len(trade_log)} | ✅ {len(wins)} wins | ❌ {len(losses)} losses\n"
                  f"💰 Net P&L: <b>₹{daily_pnl:+.0f}</b>\n\n{log_text}\n\n🤖 AutoTrender NXT")

def reset_daily():
    global daily_pnl
    for idx in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        price_history[idx].clear(); vwap_data[idx].clear()
        last_signal[idx] = None; trades_today[idx] = 0
    trade_log.clear(); daily_pnl = 0.0

def is_between(h1, m1, h2, m2):
    from datetime import time as dtime
    now = datetime.now(IST).time()
    return dtime(h1, m1) <= now <= dtime(h2, m2)

def main():
    send_telegram("🤖 <b>AutoTrender NXT STARTED</b>\n\n"
                  "✅ Auto token refresh 9:00 AM\n✅ Auto signal scan every 3 min\n"
                  "✅ Auto orders via Upstox\n✅ Auto SL & target\n"
                  "✅ Auto square-off 3:25 PM\n✅ Daily P&L summary 3:35 PM\n\n"
                  "Watching: NIFTY | BANKNIFTY | SENSEX\nRunning 24/7 on cloud ☁️")
    last_token_date = None
    scan_count = 0
    while True:
        now   = datetime.now(IST)
        today = now.date()
        if now.hour == 9 and now.minute == 0 and last_token_date != today:
            refresh_token(); reset_daily(); last_token_date = today
        if now.hour == 15 and now.minute == 25:
            square_off_all()
        if now.hour == 15 and now.minute == 35 and last_token_date == today:
            send_daily_summary(); last_token_date = None
        if not is_between(9, 15, 15, 25):
            time.sleep(30); continue
        no_trade = not is_between(9, 45, 15, 20)
        scan_count += 1
        print(f"\n[SCAN #{scan_count}] {now.strftime('%H:%M:%S')}")
        oc_data_all = {}
        for index in ["NIFTY", "BANKNIFTY", "SENSEX"]:
            try:
                oc = get_option_chain(index)
                if not oc: continue
                oc_data_all[index] = oc
                sig = get_signal(index, oc)
                print(f"  {index}: {sig['direction']} ({sig['strength']}/3) ₹{sig['price']:,.0f} RSI={sig['rsi']} PCR={sig['pcr']}")
                if no_trade: time.sleep(2); continue
                if index in open_positions:
                    check_open_positions({index: oc})
                elif sig["strength"] == 3 and last_signal[index] != sig["direction"]:
                    enter_trade(sig, oc)
                    last_signal[index] = sig["direction"]
                time.sleep(2)
            except Exception as e:
                print(f"  [ERROR] {index}: {e}")
        if open_positions and oc_data_all:
            check_open_positions(oc_data_all)
        print(f"  P&L: ₹{daily_pnl:+.0f} | Open: {list(open_positions.keys()) or 'None'}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
