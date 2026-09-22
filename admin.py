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
import hashlib
from datetime import timedelta
from urllib.parse import quote

import psycopg2
import psycopg2.extras
import resend
from flask import (
    Blueprint, request, jsonify, session, send_from_directory, current_app
)

from email_templates import render_onboarding_email, render_otp_email

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

# How long an admin login lasts before it has to be re-entered. This is an
# absolute cap measured from sign-in, not an idle timeout.
ADMIN_SESSION_HOURS = int(os.environ.get("ADMIN_SESSION_HOURS", "1"))

# Where sign-in codes go. If this is unset the OTP step is skipped entirely —
# deliberately failing open, because a missing address or a Resend outage would
# otherwise lock the only admin out of their own panel with no way back in.
ADMIN_OTP_EMAIL = os.environ.get("ADMIN_OTP_EMAIL", "").strip()

OTP_TTL_MINUTES = int(os.environ.get("ADMIN_OTP_TTL_MINUTES", "10"))
OTP_MAX_ATTEMPTS = 5
TRUSTED_DEVICE_DAYS = int(os.environ.get("ADMIN_TRUSTED_DEVICE_DAYS", "30"))
DEVICE_COOKIE = "shironc_admin_device"

admin_bp = Blueprint(
    "admin",
    __name__,
    url_prefix=ADMIN_URL_PREFIX,
)


def init_admin(app):
    """Call once from server.py: init_admin(app)"""
    if not app.secret_key:
        app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    # Flask defaults a permanent session to 31 days AND refreshes the cookie on
    # every request, so any visit inside the window silently extends it another
    # 31 days — in practice a login never expires. Pin an absolute lifetime and
    # stop the rolling refresh so the clock runs from sign-in.
    app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(hours=ADMIN_SESSION_HOURS),
        SESSION_REFRESH_EACH_REQUEST=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Render terminates TLS, so the cookie should be HTTPS-only there. Local
        # dev over plain http:// needs ADMIN_COOKIE_INSECURE=1 or login silently
        # fails — the browser accepts the cookie but never sends it back.
        SESSION_COOKIE_SECURE=os.environ.get("ADMIN_COOKIE_INSECURE") != "1",
    )

    if ADMIN_PASSWORD == "changeme":
        print(
            "\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
            "  WARNING: ADMIN_PASSWORD is not set — using default 'changeme'.\n"
            "  Set the ADMIN_PASSWORD environment variable before deploying.\n"
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        )

    if not ADMIN_OTP_EMAIL:
        print(
            "\n"
            "  NOTE: ADMIN_OTP_EMAIL is not set — the control panel will accept\n"
            "  the password alone, with no emailed code, on any device.\n"
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


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")


def _device_label():
    """A rough, human-recognisable name so revoking the right row is possible.
    Not a fingerprint and not trusted for anything — it is only ever shown."""
    ua = request.headers.get("User-Agent", "")
    browser = next((b for b in ("Edg", "OPR", "Chrome", "Firefox", "Safari") if b in ua), None)
    browser = {"Edg": "Edge", "OPR": "Opera"}.get(browser, browser) or "Unknown browser"
    os_name = next(
        (o for o in ("Windows", "Android", "iPhone", "iPad", "Mac", "Linux") if o in ua),
        "Unknown OS",
    )
    return f"{browser} on {os_name}"


def _current_device_row():
    """The non-expired trusted-device row matching this browser's cookie, if any."""
    token = request.cookies.get(DEVICE_COOKIE)
    if not token:
        return None
    return db_execute(
        "SELECT * FROM admin_trusted_devices WHERE token_hash = %s AND expires_at > %s",
        (_hash(token), int(time.time())),
        fetch="one",
    )


def _remember_device(response):
    """Issue a trusted-device token to this browser and record its hash."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + TRUSTED_DEVICE_DAYS * 86400
    db_execute(
        "INSERT INTO admin_trusted_devices (token_hash, label, created_at, last_used_at, expires_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (_hash(token), _device_label(), now, now, expires),
    )
    response.set_cookie(
        DEVICE_COOKIE,
        token,
        max_age=TRUSTED_DEVICE_DAYS * 86400,
        httponly=True,
        secure=os.environ.get("ADMIN_COOKIE_INSECURE") != "1",
        samesite="Lax",
    )
    return response


def _send_otp_email(code):
    if not RESEND_API_KEY:
        return {"ok": False, "error": "RESEND_API_KEY not configured"}
    try:
        resend.Emails.send({
            "from": RESEND_FROM,
            "to": ADMIN_OTP_EMAIL,
            "subject": f"{code} is your Shiro NC control panel code",
            "html": render_otp_email(code, OTP_TTL_MINUTES, _client_ip()),
        })
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _mask_email(addr):
    name, _, domain = addr.partition("@")
    shown = name[:2] if len(name) > 2 else name[:1]
    return f"{shown}{'*' * max(3, len(name) - len(shown))}@{domain}"


def _start_otp_challenge():
    """Create a challenge, email the code, and pin it to this browser's session."""
    code = f"{secrets.randbelow(1000000):06d}"
    now = int(time.time())
    db_execute("DELETE FROM admin_otp_challenges WHERE expires_at < %s", (now - 86400,))
    row = db_execute(
        "INSERT INTO admin_otp_challenges (code_hash, created_at, expires_at)"
        " VALUES (%s, %s, %s) RETURNING id",
        (_hash(code), now, now + OTP_TTL_MINUTES * 60),
        fetch="one",
    )
    sent = _send_otp_email(code)
    if not sent["ok"]:
        db_execute("DELETE FROM admin_otp_challenges WHERE id = %s", (row["id"],))
        return None, sent["error"]

    session.clear()
    session["otp_challenge"] = row["id"]
    return row["id"], None


@admin_bp.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    password = data.get("password", "")
    # constant-time compare
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        time.sleep(0.5)  # slow down brute force a little
        return jsonify({"error": "invalid password"}), 401

    device = _current_device_row()
    if device:
        db_execute(
            "UPDATE admin_trusted_devices SET last_used_at = %s WHERE id = %s",
            (int(time.time()), device["id"]),
        )
        session.clear()
        session["admin_authed"] = True
        session.permanent = True
        return jsonify({"ok": True, "trusted_device": True})

    if not ADMIN_OTP_EMAIL:
        session.clear()
        session["admin_authed"] = True
        session.permanent = True
        return jsonify({"ok": True, "otp_skipped": True})

    _, err = _start_otp_challenge()
    if err:
        current_app.logger.error("[ADMIN] OTP send failed: %s", err)
        return jsonify({"error": f"could not send the sign-in code: {err}"}), 500

    return jsonify({"otp_required": True, "sent_to": _mask_email(ADMIN_OTP_EMAIL)})


@admin_bp.route("/api/verify-otp", methods=["POST"])
def verify_otp():
    data = request.json or {}
    code = str(data.get("code", "")).strip()
    trust = bool(data.get("trust_device"))

    challenge_id = session.get("otp_challenge")
    if not challenge_id:
        return jsonify({"error": "no sign-in in progress"}), 401

    row = db_execute(
        "SELECT * FROM admin_otp_challenges WHERE id = %s", (challenge_id,), fetch="one"
    )
    now = int(time.time())
    if not row or row["consumed"] or row["expires_at"] < now:
        session.pop("otp_challenge", None)
        return jsonify({"error": "that code has expired — sign in again"}), 401
    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        db_execute("UPDATE admin_otp_challenges SET consumed = TRUE WHERE id = %s", (challenge_id,))
        session.pop("otp_challenge", None)
        return jsonify({"error": "too many attempts — sign in again"}), 401

    db_execute(
        "UPDATE admin_otp_challenges SET attempts = attempts + 1 WHERE id = %s", (challenge_id,)
    )
    if not secrets.compare_digest(_hash(code), row["code_hash"]):
        left = OTP_MAX_ATTEMPTS - (row["attempts"] + 1)
        time.sleep(0.5)
        return jsonify({"error": "incorrect code", "attempts_left": max(0, left)}), 401

    db_execute("UPDATE admin_otp_challenges SET consumed = TRUE WHERE id = %s", (challenge_id,))
    session.clear()
    session["admin_authed"] = True
    session.permanent = True

    response = jsonify({"ok": True, "device_trusted": trust})
    return _remember_device(response) if trust else response


@admin_bp.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@admin_bp.route("/api/me", methods=["GET"])
def me():
    return jsonify({"authed": bool(session.get("admin_authed"))})


# ---- Trusted devices ---------------------------------------------------

@admin_bp.route("/api/devices", methods=["GET"])
@login_required
def list_devices():
    db_execute("DELETE FROM admin_trusted_devices WHERE expires_at <= %s", (int(time.time()),))
    current = _current_device_row()
    current_id = current["id"] if current else None
    rows = db_execute(
        "SELECT id, label, created_at, last_used_at, expires_at FROM admin_trusted_devices"
        " ORDER BY last_used_at DESC NULLS LAST",
        fetch="all",
    )
    out = []
    for r in rows or []:
        r = dict(r)
        r["current"] = r["id"] == current_id
        out.append(r)
    return jsonify({
        "devices": out,
        "current_trusted": current_id is not None,
        "otp_enabled": bool(ADMIN_OTP_EMAIL),
        "otp_email": _mask_email(ADMIN_OTP_EMAIL) if ADMIN_OTP_EMAIL else None,
    })


@admin_bp.route("/api/devices/trust", methods=["POST"])
@login_required
def trust_this_device():
    if _current_device_row():
        return jsonify({"ok": True, "already": True})
    return _remember_device(jsonify({"ok": True}))


@admin_bp.route("/api/devices/<int:device_id>", methods=["DELETE"])
@login_required
def revoke_device(device_id):
    current = _current_device_row()
    db_execute("DELETE FROM admin_trusted_devices WHERE id = %s", (device_id,))
    response = jsonify({"ok": True})
    if current and current["id"] == device_id:
        response.delete_cookie(DEVICE_COOKIE)
    return response


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
