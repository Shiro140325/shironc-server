import os
import uuid
import requests as _requests
import httpx
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

MIN_VERSION = (1, 16, 1)  # fallback default if app_config has no min_version row yet

_last_version_check = 0
_VERSION_CHECK_INTERVAL = 300  # 5 min throttle


def supabase_execute(query, retries=2):
    for attempt in range(retries + 1):
        try:
            return query.execute()
        except httpx.TransportError:
            if attempt == retries:
                raise
            time.sleep(0.2 * (attempt + 1))  # small backoff before retry
            continue


def _parse_version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0, 0, 0)


def get_license(key):
    res = supabase_execute(supabase.table("licenses").select("*").eq("key", key))
    if not res.data:
        return None
    return res.data[0]


def get_license_by_uid(uid):
    res = supabase_execute(supabase.table("licenses").select("*").eq("unique_identifier", uid))
    if not res.data:
        return None
    return res.data[0]


def update_license(key, activated_at, device_id):
    supabase_execute(
        supabase.table("licenses").update({
            "activated_at": activated_at,
            "device_id": device_id
        }).eq("key", key)
    )


def get_min_version():
    res = supabase_execute(supabase.table("app_config").select("value").eq("key", "min_version"))
    if res.data:
        return _parse_version(res.data[0]["value"])
    return MIN_VERSION


def _maybe_bump_min_version():
    global _last_version_check
    now = time.time()
    if now - _last_version_check < _VERSION_CHECK_INTERVAL:
        return
    _last_version_check = now

    current_min = get_min_version()
    if current_min >= (1, 18, 1):
        return  # already bumped

    res = supabase_execute(
        supabase.table("licenses").select("app_version, days, activated_at").not_.is_("activated_at", "null")
    )

    now = time.time()
    versions = []
    for r in res.data:
        if not r.get("app_version"):
            continue

        days = r.get("days", 0)
        if days != 0:
            duration_secs = abs(days) * 60 if days < 0 else days * 86400
            if now > r["activated_at"] + duration_secs:
                continue  # expired license — excluded from the rollout check

        versions.append(_parse_version(r["app_version"]))

    if not versions:
        return

    if all(v >= (1, 18, 2) for v in versions):
        supabase_execute(
            supabase.table("app_config").upsert({"key": "min_version", "value": "1.18.1"})
        )


@app.route("/broadcast", methods=["GET"])
def broadcast():
    try:
        res = supabase_execute(supabase.table("app_config").select("value").eq("key", "broadcast_message"))
        msg = res.data[0]["value"] if res.data else ""
        return jsonify({"message": msg}), 200
    except Exception as e:
        return jsonify({"message": ""}), 200


@app.route("/health")
def health():
    min_v = get_min_version()
    return jsonify({"status": "ok", "min_version": f"{min_v[0]}.{min_v[1]}.{min_v[2]}"}), 200


@app.route("/transcribe", methods=["POST"])
def transcribe():
    key = request.headers.get("X-License-Key", "").strip().upper()
    device = request.headers.get("X-Device-ID", "").strip()

    lic = get_license(key)
    if not lic:
        return jsonify({"error": "unauthorized"}), 401
    if lic["device_id"] != device:
        return jsonify({"error": "unauthorized"}), 401

    days = lic["days"]
    if days != 0:
        duration_secs = abs(days) * 60 if days < 0 else days * 86400
        if time.time() > lic["activated_at"] + duration_secs:
            return jsonify({"error": "expired"}), 401

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "no file"}), 400

    try:
        response = _requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            data={k: v for k, v in request.form.items()},
            files={"file": (file.filename, file.stream, file.mimetype)},
            timeout=60,
        )
        return (response.content, response.status_code, {"Content-Type": "application/json"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def home():
    return "OK"


@app.route("/status", methods=["GET"])
def status():
    uid = request.args.get("unique_identifier")
    key = request.args.get("key", "").strip().upper()
    device = request.args.get("device")

    if uid:
        lic = get_license_by_uid(uid)
        if not lic:
            return jsonify({"active": False}), 200
    else:
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
    uid = data.get("unique_identifier")
    key = data.get("key", "").strip().upper()
    device = data.get("device")

    if uid:
        lic = get_license_by_uid(uid)
        if not lic:
            return jsonify({"error": "Invalid license"}), 400
    else:
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
    uid = data.get("unique_identifier")
    key = data.get("key", "").strip().upper()
    device = data.get("device")
    version_str = data.get("version", "0.0.0")
    version = _parse_version(version_str)

    if version < get_min_version():
        return jsonify({"error": "Invalid"}), 400

    if uid:
        lic = get_license_by_uid(uid)
        if not lic:
            return jsonify({"error": "Invalid"}), 400  # superseded by a newer device registration
    else:
        lic = get_license(key)
        if not lic:
            return jsonify({"error": "Invalid"}), 400
        if lic["device_id"] != device:
            return jsonify({"error": "Invalid device"}), 403

    try:
        supabase_execute(
            supabase.table("licenses").update({"app_version": version_str}).eq(
                "unique_identifier" if uid else "key", uid if uid else key
            )
        )
    except Exception:
        pass  # non-critical, don't fail validation over a logging write

    _maybe_bump_min_version()

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


@app.route("/device/register", methods=["POST"])
def device_register():
    data = request.json
    key = data.get("key", "").strip().upper()
    device = data.get("device")

    lic = get_license(key)
    if not lic:
        return jsonify({"error": "Invalid license"}), 400

    # Same device re-asking (reinstall, deleted device.dat, etc.) — return existing UID
    if lic.get("unique_identifier") and lic["device_id"] == device:
        return jsonify({"unique_identifier": lic["unique_identifier"]}), 200

    # New device — issue a fresh UID, overwrite the old one (this is what kicks
    # any previously-registered device on its next /validate call)
    uid = str(uuid.uuid4())
    supabase_execute(
        supabase.table("licenses").update({
            "unique_identifier": uid,
            "device_id": device,
            "activated_at": lic["activated_at"] or int(time.time())
        }).eq("key", key)
    )

    return jsonify({"unique_identifier": uid}), 200


@app.route("/add", methods=["POST"])
def add_license():
    if request.headers.get("x-admin") != "your-secret":
        return jsonify({"error": "unauthorized"}), 403

    data = request.json
    key = data.get("key")
    days = data.get("days", 90)

    supabase_execute(
        supabase.table("licenses").insert({
            "key": key,
            "days": days,
            "activated_at": None,
            "device_id": None
        })
    )

    return jsonify({"status": "added"})


if __name__ == "__main__":
    app.run()
