from flask import Flask, jsonify
from flask_cors import CORS
import requests
import json
import time

app = Flask(__name__)
CORS(app, origins="*")

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

NSE_SESSION = requests.Session()
NSE_SESSION.headers.update(NSE_HEADERS)
NSE_LAST_COOKIE_TIME = 0

def refresh_nse_cookies():
    global NSE_LAST_COOKIE_TIME
    try:
        NSE_SESSION.get("https://www.nseindia.com", timeout=10)
        NSE_LAST_COOKIE_TIME = time.time()
    except:
        pass

def get_nse_data(url):
    global NSE_LAST_COOKIE_TIME
    if time.time() - NSE_LAST_COOKIE_TIME > 300:
        refresh_nse_cookies()
    try:
        resp = NSE_SESSION.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except:
        pass
    return None

@app.route("/")
def index():
    return jsonify({"status": "AutoTrender Backend Running", "version": "1.0"})

@app.route("/api/indices")
def indices():
    data = get_nse_data("https://www.nseindia.com/api/allIndices")
    if data:
        return jsonify(data)
    return jsonify({"error": "Failed to fetch indices"}), 500

@app.route("/api/option-chain/<symbol>")
def option_chain(symbol):
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    data = get_nse_data(url)
    if data:
        return jsonify(data)
    return jsonify({"error": "Failed to fetch option chain"}), 500

@app.route("/api/fiidii")
def fiidii():
    data = get_nse_data("https://www.nseindia.com/api/fiidiiTradeReact")
    if data:
        return jsonify(data)
    return jsonify({"error": "Failed to fetch FII/DII data"}), 500

@app.route("/api/gainers-losers")
def gainers_losers():
    data = get_nse_data("https://www.nseindia.com/api/live-analysis-variations?index=gainers")
    if data:
        return jsonify(data)
    return jsonify({"error": "Failed to fetch gainers/losers"}), 500

@app.route("/api/most-active")
def most_active():
    data = get_nse_data("https://www.nseindia.com/api/live-analysis-variations?index=mostactive")
    if data:
        return jsonify(data)
    return jsonify({"error": "Failed to fetch most active"}), 500

if __name__ == "__main__":
    import os
    refresh_nse_cookies()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
