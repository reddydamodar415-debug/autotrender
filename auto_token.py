import requests, json, os, time, hmac, hashlib, struct, base64
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

UPSTOX_API_KEY    = "ab60d83a-7627-443b-be24-e1953b345195"
UPSTOX_API_SECRET = "lof701h2j0"
REDIRECT_URI      = "https://autotrender.onrender.com/api/auth/callback"

UPSTOX_MOBILE    = os.environ.get("UPSTOX_MOBILE", "")
UPSTOX_PIN       = os.environ.get("UPSTOX_PIN", "")
UPSTOX_TOTP      = os.environ.get("UPSTOX_TOTP_SECRET", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT    = os.environ.get("TELEGRAM_CHAT_ID", "1739813994")
TOKEN_FILE       = "upstox_token.json"

def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except:
        pass

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

def get_auth_code():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    auth_url = (f"https://api.upstox.com/v2/login/authorization/dialog?"
                f"response_type=code&client_id={UPSTOX_API_KEY}&redirect_uri={REDIRECT_URI}")
    try:
        session.get(auth_url, timeout=10)
        session.post("https://api.upstox.com/v2/login/authorization/dialog",
                     json={"mobileNum": UPSTOX_MOBILE, "source": "WEB"}, timeout=10)
        session.post("https://api.upstox.com/v2/login/authorization/pin-verification",
                     json={"pin": UPSTOX_PIN, "source": "WEB"}, timeout=10)
        totp_code = generate_totp(UPSTOX_TOTP)
        r = session.post("https://api.upstox.com/v2/login/authorization/totp-verification",
                         json={"otp": totp_code, "source": "WEB"},
                         allow_redirects=False, timeout=10)
        location = r.headers.get("Location", "")
        if "code=" in location:
            return location.split("code=")[1].split("&")[0]
        if r.status_code == 200:
            return r.json().get("code", "")
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
    return ""

def exchange_code_for_token(auth_code):
    try:
        r = requests.post("https://api.upstox.com/v2/login/authorization/token",
                          data={"code": auth_code, "client_id": UPSTOX_API_KEY,
                                "client_secret": UPSTOX_API_SECRET,
                                "redirect_uri": REDIRECT_URI,
                                "grant_type": "authorization_code"}, timeout=15)
        return r.json().get("access_token", "")
    except Exception as e:
        print(f"[TOKEN ERROR] {e}")
        return ""

def refresh_token():
    print(f"[{datetime.now(IST).strftime('%H:%M:%S')}] Refreshing Upstox token...")
    try:
        auth_code = get_auth_code()
        if not auth_code:
            raise Exception("Failed to get auth code")
        token = exchange_code_for_token(auth_code)
        if not token:
            raise Exception("Failed to get access token")
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": token, "generated_at": datetime.now(IST).isoformat()}, f)
        send_telegram("✅ <b>Upstox token refreshed</b>\nBot ready for trading!")
        return token
    except Exception as e:
        send_telegram(f"❌ <b>Token refresh FAILED</b>\n{e}")
        return ""

def load_token():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f).get("access_token", "")
    except:
        return ""

if __name__ == "__main__":
    refresh_token()
