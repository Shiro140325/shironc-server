import os
import uuid
import requests as _requests
import time
from itertools import zip_longest
from urllib.parse import quote

print("SERVER RUNNING FROM:", os.getcwd())
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras

app = Flask(__name__)

NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL")


def get_conn():
    return psycopg2.connect(NEON_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def db_execute(query, params=None, fetch=None, retries=2):
    """
    fetch: None (no return), "one" (fetchone), "all" (fetchall)
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params or ())
                    if fetch == "one":
                        result = cur.fetchone()
                    elif fetch == "all":
                        result = cur.fetchall()
                    else:
                        result = None
                    conn.commit()
                    return result
        except psycopg2.OperationalError as e:
            last_err = e
            if attempt == retries:
                raise
            time.sleep(0.2 * (attempt + 1))
            continue
    raise last_err


MIN_VERSION = (1, 16, 1)  # fallback default if app_config has no min_version row yet

_last_version_check = 0
_VERSION_CHECK_INTERVAL = 300  # 5 min throttle


def _parse_version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0, 0, 0)


def get_license(key):
    return db_execute("SELECT * FROM licenses WHERE key = %s", (key,), fetch="one")


def get_license_by_uid(uid):
    return db_execute("SELECT * FROM licenses WHERE unique_identifier = %s", (uid,), fetch="one")


def update_license(key, activated_at, device_id):
    db_execute(
        "UPDATE licenses SET activated_at = %s, device_id = %s WHERE key = %s",
        (activated_at, device_id, key)
    )


def get_min_version():
    row = db_execute("SELECT value FROM app_config WHERE key = %s", ("min_version",), fetch="one")
    return _parse_version(row["value"]) if row else MIN_VERSION


def _maybe_bump_min_version():
    global _last_version_check
    now = time.time()
    if now - _last_version_check < _VERSION_CHECK_INTERVAL:
        return
    _last_version_check = now

    current_min = get_min_version()
    if current_min >= (1, 18, 1):
        return  # already bumped

    rows = db_execute(
        "SELECT app_version, days, activated_at FROM licenses WHERE activated_at IS NOT NULL",
        fetch="all"
    )

    now = time.time()
    versions = []
    for r in rows:
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
        db_execute(
            """
            INSERT INTO app_config (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            ("min_version", "1.18.1")
        )


LATEST_VERSION = "1.18.3"
LATEST_OBJECT_KEY = "Shiro NC 1.18.3.zip"
R2_PUBLIC_BASE_URL = "https://pub-2ab9e11b01a74a098d73ef9b2169d809.r2.dev"


def _parse_version_list(v: str) -> list:
    v = v.lstrip("vV")
    return [int(x) if x.isdigit() else 0 for x in v.split(".")]


def _is_newer(a: str, b: str) -> bool:
    av, bv = _parse_version_list(a), _parse_version_list(b)
    for ai, bi in zip_longest(av, bv, fillvalue=0):
        if ai > bi:
            return True
        if ai < bi:
            return False
    return False


@app.route("/check-update", methods=["POST"])
def check_update():
    data = request.json or {}
    current_version = data.get("current_version", "0.0.0")

    if not _is_newer(LATEST_VERSION, current_version):
        return jsonify({
            "ok": True,
            "update_available": False,
            "latest_version": LATEST_VERSION
        })

    download_url = f"{R2_PUBLIC_BASE_URL}/{quote(LATEST_OBJECT_KEY)}"

    return jsonify({
        "ok": True,
        "update_available": True,
        "latest_version": LATEST_VERSION,
        "file_name": LATEST_OBJECT_KEY,
        "download_url": download_url
    })


@app.route("/broadcast", methods=["GET"])
def broadcast():
    try:
        row = db_execute("SELECT value FROM app_config WHERE key = %s", ("broadcast_message",), fetch="one")
        msg = row["value"] if row else ""
        return jsonify({"message": msg}), 200
    except Exception:
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
        db_execute(
            f"UPDATE licenses SET app_version = %s WHERE {'unique_identifier' if uid else 'key'} = %s",
            (version_str, uid if uid else key)
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
    db_execute(
        "UPDATE licenses SET unique_identifier = %s, device_id = %s, activated_at = %s WHERE key = %s",
        (uid, device, lic["activated_at"] or int(time.time()), key)
    )

    return jsonify({"unique_identifier": uid}), 200


@app.route("/add", methods=["POST"])
def add_license():
    if request.headers.get("x-admin") != "your-secret":
        return jsonify({"error": "unauthorized"}), 403

    data = request.json
    key = data.get("key")
    days = data.get("days", 90)

    db_execute(
        "INSERT INTO licenses (key, days, activated_at, device_id) VALUES (%s, %s, NULL, NULL)",
        (key, days)
    )

    return jsonify({"status": "added"})


if __name__ == "__main__":
    app.run()
