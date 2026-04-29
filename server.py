import os
print("SERVER RUNNING FROM:", os.getcwd())
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "licenses.db")

from flask import Flask, request, jsonify
from supabase import create_client
import time

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)

licenses = {
    "TEST-KEY-1234-ABCD": {
        "days": 90,
        "activated_at": None,
        "device_id": None
    }
}

MIN_VERSION = (1, 6, 8)

def _parse_version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0, 0, 0)

def get_license(key):
    res = supabase.table("licenses").select("*").eq("key", key).execute()
    if not res.data:
        return None
    return res.data[0]


def update_license(key, activated_at, device_id):
    supabase.table("licenses").update({
        "activated_at": activated_at,
        "device_id": device_id
    }).eq("key", key).execute()

@app.route("/broadcast", methods=["GET"])
def broadcast():
    try:
        res = supabase.table("app_config").select("value").eq("key", "broadcast_message").execute()
        msg = res.data[0]["value"] if res.data else ""
        return jsonify({"message": msg}), 200
    except Exception as e:
        return jsonify({"message": ""}), 200

@app.route("/health")
def health():
    return jsonify({"status": "ok", "min_version": "1.5.5"}), 200

@app.route("/")
def home():
    return "OK"

@app.route("/status", methods=["GET"])
def status():
    key = request.args.get("key", "").strip().upper()
    device = request.args.get("device")

    lic = get_license(key)
    if not lic or lic["device_id"] != device:
        return jsonify({"active": False}), 200

    days = lic["days"]
    if days != 0:
        duration_secs = abs(days) * 60 if days < 0 else days * 86400
        if time.time() > lic["activated_at"] + duration_secs:
            return jsonify({"active": False}), 200

    return jsonify({"active": True}), 200

@app.route("/activate", methods=["POST"])
def activate():
    data = request.json
    key = data.get("key", "").strip().upper()
    device = data.get("device")

    lic = get_license(key)
    if not lic:
        return jsonify({"error": "Invalid license"}), 400

    if lic["activated_at"] is None:
        activated = int(time.time())
        update_license(key, activated, device)
        lic = get_license(key)

    if lic["device_id"] and lic["device_id"] != device:
        return jsonify({"error": "Used on another device"}), 403

    days = lic["days"]

    if days != 0:
        duration_secs = abs(days) * 60 if days < 0 else days * 86400
        if time.time() > lic["activated_at"] + duration_secs:
            return jsonify({"error": "Expired"}), 403

    expires_at = None
    if days != 0:
        duration_secs = abs(days) * 60 if days < 0 else days * 86400
        expires_at = lic["activated_at"] + duration_secs

    return jsonify({"expires_at": expires_at})


@app.route("/validate", methods=["POST"])
def validate():
    data = request.json
    key = data.get("key", "").strip().upper()
    device = data.get("device")
    version = _parse_version(data.get("version", "0.0.0"))

    if version < MIN_VERSION:
        return jsonify({"error": "Invalid"}), 400

    lic = get_license(key)
    if not lic:
        return jsonify({"error": "Invalid"}), 400

    if lic["device_id"] != device:
        return jsonify({"error": "Invalid device"}), 403

    days = lic["days"]
    if days != 0:
        duration_secs = abs(days) * 60 if days < 0 else days * 86400
        if time.time() > lic["activated_at"] + duration_secs:
            return jsonify({"error": "Expired"}), 403

    expires_at = None
    if days != 0:
        duration_secs = abs(days) * 60 if days < 0 else days * 86400
        expires_at = lic["activated_at"] + duration_secs

    return jsonify({"status": "ok", "expires_at": expires_at})

@app.route("/add", methods=["POST"])
def add_license():
    if request.headers.get("x-admin") != "your-secret":
        return jsonify({"error": "unauthorized"}), 403

    data = request.json
    key = data.get("key")
    days = data.get("days", 90)

    supabase.table("licenses").insert({
        "key": key,
        "days": days,
        "activated_at": None,
        "device_id": None
    }).execute()

    return jsonify({"status": "added"})


if __name__ == "__main__":
    app.run()
