import os
import json
import requests as _requests
import time
import traceback
import logging
from itertools import zip_longest
from urllib.parse import quote

print("SERVER RUNNING FROM:", os.getcwd())
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from flask import Flask, request, jsonify
from werkzeug.exceptions import HTTPException
import psycopg2
import psycopg2.extras

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shironc")

from admin import admin_bp, init_admin
init_admin(app)
app.register_blueprint(admin_bp)

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
            logger.error(
                "[DB_EXECUTE] OperationalError on attempt %d/%d for query %r: %s\n%s",
                attempt + 1, retries + 1, query[:80], e, traceback.format_exc()
            )
            if attempt == retries:
                raise
            time.sleep(0.2 * (attempt + 1))
            continue
    raise last_err


# --- Global error handler: log full traceback for any UNHANDLED server error.
# HTTPException (404, 405, etc.) is a subclass of Exception in Flask/Werkzeug,
# so it's explicitly passed through here — otherwise routine 404s (bots
# hitting /robots.txt, /sitemap.xml) get logged as errors and turned into 500s.
@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    if isinstance(e, HTTPException):
        return e
    logger.error(
        "[UNHANDLED] %s %s -> %s\n%s",
        request.method, request.path, e, traceback.format_exc()
    )
    return jsonify({"error": "internal_error", "detail": str(e)}), 500


MIN_VERSION = (1, 16, 1)  # fallback default if app_config has no min_version row yet

# --- min_version read cache (30 min TTL) ---
_min_version_cache = {"value": None, "ts": 0}
_MIN_VERSION_TTL = 1800  # 30 min

# --- broadcast read cache (10 min TTL) ---
_broadcast_cache = {"value": "", "ts": 0}
_BROADCAST_TTL = 600  # 10 min

# --- server-driven polling config (cached like min_version) ---
_poll_config_cache = {"value": None, "ts": 0}
_POLL_CONFIG_TTL = 1800  # 30 min

DEFAULT_POLL_CONFIG = {
    "license_poll_interval": 20,   # seconds between client /validate calls
    "monitor_interval": 30,        # seconds between client /health + /broadcast calls
    "shadow_check_every": 3,       # client runs /status shadow-check every Nth license poll
    "failure_threshold": 3         # consecutive failed heartbeats before client locks down
}


def get_poll_config():
    now = time.time()
    if _poll_config_cache["value"] is None or now - _poll_config_cache["ts"] > _POLL_CONFIG_TTL:
        row = db_execute("SELECT value FROM app_config WHERE key = %s", ("poll_config",), fetch="one")
        if row:
            try:
                _poll_config_cache["value"] = {**DEFAULT_POLL_CONFIG, **json.loads(row["value"])}
            except Exception:
                logger.error("[POLL_CONFIG] failed to parse row value\n%s", traceback.format_exc())
                _poll_config_cache["value"] = DEFAULT_POLL_CONFIG
        else:
            _poll_config_cache["value"] = DEFAULT_POLL_CONFIG
        _poll_config_cache["ts"] = now
    return _poll_config_cache["value"]


def _parse_version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0, 0, 0)


def get_license(key):
    return db_execute("SELECT * FROM licenses WHERE key = %s", (key,), fetch="one")


def update_license(key, activated_at, device_id):
    db_execute(
        "UPDATE licenses SET activated_at = %s, device_id = %s WHERE key = %s",
        (activated_at, device_id, key)
    )


def get_min_version():
    now = time.time()
    if _min_version_cache["value"] is None or now - _min_version_cache["ts"] > _MIN_VERSION_TTL:
        row = db_execute("SELECT value FROM app_config WHERE key = %s", ("min_version",), fetch="one")
        _min_version_cache["value"] = _parse_version(row["value"]) if row else MIN_VERSION
        _min_version_cache["ts"] = now
    return _min_version_cache["value"]


# --- Cache warm-up ---------------------------------------------------------
# Runs once at process start, inside an app context, so /health never has to
# touch Neon itself.
def _warm_caches():
    try:
        get_min_version()
        get_poll_config()
        print("Cache warm-up OK: min_version + poll_config loaded from DB")
    except Exception as e:
        logger.error("[WARM_CACHES] failed, /health will serve defaults: %s\n%s", e, traceback.format_exc())


with app.app_context():
    _warm_caches()
# ---------------------------------------------------------------------------


LATEST_VERSION = "1.18.7"
LATEST_OBJECT_KEY = "Shiro NC 1.18.7.zip"  # ← confirm this matches the exact filename in your R2 bucket

R2_PUBLIC_BASE_URL = "https://updates.shironc.com"

# GITHUB_OWNER = "Shiro140325"
# GITHUB_REPO = "shironc-releases"
# GITHUB_RELEASE_TAG = "v1.18.7"


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
    now = time.time()
    if now - _broadcast_cache["ts"] > _BROADCAST_TTL:
        try:
            row = db_execute("SELECT value FROM app_config WHERE key = %s", ("broadcast_message",), fetch="one")
            _broadcast_cache["value"] = row["value"] if row else ""
        except Exception:
            logger.error("[BROADCAST] DB read failed, serving stale cache\n%s", traceback.format_exc())
        _broadcast_cache["ts"] = now
    return jsonify({"message": _broadcast_cache["value"]}), 200


@app.route("/health")
def health():
    # Pure liveness check — never touches the DB, directly or indirectly.
    min_v = _min_version_cache["value"] or MIN_VERSION
    cfg = _poll_config_cache["value"] or DEFAULT_POLL_CONFIG
    return jsonify({
        "status": "ok",
        "min_version": f"{min_v[0]}.{min_v[1]}.{min_v[2]}",
        "monitor_interval": cfg["monitor_interval"],
        "failure_threshold": cfg["failure_threshold"]
    }), 200


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
        logger.error("[TRANSCRIBE] failed: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": str(e)}), 500


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
    version_str = data.get("version", "0.0.0")
    version = _parse_version(version_str)

    if version < get_min_version():
        return jsonify({"error": "Invalid"}), 400

    lic = get_license(key)
    if not lic:
        return jsonify({"error": "Invalid"}), 400
    if lic["device_id"] != device:
        return jsonify({"error": "Invalid device"}), 403

    # Only write the version-log update when it actually changed —
    # avoids a DB write on every single /validate poll.
    if lic.get("app_version") != version_str:
        try:
            db_execute(
                "UPDATE licenses SET app_version = %s WHERE key = %s",
                (version_str, key)
            )
        except Exception:
            logger.error("[VALIDATE] version-log update failed (non-critical)\n%s", traceback.format_exc())

    days = lic["days"]
    if days != 0:
        duration_secs = abs(days) * 60 if days < 0 else days * 86400
        if time.time() > lic["activated_at"] + duration_secs:
            return jsonify({"error": "Expired"}), 403

    expires_at = None
    if days != 0:
        duration_secs = abs(days) * 60 if days < 0 else days * 86400
        expires_at = lic["activated_at"] + duration_secs

    cfg = get_poll_config()
    return jsonify({
        "status": "ok",
        "expires_at": expires_at,
        "poll_interval": cfg["license_poll_interval"],
        "shadow_check_every": cfg["shadow_check_every"]
    })


@app.route("/migrate-device", methods=["POST"])
def migrate_device():
    """One-time bridge for clients still holding a UID from before the
    MachineGuid switch. The UID proves identity (only the real holder of an
    already-activated license would have it cached locally), so we use it to
    re-stamp device_id to the new value the client now sends — meaning their
    very next plain key+device request succeeds instead of getting rejected
    as 'used on another device'. Safe to delete once existing installs have
    all had a chance to run this at least once."""
    data = request.json
    key = data.get("key", "").strip().upper()
    uid = data.get("unique_identifier")
    device = data.get("device")

    lic = get_license(key)
    if not lic or not uid or lic.get("unique_identifier") != uid:
        return jsonify({"error": "Invalid"}), 400

    db_execute("UPDATE licenses SET device_id = %s WHERE key = %s", (device, key))
    return jsonify({"ok": True}), 200


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
