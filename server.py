import os
print("SERVER RUNNING FROM:", os.getcwd())
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "licenses.db")

from flask import Flask, request, jsonify
from supabase import create_client
import time

app = Flask(__name__)

import os

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

@app.route("/")
def home():
    return "OK"

@app.route("/activate", methods=["POST"])
def activate():
    data = request.json
    key = data.get("key", "").strip().upper()
    device = data.get("device")

    lic = get_license(key)
    if not lic:
        return jsonify({"error": "Invalid license"}), 400

    # first activation only
    if lic["activated_at"] is None:
        activated = int(time.time())
        update_license(key, activated, device)
        lic = get_license(key)

    # prevent reuse on another device
    if lic["device_id"] and lic["device_id"] != device:
        return jsonify({"error": "Used on another device"}), 403

    # expiry check
    if lic["days"] != 0:
        if time.time() > lic["activated_at"] + lic["days"] * 86400:
            return jsonify({"error": "Expired"}), 403

    if lic["activated_at"] is not None:
        print("ALREADY ACTIVATED AT:", lic["activated_at"])

    print("BEFORE:", lic)

    if lic["activated_at"] is None:
        activated = int(time.time())
        print("SETTING ACTIVATED AT:", activated)
        update_license(key, activated, device)
        lic = get_license(key)

    print("AFTER:", lic)

    return jsonify({
        "expires_at": lic["activated_at"] + lic["days"] * 86400 if lic["days"] != 0 else None
    })


@app.route("/validate", methods=["POST"])
def validate():
    data = request.json
    key = data.get("key", "").strip().upper()
    device = data.get("device")

    print("VALIDATE KEY:", repr(key))
    print("DEVICE:", device)

    lic = get_license(key)
    if not lic:
        return jsonify({"error": "Invalid"}), 400

    if lic["device_id"] != device:
        return jsonify({"error": "Invalid device"}), 403

    if lic["days"] != 0:
        if time.time() > lic["activated_at"] + lic["days"] * 86400:
            return jsonify({"error": "Expired"}), 403

    return jsonify({"status": "ok"})

@app.route("/add", methods=["POST"])
def add_license():

    # 🔐 protect this endpoint
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
