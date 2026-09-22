"""
Shiro NC — Admin Control Panel
================================

Self-contained Flask Blueprint. Drop this file next to server.py, then in
server.py add:

    from admin import admin_bp, init_admin
    init_admin(app)   # reads ADMIN_PASSWORD / SECRET_KEY / ADMIN_URL_PREFIX from env
    app.register_blueprint(admin_bp)

That's it — no other changes to server.py are required. This module opens
its own DB connections (same NEON_DATABASE_URL env var your server already
uses) so it never has to import from server.py, which means no circular
imports and you can delete/replace it without touching anything else.

--------------------------------------------------------------------------
SETTING THE ADMIN PASSWORD
--------------------------------------------------------------------------
Set it as an environment variable (same place you set NEON_DATABASE_URL /
GROQ_API_KEY / etc — e.g. Render/Railway/Fly dashboard, or a .env file):

    ADMIN_PASSWORD=your-real-password-here
    SECRET_KEY=some-long-random-string   (keeps you logged in across restarts)

If ADMIN_PASSWORD is not set, it falls back to "changeme" and a big warning
is printed to the server log on startup — change it before you deploy.

--------------------------------------------------------------------------
ONBOARDING EMAIL (Resend)
--------------------------------------------------------------------------
Set these env vars:

    RESEND_API_KEY=re_xxxxxxxx
    RESEND_FROM=Shiro NC <onboarding@shironc.com>   (domain must be verified in Resend;
                                                      local part can be anything)
    R2_PUBLIC_BASE=https://cdn.shironc.com          (public R2 bucket/custom domain base)

When "Email" is filled in on the Quick add license form, creating the
license also sends an onboarding email with the current release's download
link (built from the Release tab's latest_version/object_key) and the new
license key. Leaving the email field blank just creates the license as
before — nothing else changes.

--------------------------------------------------------------------------
CHANGING THE URL LATER (shironc.com/admin/  ->  admin.shironc.com)
--------------------------------------------------------------------------
Everything is driven by ADMIN_URL_PREFIX (default "/admin"). When you move
to a subdomain, just set:

    ADMIN_URL_PREFIX=/

...and point admin.shironc.com at this same app (or run this blueprint on
its own tiny Flask app on that subdomain). Nothing inside this file needs
to change — every API call the frontend makes is a *relative* path.

--------------------------------------------------------------------------
ONE-TIME DB MIGRATION (customer name on licenses)
--------------------------------------------------------------------------
Run this once against your Neon database (SQL editor in the Neon console,
or `psql`) before the "Customer" field in the panel will work:

    ALTER TABLE licenses ADD COLUMN IF NOT EXISTS customer_name TEXT;

Everything else (list/create/update) already reads and writes this column.

--------------------------------------------------------------------------
ADDING NEW FEATURES LATER
--------------------------------------------------------------------------
1. Add a new @admin_bp.route(...) function down in the "API ROUTES" section.
2. Add a matching fetch() call + UI bit in static/admin.js (and a tab/button
   in templates/admin.html if it needs its own screen).
That's the whole pattern every existing feature below follows.
"""

import os
import time
import json
import functools
import secrets
from urllib.parse import quote

import psycopg2
import psycopg2.extras
import resend
from flask import (
    Blueprint, request, jsonify, session, send_from_directory, current_app
)

from email_templates import render_onboarding_email

# --------------------------------------------------------------------------
# Config (env-driven so nothing is hardcoded / everything is easy to change)
# --------------------------------------------------------------------------

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
ADMIN_URL_PREFIX = os.environ.get("ADMIN_URL_PREFIX", "/admin")
NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM = os.environ.get("RESEND_FROM", "Shiro NC <onboarding@shironc.com>")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "")

resend.api_key = RESEND_API_KEY

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_static")

admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix=ADMIN_URL_PREFIX,
)


def init_admin(app):
    """Call once from server.py: init_admin(app)"""
    if not app.secret_key:
        app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    if ADMIN_PASSWORD == "changeme":
        print(
            "\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            "  WARNING: ADMIN_PASSWORD is not set — using default 'changeme'.\n"
            "  Set the ADMIN_PASSWORD environment variable before deploying.\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        )

    if not RESEND_API_KEY:
        print(
            "\n"
            "  NOTE: RESEND_API_KEY is not set — onboarding emails will fail\n"
            "  silently (license creation still succeeds either way).\n"
        )


# A license counts as online if /validate refreshed last_seen this recently.
# Must stay above server.py's LAST_SEEN_REFRESH_SECS plus one poll interval,
# otherwise a client that is genuinely running will flicker offline.
ONLINE_WINDOW_SECS = 300


# --------------------------------------------------------------------------
# DB helpers (self-contained, mirrors server.py's pattern)
# --------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(NEON_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def db_execute(query, params=None, fetch=None, retries=2):
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


def get_config_value(key, default=None):
    row = db_execute("SELECT value FROM app_config WHERE key = %s", (key,), fetch="one")
    return row["value"] if row else default


def set_config_value(key, value):
    db_execute(
        """
        INSERT INTO app_config (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        (key, value),
    )


# --------------------------------------------------------------------------
# Onboarding email
# --------------------------------------------------------------------------

def _send_onboarding_email(to_email, license_key):
    """Best-effort send. Returns {"ok": True} or {"ok": False, "error": "..."} —
    never raises, so a bad email never blocks license creation."""
    raw = get_config_value("release_info", "{}")
    try:
        release = json.loads(raw)
    except Exception:
        release = {}

    object_key = release.get("installer_object_key")
    version = release.get("latest_version", "")

    if not RESEND_API_KEY:
        return {"ok": False, "error": "RESEND_API_KEY not configured"}
    if not object_key or not R2_PUBLIC_BASE:
        return {"ok": False, "error": "installer file name or R2_PUBLIC_BASE not configured"}

    download_url = f"{R2_PUBLIC_BASE}/{quote(object_key)}"
    html = render_onboarding_email(version, download_url, license_key)

    try:
        resend.Emails.send({
            "from": RESEND_FROM,
            "to": to_email,
            "subject": "Welcome to Shiro NC — your download is ready",
            "html": html,
        })
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_authed"):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


@admin_bp.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    password = data.get("password", "")
    # constant-time compare
    if secrets.compare_digest(password, ADMIN_PASSWORD):
        session["admin_authed"] = True
        session.permanent = True
        return jsonify({"ok": True})
    time.sleep(0.5)  # slow down brute force a little
    return jsonify({"error": "invalid password"}), 401


@admin_bp.route("/api/logout", methods=["POST"])
def logout():
    session.pop("admin_authed", None)
    return jsonify({"ok": True})


@admin_bp.route("/api/me", methods=["GET"])
def me():
    return jsonify({"authed": bool(session.get("admin_authed"))})


# --------------------------------------------------------------------------
# Page serving (single-page dashboard, static assets)
# --------------------------------------------------------------------------

@admin_bp.route("/", methods=["GET"])
def admin_index():
    return send_from_directory(_STATIC_DIR, "index.html")


@admin_bp.route("/assets/<path:filename>", methods=["GET"])
def admin_assets(filename):
    return send_from_directory(_STATIC_DIR, filename)


# ==========================================================================
# API ROUTES — add new features below following this same pattern
# ==========================================================================

# ---- Dashboard stats -------------------------------------------------

@admin_bp.route("/api/stats", methods=["GET"])
@login_required
def stats():
    total = db_execute("SELECT COUNT(*) AS c FROM licenses", fetch="one")["c"]
    activated = db_execute(
        "SELECT COUNT(*) AS c FROM licenses WHERE activated_at IS NOT NULL", fetch="one"
    )["c"]

    now = int(time.time())
    rows = db_execute(
        "SELECT days, activated_at FROM licenses WHERE activated_at IS NOT NULL", fetch="all"
    )
    active = 0
    expired = 0
    for r in rows:
        days = r["days"]
        if days == 0:
            active += 1
            continue
        duration = abs(days) * 60 if days < 0 else days * 86400
        if now > r["activated_at"] + duration:
            expired += 1
        else:
            active += 1

    online = db_execute(
        "SELECT COUNT(*) AS c FROM licenses WHERE last_seen >= %s",
        (now - ONLINE_WINDOW_SECS,),
        fetch="one",
    )["c"]

    return jsonify({
        "total_licenses": total,
        "activated_licenses": activated,
        "unactivated_licenses": total - activated,
        "active_licenses": active,
        "expired_licenses": expired,
        "online_licenses": online,
    })


# ---- Licenses ----------------------------------------------------------

def _license_status(lic):
    days = lic["days"]
    if lic["activated_at"] is None:
        return "unactivated"
    if days == 0:
        return "active"
    duration = abs(days) * 60 if days < 0 else days * 86400
    return "expired" if time.time() > lic["activated_at"] + duration else "active"


@admin_bp.route("/api/licenses", methods=["GET"])
@login_required
def list_licenses():
    search = request.args.get("q", "").strip()
    if search:
        rows = db_execute(
            "SELECT * FROM licenses WHERE key ILIKE %s OR customer_name ILIKE %s ORDER BY key",
            (f"%{search.upper()}%", f"%{search}%"),
            fetch="all",
        )
    else:
        rows = db_execute("SELECT * FROM licenses ORDER BY key", fetch="all")

    cutoff = int(time.time()) - ONLINE_WINDOW_SECS
    out = []
    for r in rows:
        r = dict(r)
        r["status"] = _license_status(r)
        r["online"] = (r.get("last_seen") or 0) >= cutoff
        out.append(r)
    return jsonify({"licenses": out})


@admin_bp.route("/api/licenses", methods=["POST"])
@login_required
def create_license():
    data = request.json or {}
    key = (data.get("key") or "").strip().upper()
    if not key:
        key = "-".join(secrets.token_hex(2).upper() for _ in range(4))
    days = int(data.get("days", 90))
    customer_name = (data.get("customer_name") or "").strip() or None
    email = (data.get("email") or "").strip() or None

    existing = db_execute("SELECT key FROM licenses WHERE key = %s", (key,), fetch="one")
    if existing:
        return jsonify({"error": "a license with that key already exists"}), 409

    db_execute(
        "INSERT INTO licenses (key, days, activated_at, device_id, customer_name) "
        "VALUES (%s, %s, NULL, NULL, %s)",
        (key, days, customer_name),
    )

    email_sent = None
    if email:
        email_sent = _send_onboarding_email(email, key)

    return jsonify({"ok": True, "key": key, "email_sent": email_sent})


@admin_bp.route("/api/licenses/<key>", methods=["PATCH"])
@login_required
def update_license_route(key):
    key = key.strip().upper()
    lic = db_execute("SELECT * FROM licenses WHERE key = %s", (key,), fetch="one")
    if not lic:
        return jsonify({"error": "not found"}), 404

    data = request.json or {}
    fields, params = [], []

    if "days" in data:
        fields.append("days = %s")
        params.append(int(data["days"]))

    if "customer_name" in data:
        fields.append("customer_name = %s")
        params.append((data.get("customer_name") or "").strip() or None)

    if data.get("reset_device"):
        fields.append("device_id = NULL")
        fields.append("unique_identifier = NULL")

    if data.get("reset_activation"):
        fields.append("activated_at = NULL")
        fields.append("device_id = NULL")
        fields.append("unique_identifier = NULL")

    if not fields:
        return jsonify({"error": "nothing to update"}), 400

    params.append(key)
    db_execute(f"UPDATE licenses SET {', '.join(fields)} WHERE key = %s", tuple(params))
    return jsonify({"ok": True})


@admin_bp.route("/api/licenses/<key>", methods=["DELETE"])
@login_required
def delete_license(key):
    key = key.strip().upper()
    db_execute("DELETE FROM licenses WHERE key = %s", (key,))
    return jsonify({"ok": True})


# ---- Broadcast message ---------------------------------------------------

@admin_bp.route("/api/broadcast", methods=["GET"])
@login_required
def get_broadcast():
    return jsonify({"message": get_config_value("broadcast_message", "")})


@admin_bp.route("/api/broadcast", methods=["POST"])
@login_required
def set_broadcast():
    data = request.json or {}
    set_config_value("broadcast_message", data.get("message", ""))
    return jsonify({"ok": True})


# ---- Min version -----------------------------------------------------

@admin_bp.route("/api/min-version", methods=["GET"])
@login_required
def get_min_version_route():
    return jsonify({"min_version": get_config_value("min_version", "")})


@admin_bp.route("/api/min-version", methods=["POST"])
@login_required
def set_min_version_route():
    data = request.json or {}
    value = (data.get("min_version") or "").strip()
    if not value:
        return jsonify({"error": "min_version required"}), 400
    set_config_value("min_version", value)
    return jsonify({"ok": True})


# ---- Poll config -------------------------------------------------------

@admin_bp.route("/api/poll-config", methods=["GET"])
@login_required
def get_poll_config_route():
    raw = get_config_value("poll_config", "{}")
    try:
        value = json.loads(raw)
    except Exception:
        value = {}
    return jsonify({"poll_config": value})


@admin_bp.route("/api/poll-config", methods=["POST"])
@login_required
def set_poll_config_route():
    data = request.json or {}
    cfg = data.get("poll_config")
    if not isinstance(cfg, dict):
        return jsonify({"error": "poll_config must be an object"}), 400
    set_config_value("poll_config", json.dumps(cfg))
    return jsonify({"ok": True})


# ---- Update / release info ----------------------------------------------
# These mirror the LATEST_VERSION / GITHUB_* constants in server.py so you
# can update them from the panel without redeploying. server.py should read
# these via get_config_value(...) with the hardcoded constant as fallback —
# see the integration notes in server_integration.md.

@admin_bp.route("/api/release", methods=["GET"])
@login_required
def get_release():
    raw = get_config_value("release_info", "{}")
    try:
        value = json.loads(raw)
    except Exception:
        value = {}
    return jsonify({"release": value})


@admin_bp.route("/api/release", methods=["POST"])
@login_required
def set_release():
    data = request.json or {}
    release = {
        "latest_version": (data.get("latest_version") or "").strip(),
        "object_key": (data.get("object_key") or "").strip(),
        "installer_object_key": (data.get("installer_object_key") or "").strip(),
    }
    if not release["latest_version"]:
        return jsonify({"error": "latest_version required"}), 400
    set_config_value("release_info", json.dumps(release))
    return jsonify({"ok": True})
