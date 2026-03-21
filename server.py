from flask import Flask, jsonify, request, redirect
from flask_cors import CORS
import requests
import os
import time

app = Flask(__name__)
CORS(app, origins="*")

API_KEY = os.environ.get("UPSTOX_API_KEY", "")
SECRET = os.environ.get("UPSTOX_SECRET", "")
REDIRECT_URI = os.environ.get("UPSTOX_REDIRECT_URI", "https://reddydamodar415-debug.github.io/autotrender")

# In-memory token store
TOKEN_STORE = {"access_token": None, "expires_at": 0}

UPSTOX_SYMBOLS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
    "MIDCAP": "NSE_INDEX|Nifty Midcap 50"
}

def get_headers():
    return {
        "Authorization": f"Bearer {TOKEN_STORE['access_token']}",
        "Accept": "application/json"
    }

def is_token_valid():
    return TOKEN_STORE["access_token"] and time.time() < TOKEN_STORE["expires_at"]

@app.route("/")
def index():
    return jsonify({"status": "AutoTrender Backend Running", "version": "2.0", "upstox": is_token_valid()})

@app.route("/api/auth/url")
def auth_url():
    url = (
        f"https://api.upstox.com/v2/login/authorization/dialog"
        f"?client_id={API_KEY}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
    )
    return jsonify({"url": url})

@app.route("/api/auth/token", methods=["POST"])
def exchange_token():
    data = request.get_json()
    code = data.get("code")
    if not code:
        return jsonify({"error": "No code provided"}), 400
    try:
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "client_id": API_KEY,
                "client_secret": SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code"
            },
            timeout=15
        )
        result = resp.json()
        if "access_token" in result:
            TOKEN_STORE["access_token"] = result["access_token"]
            TOKEN_STORE["expires_at"] = time.time() + 86400
            return jsonify({"success": True, "message": "Token saved"})
        return jsonify({"error": result}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/auth/save", methods=["POST"])
def save_token():
    data = request.get_json()
    token = data.get("token")
    if not token:
        return jsonify({"error": "No token"}), 400
    TOKEN_STORE["access_token"] = token
    TOKEN_STORE["expires_at"] = time.time() + 86400
    return jsonify({"success": True})

@app.route("/api/auth/status")
def auth_status():
    return jsonify({"authenticated": is_token_valid()})

@app.route("/api/indices")
def indices():
    if not is_token_valid():
        return jsonify({"error": "Not authenticated", "auth_required": True}), 401
    try:
        symbols = ",".join(UPSTOX_SYMBOLS.values())
        resp = requests.get(
            f"https://api.upstox.com/v2/market-quote/quotes?symbol={symbols}",
            headers=get_headers(),
            timeout=15
        )
        data = resp.json()
        if data.get("status") == "success":
            result = []
            for name, sym in UPSTOX_SYMBOLS.items():
                q = data["data"].get(sym.replace("|", ":"), {})
                if q:
                    result.append({
                        "index": name,
                        "last": q.get("last_price", 0),
                        "percentChange": round(((q.get("last_price",0) - q.get("ohlc",{}).get("close",1)) / max(q.get("ohlc",{}).get("close",1),1)) * 100, 2),
                        "previousClose": q.get("ohlc", {}).get("close", 0)
                    })
            return jsonify({"data": result})
        return jsonify({"error": "Upstox error", "detail": data}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/option-chain/<symbol>")
def option_chain(symbol):
    if not is_token_valid():
        return jsonify({"error": "Not authenticated", "auth_required": True}), 401
    try:
        upstox_sym = UPSTOX_SYMBOLS.get(symbol.upper(), UPSTOX_SYMBOLS["NIFTY"])
        resp = requests.get(
            f"https://api.upstox.com/v2/option/chain?instrument_key={upstox_sym.replace('|',':')}&expiry_date=",
            headers=get_headers(),
            timeout=15
        )
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/market-quote")
def market_quote():
    if not is_token_valid():
        return jsonify({"error": "Not authenticated", "auth_required": True}), 401
    symbol = request.args.get("symbol", "NSE_INDEX:Nifty 50")
    try:
        resp = requests.get(
            f"https://api.upstox.com/v2/market-quote/quotes?symbol={symbol}",
            headers=get_headers(),
            timeout=15
        )
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/ltp")
def ltp():
    if not is_token_valid():
        return jsonify({"error": "Not authenticated", "auth_required": True}), 401
    symbols = request.args.get("symbols", "NSE_INDEX:Nifty 50,NSE_INDEX:Nifty Bank")
    try:
        resp = requests.get(
            f"https://api.upstox.com/v2/market-quote/ltp?symbol={symbols}",
            headers=get_headers(),
            timeout=15
        )
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
