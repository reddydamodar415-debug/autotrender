from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, os, time, threading, hmac, hashlib, struct, base64
from datetime import datetime, date
import pytz

app = Flask(__name__)
CORS(app, origins="*")

# ── ENV VARS ──────────────────────────────────────────────────────────────────
API_KEY       = os.environ.get("UPSTOX_API_KEY", "")
SECRET        = os.environ.get("UPSTOX_SECRET", "")
REDIRECT_URI  = os.environ.get("UPSTOX_REDIRECT_URI", "https://reddydamodar415-debug.github.io/autotrender")
MOBILE        = os.environ.get("UPSTOX_MOBILE", "")
PIN           = os.environ.get("UPSTOX_PIN", "")
TOTP_SECRET   = os.environ.get("UPSTOX_TOTP_SECRET", "")
TG_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.environ.get("TELEGRAM_CHAT_ID", "")
IST           = pytz.timezone("Asia/Kolkata")

# ── TOKEN STORE ───────────────────────────────────────────────────────────────
TOKEN_STORE = {"access_token": None, "expires_at": 0}

UPSTOX_SYMBOLS = {
    "NIFTY":     "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX":    "BSE_INDEX|SENSEX",
    "MIDCAP":    "NSE_INDEX|Nifty Midcap 50"
}

# ── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

# ── TOTP ──────────────────────────────────────────────────────────────────────
def generate_totp(secret):
    try:
        pad = secret.upper() + "=" * (8 - len(secret) % 8 if len(secret) % 8 else 0)
        key = base64.b32decode(pad)
        counter = int(time.time()) // 30
        msg_bytes = struct.pack(">Q", counter)
        h = hmac.new(key, msg_bytes, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = (struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF) % 1000000
        return str(code).zfill(6)
    except Exception as e:
        print(f"[TOTP ERROR] {e}")
        return ""

# ── AUTO LOGIN ────────────────────────────────────────────────────────────────
def auto_refresh_token():
    print("[AUTO-TOKEN] Starting token refresh...")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        auth_url = (
            f"https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}"
        )
        session.get(auth_url, timeout=10)
        session.post(
            "https://api.upstox.com/v2/login/authorization/dialog",
            json={"mobileNum": MOBILE, "source": "WEB"}, timeout=10
        )
        session.post(
            "https://api.upstox.com/v2/login/authorization/pin-verification",
            json={"pin": PIN, "source": "WEB"}, timeout=10
        )
        totp = generate_totp(TOTP_SECRET)
        r = session.post(
            "https://api.upstox.com/v2/login/authorization/totp-verification",
            json={"otp": totp, "source": "WEB"},
            allow_redirects=False, timeout=10
        )
        location = r.headers.get("Location", "")
        code = ""
        if "code=" in location:
            code = location.split("code=")[1].split("&")[0]
        elif r.status_code == 200:
            code = r.json().get("code", "")
        if not code:
            raise Exception("No auth code received")
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            data={
                "code": code, "client_id": API_KEY, "client_secret": SECRET,
                "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"
            }, timeout=15
        )
        result = resp.json()
        if "access_token" in result:
            TOKEN_STORE["access_token"] = result["access_token"]
            TOKEN_STORE["expires_at"] = time.time() + 86400
            print("[AUTO-TOKEN] Token refreshed successfully!")
            send_telegram("✅ <b>AutoTrender NXT</b>\n\nUpstox token auto-refreshed 🔑\nLive NSE data is now active!")
            return True
        else:
            raise Exception(f"Token exchange failed: {result}")
    except Exception as e:
        print(f"[AUTO-TOKEN ERROR] {e}")
        send_telegram(f"❌ <b>AutoTrender NXT</b>\n\nToken refresh FAILED\n<code>{e}</code>")
        return False

# ── HELPERS ───────────────────────────────────────────────────────────────────
def get_headers():
    return {"Authorization": f"Bearer {TOKEN_STORE['access_token']}", "Accept": "application/json"}

def is_token_valid():
    return bool(TOKEN_STORE["access_token"] and time.time() < TOKEN_STORE["expires_at"])

def get_nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/"
    })
    try:
        s.get("https://www.nseindia.com", timeout=10)
        s.get("https://www.nseindia.com/option-chain", timeout=10)
    except:
        pass
    return s

nse_session = get_nse_session()

# ── PCR & SPOT ────────────────────────────────────────────────────────────────
def get_pcr_and_spot(symbol):
    global nse_session
    try:
        r = nse_session.get(
            f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}",
            timeout=10
        )
        if r.status_code == 401:
            nse_session = get_nse_session()
            r = nse_session.get(
                f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}",
                timeout=10
            )
        oc = r.json()
        records = oc.get("filtered", {}).get("data", [])
        ce_oi = sum(x["CE"]["openInterest"] for x in records if "CE" in x)
        pe_oi = sum(x["PE"]["openInterest"] for x in records if "PE" in x)
        pcr = round(pe_oi / ce_oi, 2) if ce_oi > 0 else 1.0
        spot = float(oc["records"]["underlyingValue"])
        return pcr, spot
    except Exception as e:
        print(f"[PCR ERROR {symbol}] {e}")
        return 1.0, 0.0

# ── TRADING BOT STATE ─────────────────────────────────────────────────────────
bot_state = {
    "price_history":  {"NIFTY": [], "BANKNIFTY": [], "SENSEX": []},
    "vwap_data":      {"NIFTY": [], "BANKNIFTY": [], "SENSEX": []},
    "last_signal":    {"NIFTY": None, "BANKNIFTY": None, "SENSEX": None},
    "open_positions": {},
    "daily_pnl":      0.0,
    "trades_today":   {"NIFTY": 0, "BANKNIFTY": 0, "SENSEX": 0},
    "trade_log":      [],
    "scan_count":     0,
}

MAX_TRADES = 3
LOT_SIZE   = {"NIFTY": 25, "BANKNIFTY": 15, "SENSEX": 10}
LOTS       = 1
TARGET_PTS = 50
SL_PTS     = 30
MAX_LOSS   = 5000
SCAN_SECS  = 180

# ── INDICATORS ────────────────────────────────────────────────────────────────
def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    ag = sum(gains) / period if gains else 0
    al = sum(losses) / period if losses else 0.001
    return round(100 - 100 / (1 + ag / al), 1)

def update_vwap(sym, price):
    bot_state["vwap_data"][sym].append(price)
    data = bot_state["vwap_data"][sym]
    return round(sum(data) / len(data), 2)

# ── FUTURES KEY ───────────────────────────────────────────────────────────────
def futures_key(sym):
    month = datetime.now(IST).strftime("%y%b").upper()
    if sym == "SENSEX":
        return f"BSE_FO|SENSEX{month}FUT"
    elif sym == "BANKNIFTY":
        return f"NSE_FO|BANKNIFTY{month}FUT"
    return f"NSE_FO|NIFTY{month}FUT"

# ── PLACE ORDER ───────────────────────────────────────────────────────────────
def place_order(sym, txn, qty):
    payload = {
        "quantity": qty * LOT_SIZE[sym], "product": "D", "validity": "DAY",
        "tag": "AutoTrenderNXT", "instrument_token": futures_key(sym),
        "price": 0, "order_type": "MARKET", "transaction_type": txn,
        "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False
    }
    try:
        r = requests.post(
            "https://api.upstox.com/v2/order/place",
            headers=get_headers(), json=payload, timeout=15
        )
        return r.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ── SIGNAL ────────────────────────────────────────────────────────────────────
def get_signal(sym):
    pcr, spot = get_pcr_and_spot(sym)
    if spot == 0:
        return None
    hist = bot_state["price_history"][sym]
    hist.append(spot)
    if len(hist) > 50:
        hist.pop(0)
    rsi  = calc_rsi(hist)
    vwap = update_vwap(sym, spot)
    ls = ss = 0
    reasons = []
    if rsi < 30:         ls += 1; reasons.append(f"RSI={rsi}(oversold→BUY)")
    elif rsi > 70:       ss += 1; reasons.append(f"RSI={rsi}(overbought→SELL)")
    if spot > vwap*1.001:  ls += 1; reasons.append("Above VWAP→BUY")
    elif spot < vwap*0.999: ss += 1; reasons.append("Below VWAP→SELL")
    if pcr > 1.3:        ls += 1; reasons.append(f"PCR={pcr}(bullish→BUY)")
    elif pcr < 0.8:      ss += 1; reasons.append(f"PCR={pcr}(bearish→SELL)")
    direction = "NEUTRAL"
    strength  = 0
    if ls == 3:   direction, strength = "BUY", 3
    elif ss == 3: direction, strength = "SELL", 3
    return {"sym": sym, "direction": direction, "strength": strength,
            "price": spot, "rsi": rsi, "pcr": pcr, "vwap": vwap, "reasons": reasons}

# ── ENTER TRADE ───────────────────────────────────────────────────────────────
def enter_trade(sig):
    sym  = sig["sym"]
    dire = sig["direction"]
    if bot_state["trades_today"][sym] >= MAX_TRADES:
        return
    if bot_state["daily_pnl"] <= -MAX_LOSS:
        send_telegram(f"🛑 <b>DAILY LOSS LIMIT HIT</b>\nNo more trades today. Loss: Rs.{abs(bot_state['daily_pnl']):.0f}")
        return
    txn    = "BUY" if dire == "BUY" else "SELL"
    result = place_order(sym, txn, LOTS)
    if result.get("status") == "success":
        ep  = sig["price"]
        qty = LOTS * LOT_SIZE[sym]
        tgt = round(ep + TARGET_PTS, 2) if dire == "BUY" else round(ep - TARGET_PTS, 2)
        sl  = round(ep - SL_PTS, 2)     if dire == "BUY" else round(ep + SL_PTS, 2)
        bot_state["open_positions"][sym] = {
            "direction": dire, "entry_price": ep, "target": tgt,
            "sl": sl, "qty": qty, "entry_time": datetime.now(IST).strftime("%H:%M:%S")
        }
        bot_state["trades_today"][sym] += 1
        icon = "🟢" if dire == "BUY" else "🔴"
        send_telegram(
            f"{icon} <b>FUTURES ORDER -- {sym}</b>\n\n"
            f"{'BUY' if dire=='BUY' else 'SELL'} {sym} FUTURES\n"
            f"Entry: Rs.{ep:,.0f}\n"
            f"Target: Rs.{tgt:,.0f} (+{TARGET_PTS} pts)\n"
            f"SL: Rs.{sl:,.0f} (-{SL_PTS} pts)\n"
            f"Qty: {qty} ({LOTS} lot)\n"
            f"Signals: {' | '.join(sig['reasons'])}\n"
            f"Time: {datetime.now(IST).strftime('%H:%M:%S')} IST"
        )
    else:
        send_telegram(f"❌ <b>ORDER FAILED -- {sym}</b>\n{result.get('message','unknown error')}")

# ── CHECK POSITIONS ───────────────────────────────────────────────────────────
def check_positions():
    for sym, pos in list(bot_state["open_positions"].items()):
        try:
            _, price = get_pcr_and_spot(sym)
            if price <= 0:
                continue
            dire = pos["direction"]
            pnl  = (price - pos["entry_price"]) * pos["qty"] if dire == "BUY" else (pos["entry_price"] - price) * pos["qty"]
            exit_reason = None
            if dire == "BUY":
                if price >= pos["target"]: exit_reason = f"TARGET HIT (+Rs.{pnl:.0f})"
                elif price <= pos["sl"]:   exit_reason = f"STOP LOSS (-Rs.{abs(pnl):.0f})"
            else:
                if price <= pos["target"]: exit_reason = f"TARGET HIT (+Rs.{pnl:.0f})"
                elif price >= pos["sl"]:   exit_reason = f"STOP LOSS (-Rs.{abs(pnl):.0f})"
            if exit_reason:
                close  = "SELL" if dire == "BUY" else "BUY"
                result = place_order(sym, close, LOTS)
                if result.get("status") == "success":
                    bot_state["daily_pnl"] += pnl
                    bot_state["trade_log"].append({
                        "sym": sym, "direction": dire,
                        "entry": pos["entry_price"], "exit": price, "pnl": round(pnl, 2)
                    })
                    icon = "🟢" if pnl > 0 else "🔴"
                    send_telegram(
                        f"{icon} <b>CLOSED -- {sym} FUTURES</b>\n\n"
                        f"{exit_reason}\n"
                        f"Entry: Rs.{pos['entry_price']:,.0f} -> Exit: Rs.{price:,.0f}\n"
                        f"P&L: Rs.{pnl:+.0f}\n"
                        f"Daily P&L: Rs.{bot_state['daily_pnl']:+.0f}\n"
                        f"Time: {datetime.now(IST).strftime('%H:%M:%S')} IST"
                    )
                    del bot_state["open_positions"][sym]
                    bot_state["last_signal"][sym] = None
        except Exception as e:
            print(f"[EXIT ERROR {sym}] {e}")

# ── SQUARE OFF ────────────────────────────────────────────────────────────────
def square_off_all():
    if not bot_state["open_positions"]:
        return
    send_telegram("Market closing -- squaring off all positions")
    for sym, pos in list(bot_state["open_positions"].items()):
        close = "SELL" if pos["direction"] == "BUY" else "BUY"
        place_order(sym, close, LOTS)
        del bot_state["open_positions"][sym]

# ── DAILY SUMMARY ─────────────────────────────────────────────────────────────
def send_daily_summary():
    log = bot_state["trade_log"]
    pnl = bot_state["daily_pnl"]
    dt  = date.today().strftime("%d %b %Y")
    if not log:
        send_telegram(f"Daily Summary -- {dt}\n\nNo trades today (no 3/3 signals).")
        return
    wins  = [t for t in log if t["pnl"] > 0]
    loss  = [t for t in log if t["pnl"] <= 0]
    lines = "\n".join(f"* {t['sym']} {t['direction']}: Rs.{t['pnl']:+.0f}" for t in log)
    send_telegram(
        f"Daily Summary -- {dt}\n\n"
        f"Trades: {len(log)} | Wins: {len(wins)} | Losses: {len(loss)}\n"
        f"Net P&L: Rs.{pnl:+.0f}\n\n{lines}"
    )

# ── TIME HELPERS ──────────────────────────────────────────────────────────────
def is_market_hours():
    from datetime import time as dtime
    now = datetime.now(IST).time()
    return dtime(9, 15) <= now <= dtime(15, 25)

def is_between(h1, m1, h2, m2):
    from datetime import time as dtime
    now = datetime.now(IST).time()
    return dtime(h1, m1) <= now <= dtime(h2, m2)

# ── BACKGROUND BOT LOOP ───────────────────────────────────────────────────────
def bot_loop():
    print("[BOT] Background trading bot started")
    send_telegram(
        "AutoTrender NXT STARTED\n\n"
        "Web server running on Railway\n"
        "Auto token refresh at 9:00 AM IST\n"
        "Signal scan every 3 min\n"
        "Telegram alerts active\n"
        "Auto square-off 3:25 PM IST"
    )
    last_token_date = None
    while True:
        try:
            now   = datetime.now(IST)
            today = now.date()
            # Token refresh at 9:00 AM
            if now.hour == 9 and now.minute == 0 and last_token_date != today:
                auto_refresh_token()
                for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
                    bot_state["price_history"][sym].clear()
                    bot_state["vwap_data"][sym].clear()
                    bot_state["last_signal"][sym] = None
                    bot_state["trades_today"][sym] = 0
                bot_state["trade_log"].clear()
                bot_state["daily_pnl"] = 0.0
                last_token_date = today
            # Square off at 3:25 PM
            if now.hour == 15 and now.minute == 25:
                square_off_all()
            # Daily summary at 3:35 PM
            if now.hour == 15 and now.minute == 35 and last_token_date == today:
                send_daily_summary()
                last_token_date = None
            # Sleep outside market hours
            if not is_market_hours():
                time.sleep(30)
                continue
            no_trade_zone = not is_between(9, 45, 15, 20)
            bot_state["scan_count"] += 1
            print(f"[BOT] Scan #{bot_state['scan_count']} {now.strftime('%H:%M:%S')}")
            if is_token_valid():
                for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
                    try:
                        sig = get_signal(sym)
                        if not sig:
                            continue
                        print(f"  {sym}: {sig['direction']} ({sig['strength']}/3) RSI={sig['rsi']} PCR={sig['pcr']}")
                        time.sleep(2)
                        if sym in bot_state["open_positions"]:
                            check_positions()
                        elif (sig["strength"] == 3 and not no_trade_zone
                              and bot_state["last_signal"][sym] != sig["direction"]):
                            enter_trade(sig)
                            bot_state["last_signal"][sym] = sig["direction"]
                    except Exception as e:
                        print(f"  [ERROR {sym}] {e}")
                if bot_state["open_positions"]:
                    check_positions()
            else:
                print("[BOT] Token not valid -- skipping scan")
        except Exception as e:
            print(f"[BOT LOOP ERROR] {e}")
        time.sleep(SCAN_SECS)

# ── FLASK ROUTES ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return jsonify({
        "status": "AutoTrender Backend Running",
        "version": "3.0",
        "upstox": is_token_valid(),
        "scan_count": bot_state["scan_count"],
        "daily_pnl": bot_state["daily_pnl"],
        "open_positions": list(bot_state["open_positions"].keys())
    })

@app.route("/api/auth/url")
def auth_url():
    url = (f"https://api.upstox.com/v2/login/authorization/dialog"
           f"?client_id={API_KEY}&redirect_uri={REDIRECT_URI}&response_type=code")
    return jsonify({"url": url})

@app.route("/api/auth/token", methods=["POST"])
def exchange_token():
    code = (request.get_json() or {}).get("code")
    if not code:
        return jsonify({"error": "No code"}), 400
    try:
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "client_id": API_KEY, "client_secret": SECRET,
                  "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"},
            timeout=15
        )
        result = resp.json()
        if "access_token" in result:
            TOKEN_STORE["access_token"] = result["access_token"]
            TOKEN_STORE["expires_at"]   = time.time() + 86400
            send_telegram("Upstox token saved via OAuth! Live data active.")
            return jsonify({"success": True})
        return jsonify({"error": result}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/save", methods=["POST"])
def save_token():
    token = (request.get_json() or {}).get("token")
    if not token:
        return jsonify({"error": "No token"}), 400
    TOKEN_STORE["access_token"] = token
    TOKEN_STORE["expires_at"]   = time.time() + 86400
    send_telegram("Upstox token saved manually! Live data active.")
    return jsonify({"success": True})

@app.route("/api/auth/refresh", methods=["POST"])
def manual_refresh():
    ok = auto_refresh_token()
    return jsonify({"success": ok, "authenticated": is_token_valid()})

@app.route("/api/auth/status")
def auth_status():
    return jsonify({"authenticated": is_token_valid()})

@app.route("/api/indices")
def get_indices():
    if not is_token_valid():
        return jsonify({"error": "Not authenticated", "auth_required": True}), 401
    try:
        symbols = ",".join(UPSTOX_SYMBOLS.values())
        resp = requests.get(
            f"https://api.upstox.com/v2/market-quote/quotes?symbol={symbols}",
            headers=get_headers(), timeout=15
        )
        data = resp.json()
        if data.get("status") == "success":
            result = {}
            for name, sym in UPSTOX_SYMBOLS.items():
                q = data["data"].get(sym.replace("|", ":"), {})
                if q:
                    prev = q.get("ohlc", {}).get("close", 1)
                    result[name] = {
                        "price":  q.get("last_price", 0),
                        "change": round(q.get("last_price", 0) - prev, 2),
                        "pct":    round((q.get("last_price", 0) - prev) / max(prev, 1) * 100, 2)
                    }
            return jsonify(result)
        return jsonify({"error": "Upstox error", "detail": data}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pcr")
def get_pcr():
    result = {}
    for sym in ["NIFTY", "BANKNIFTY", "SENSEX"]:
        pcr, spot = get_pcr_and_spot(sym)
        result[sym] = {"pcr": pcr, "spot": spot}
    return jsonify(result)

@app.route("/api/option-chain/<symbol>")
def option_chain(symbol):
    global nse_session
    sym = symbol.upper()
    try:
        r = nse_session.get(
            f"https://www.nseindia.com/api/option-chain-indices?symbol={sym}", timeout=10
        )
        if r.status_code == 401:
            nse_session = get_nse_session()
            r = nse_session.get(
                f"https://www.nseindia.com/api/option-chain-indices?symbol={sym}", timeout=10
            )
        oc   = r.json()
        spot = float(oc["records"]["underlyingValue"])
        step = 50 if sym == "NIFTY" else 200 if sym == "BANKNIFTY" else 500
        atm  = round(spot / step) * step
        rows = []
        ce_oi = pe_oi = 0
        for rec in oc.get("filtered", {}).get("data", []):
            strike = rec.get("strikePrice", 0)
            ce = rec.get("CE", {}); pe = rec.get("PE", {})
            ce_oi += ce.get("openInterest", 0)
            pe_oi += pe.get("openInterest", 0)
            rows.append({
                "strike":  strike, "isATM": strike == atm,
                "ceOI":    ce.get("openInterest", 0),
                "peOI":    pe.get("openInterest", 0),
                "ceChgOI": ce.get("changeinOpenInterest", 0),
                "peChgOI": pe.get("changeinOpenInterest", 0),
                "ceLTP":   ce.get("lastPrice", 0),
                "peLTP":   pe.get("lastPrice", 0),
                "ceIV":    round(ce.get("impliedVolatility", 0), 1),
                "peIV":    round(pe.get("impliedVolatility", 0), 1),
                "pcr":     round(pe.get("openInterest", 0) / max(ce.get("openInterest", 1), 1), 2),
            })
        return jsonify({"rows": rows, "pcr": round(pe_oi / max(ce_oi, 1), 2), "atm": atm, "spot": spot})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/fiidii")
def fiidii():
    try:
        r = nse_session.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=10)
        return jsonify({"data": r.json()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/intraday-oi/<symbol>")
def intraday_oi(symbol):
    pcr, spot = get_pcr_and_spot(symbol.upper())
    return jsonify({"symbol": symbol, "pcr": pcr, "spot": spot, "history": []})

@app.route("/api/sectors")
def sectors():
    return jsonify({"leading": ["IT", "AUTO", "PHARMA"], "trailing": ["REALTY", "MEDIA"]})

@app.route("/api/upstox/quotes")
def upstox_quotes():
    token   = request.args.get("token", TOKEN_STORE.get("access_token") or "")
    symbols = request.args.get("symbols", "NSE_INDEX:Nifty 50,NSE_INDEX:Nifty Bank,BSE_INDEX:SENSEX")
    try:
        r = requests.get(
            f"https://api.upstox.com/v2/market-quote/ltp?symbol={symbols}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=15
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/bot/status")
def bot_status():
    return jsonify({
        "scan_count":    bot_state["scan_count"],
        "daily_pnl":     bot_state["daily_pnl"],
        "trades_today":  bot_state["trades_today"],
        "open_positions": list(bot_state["open_positions"].keys()),
        "authenticated": is_token_valid(),
    })

@app.route("/api/bot/refresh-token", methods=["POST"])
def trigger_refresh():
    t = threading.Thread(target=auto_refresh_token, daemon=True)
    t.start()
    return jsonify({"message": "Token refresh triggered in background"})

# ── STARTUP ───────────────────────────────────────────────────────────────────
def start_background_bot():
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    print("[SERVER] Background bot thread started")

def startup_token():
    time.sleep(5)
    if MOBILE and PIN and TOTP_SECRET:
        print("[STARTUP] Attempting auto token refresh...")
        auto_refresh_token()
    else:
        print("[STARTUP] Missing credentials -- skipping auto token refresh")

if __name__ == "__main__":
    start_background_bot()
    threading.Thread(target=startup_token, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    start_background_bot()
    threading.Thread(target=startup_token, daemon=True).start()
