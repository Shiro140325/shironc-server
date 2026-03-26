import os
print("SERVER RUNNING FROM:", os.getcwd())
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "licenses.db")

from flask import Flask, request, jsonify
import time

app = Flask(__name__)

licenses = {
    "TEST-KEY-1234-ABCD": {
        "days": 90,
        "activated_at": None,
        "device_id": None
    }
}

import sqlite3

def get_license(key):
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT key, days, activated_at, device_id FROM licenses WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "key": row[0],
        "days": row[1],
        "activated_at": row[2],
        "device_id": row[3]
    }


def update_license(key, activated_at, device_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
        UPDATE licenses
        SET 
            activated_at = COALESCE(activated_at, ?),
            device_id = COALESCE(device_id, ?)
        WHERE key=?
    """, (activated_at, device_id, key))

    conn.commit()
    conn.close()

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
    data = request.json
    key = data.get("key")
    days = data.get("days", 90)

    import sqlite3
    conn = sqlite3.connect("licenses.db")
    c = conn.cursor()

    try:
        c.execute(
            "INSERT INTO licenses (key, days, activated_at, device_id) VALUES (?, ?, ?, ?)",
            (key, days, None, None)
        )
    except sqlite3.IntegrityError:
        return jsonify({"error": "Key already exists"}), 400

    conn.close()
    return jsonify({"status": "added"})


if __name__ == "__main__":
    app.run()