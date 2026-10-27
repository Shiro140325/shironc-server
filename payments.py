"""
Shiro NC — PayMongo checkout (buy a 30-day license, or renew one)
=================================================================

Self-contained Flask Blueprint, same pattern as admin.py: it opens its own DB
connections (NEON_DATABASE_URL, the one server.py already uses), so it never
imports from server.py and can be removed without touching anything else.

Wire it up in server.py with:

    from payments import payments_bp, init_payments
    init_payments(app)
    app.register_blueprint(payments_bp)

--------------------------------------------------------------------------
WHAT THE CLIENT TALKS TO
--------------------------------------------------------------------------
  POST /purchase          start a checkout -> {"checkout_url", "reference"}
  GET  /purchase/status   poll until paid  -> {"status", "key", "expires_at"}
  POST /paymongo/webhook  PayMongo calls this when the payment lands
  GET  /pay/thanks        the page the browser is sent to after paying
  GET  /pay/cancelled     ...and after backing out

The app opens /purchase's checkout_url in the user's browser, then polls
/purchase/status. The webhook is what actually grants the time — polling only
reads the result, so a user who closes the browser mid-payment still gets
their license (and their key by email).

--------------------------------------------------------------------------
ENVIRONMENT
--------------------------------------------------------------------------
    PAYMONGO_SECRET_KEY        sk_live_... (or sk_test_... while testing)
    PAYMONGO_WEBHOOK_SECRET    the signing secret of the webhook you created
                               for THIS server's /paymongo/webhook URL

Both are read with a few alternative spellings (see _env_any) so an existing
Render environment that called them something slightly different still works —
the name actually found is printed at startup.

Optional:
    LICENSE_PRICE_CENTAVOS     default 12900  (= PHP 129.00)
    LICENSE_DAYS               default 30
    PAYMONGO_METHODS           comma-separated, default "qrph" (what this account
                               has activated in live mode); falls back through
                               qrph / gcash / card if PayMongo refuses the list
    PUBLIC_BASE_URL            default "https://server.shironc.com" (success/cancel pages)
    PAYMONGO_WEBHOOK_INSECURE  "1" skips signature checks — local testing only

Resend's settings are shared with admin.py (RESEND_API_KEY / RESEND_FROM), and
the onboarding email itself is admin.py's, so a license bought here arrives
looking exactly like one created by hand in the panel.

--------------------------------------------------------------------------
ONE-TIME DB MIGRATION
--------------------------------------------------------------------------
init_payments() runs this itself at startup (it is idempotent), so there is
nothing to do by hand:

    CREATE TABLE IF NOT EXISTS license_orders (
        id          SERIAL PRIMARY KEY,
        reference   TEXT UNIQUE NOT NULL,
        checkout_id TEXT,
        mode        TEXT NOT NULL,
        license_key TEXT,
        device_id   TEXT,
        email       TEXT,
        amount      INTEGER NOT NULL,
        days        INTEGER NOT NULL,
        status      TEXT NOT NULL,
        created_at  BIGINT NOT NULL,
        paid_at     BIGINT,
        payment_id  TEXT,
        note        TEXT
    );
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import traceback

import psycopg2
import psycopg2.extras
import requests
from flask import Blueprint, jsonify, request

from netguard import Limiter, real_ip

logger = logging.getLogger("shironc.payments")

payments_bp = Blueprint("payments", __name__)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _env_any(*names, default=""):
    """First of [names] that is set, plus which one it was (for the startup log).
    Having more than one spelling here means the server works against whatever
    the environment already calls these, instead of silently refusing to sell."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v, n
    return default, ""


PAYMONGO_SECRET_KEY, _SECRET_ENV_NAME = _env_any(
    "PAYMONGO_SECRET_KEY", "PAYMONGO_SECRET", "PAYMONGO_API_KEY",
    "PAYMONGO_SK", "PAYMONGO_KEY",
)
PAYMONGO_WEBHOOK_SECRET, _WEBHOOK_ENV_NAME = _env_any(
    "PAYMONGO_WEBHOOK_SECRET", "PAYMONGO_WEBHOOK_SECRET_KEY",
    "PAYMONGO_WEBHOOK_SIGNING_SECRET", "PAYMONGO_WEBHOOK_KEY",
    "PAYMONGO_WEBHOOK_SIG", "PAYMONGO_WEBHOOK",
)

NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL")

PRICE_CENTAVOS = int(os.environ.get("LICENSE_PRICE_CENTAVOS", "12900"))
LICENSE_DAYS = int(os.environ.get("LICENSE_DAYS", "30"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://server.shironc.com").rstrip("/")

# What the checkout page offers. PayMongo rejects the whole request if the
# account has not activated one of these, so the default is what the SMM panel
# on this same account sells with in live mode (QR Ph, which every PH bank and
# wallet app can scan). Widen it with PAYMONGO_METHODS once more are activated
# — "gcash,paymaya,card,grab_pay,qrph" and so on.
PAYMONGO_METHODS = [
    m.strip() for m in
    os.environ.get("PAYMONGO_METHODS", "qrph").split(",")
    if m.strip()
]

# Tried in turn when PayMongo refuses the list above by name, so a method that
# is not activated yet costs us a retry instead of the sale.
_FALLBACK_METHODS = (["qrph"], ["gcash"], ["card"])

PAYMONGO_API = "https://api.paymongo.com/v1"

# A pending checkout the user never paid for stops being pollable after this,
# so a stale reference can't be left polling forever.
ORDER_TTL_SECS = 24 * 3600

# Webhook deliveries are retried by PayMongo for a while after a failure, so
# the window has to be generous — this only rejects a signature replayed days
# later.
SIGNATURE_MAX_AGE_SECS = 7 * 24 * 3600


def init_payments(app):
    """Call once from server.py: init_payments(app)"""
    if not PAYMONGO_SECRET_KEY:
        print(
            "\n"
            "  NOTE: no PayMongo secret key in the environment — /purchase will\n"
            "  refuse every checkout. Set PAYMONGO_SECRET_KEY (sk_live_... or\n"
            "  sk_test_... while testing).\n",
            flush=True,
        )
    else:
        mode = "TEST" if PAYMONGO_SECRET_KEY.startswith("sk_test") else "LIVE"
        print(f"[payments] PayMongo key from {_SECRET_ENV_NAME} ({mode} mode)", flush=True)

    if not PAYMONGO_WEBHOOK_SECRET:
        print(
            "\n"
            "  WARNING: no PayMongo webhook secret in the environment — every\n"
            "  webhook delivery will be rejected unsigned, so paid licenses will\n"
            "  never be granted. Set PAYMONGO_WEBHOOK_SECRET to the signing\n"
            "  secret PayMongo shows for this server's /paymongo/webhook.\n",
            flush=True,
        )
    else:
        print(f"[payments] webhook secret from {_WEBHOOK_ENV_NAME}", flush=True)

    print(f"[payments] selling {LICENSE_DAYS} days for {PRICE_CENTAVOS / 100:.2f} PHP", flush=True)

    # Idempotent, so it is safe on every boot — and means there is no manual
    # migration step to forget when this is deployed.
    with app.app_context():
        try:
            db_execute(
                """
                CREATE TABLE IF NOT EXISTS license_orders (
                    id          SERIAL PRIMARY KEY,
                    reference   TEXT UNIQUE NOT NULL,
                    checkout_id TEXT,
                    mode        TEXT NOT NULL,
                    license_key TEXT,
                    device_id   TEXT,
                    email       TEXT,
                    amount      INTEGER NOT NULL,
                    days        INTEGER NOT NULL,
                    status      TEXT NOT NULL,
                    created_at  BIGINT NOT NULL,
                    paid_at     BIGINT,
                    payment_id  TEXT,
                    note        TEXT
                )
                """
            )
            db_execute(
                "CREATE INDEX IF NOT EXISTS license_orders_checkout"
                " ON license_orders (checkout_id)"
            )
            print("[payments] license_orders table ready", flush=True)
        except Exception:
            logger.error("[PAYMENTS] table setup failed, /purchase will error until it works\n%s",
                         traceback.format_exc())


# --------------------------------------------------------------------------
# DB helpers (self-contained, mirrors server.py / admin.py's pattern)
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


# --------------------------------------------------------------------------
# Rate limits — a checkout costs us a PayMongo API call, and the status poll
# hands out a license key, so neither is left open.
# --------------------------------------------------------------------------

_checkout_limiter = Limiter(12, 3600)      # per IP, per hour
# Generous on purpose: a single purchase polls every 4s for up to 20 minutes,
# and whole neighbourhoods can share one address behind an ISP's NAT.
_status_limiter = Limiter(3000, 3600)
_unknown_refs = Limiter(40, 15 * 60)       # guessing references


def _ip():
    return real_ip(request.headers, request.remote_addr)


# --------------------------------------------------------------------------
# License time
# --------------------------------------------------------------------------

def _duration_secs(days):
    """Mirrors server.py: a negative [days] means minutes (test licenses)."""
    return abs(days) * 60 if days < 0 else days * 86400


def _expires_at(lic):
    if lic["days"] == 0 or lic["activated_at"] is None:
        return None
    return lic["activated_at"] + _duration_secs(lic["days"])


def _new_key():
    """Same shape the admin panel generates: XXXX-XXXX-XXXX-XXXX."""
    for _ in range(5):
        key = "-".join(secrets.token_hex(2).upper() for _ in range(4))
        if not db_execute("SELECT key FROM licenses WHERE key = %s", (key,), fetch="one"):
            return key
    raise RuntimeError("could not generate an unused license key")


def _add_days(key, days_to_add):
    """Add [days_to_add] to a license and return its new expiry (or None for a
    license that never expires / isn't activated yet).

    Expiry lives in the licenses table as activated_at + days, so there are
    three cases:
      * still valid  -> bump days, keep activated_at. Renewing early stacks on
                        top of the time left instead of throwing it away.
      * lapsed       -> start a fresh window from now, or the time already run
                        down would eat straight into what was just paid for.
      * not activated-> bump days and leave activated_at NULL; the clock starts
                        when the user activates, as it does for a new license.
    """
    lic = db_execute("SELECT * FROM licenses WHERE key = %s", (key,), fetch="one")
    if not lic:
        return None, "not found"
    if lic["days"] == 0:
        return None, "lifetime"  # nothing to extend

    now = int(time.time())
    if lic["activated_at"] is None:
        new_days = max(0, lic["days"]) + days_to_add
        new_activated = None
    elif lic["days"] > 0 and lic["activated_at"] + _duration_secs(lic["days"]) > now:
        new_days = lic["days"] + days_to_add
        new_activated = lic["activated_at"]
    else:
        # Lapsed, or one of the minutes-based test keys (days < 0) — either way
        # this becomes a plain [days_to_add]-day license starting now.
        new_days = days_to_add
        new_activated = now

    db_execute(
        "UPDATE licenses SET days = %s, activated_at = %s WHERE key = %s",
        (new_days, new_activated, key),
    )
    return (None if new_activated is None else new_activated + new_days * 86400), None


# --------------------------------------------------------------------------
# PayMongo
# --------------------------------------------------------------------------

def _auth_header():
    # PayMongo uses HTTP basic auth with the secret key as the username and an
    # empty password.
    token = base64.b64encode(f"{PAYMONGO_SECRET_KEY}:".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def _create_checkout_session(reference, description, metadata):
    """Returns (checkout_url, checkout_id). Raises RuntimeError with something
    printable on failure."""
    tried, last_error = [], None
    for methods in (PAYMONGO_METHODS, *_FALLBACK_METHODS):
        if not methods or methods in tried:
            continue
        tried.append(methods)
        try:
            return _post_checkout_session(reference, description, metadata, methods)
        except _MethodRefused as e:
            logger.warning("[PAYMENTS] %s not accepted (%s), trying the next list", methods, e)
            last_error = e
    raise RuntimeError(
        f"PayMongo would not accept any of these payment methods: {tried}. "
        f"Activate one on the account, or set PAYMONGO_METHODS. ({last_error})"
    )


class _MethodRefused(Exception):
    """PayMongo turned the request down over payment_method_types specifically."""


def _post_checkout_session(reference, description, metadata, methods):
    payload = {
        "data": {
            "attributes": {
                "line_items": [{
                    "currency": "PHP",
                    "amount": PRICE_CENTAVOS,
                    "name": f"Shiro NC — {LICENSE_DAYS}-day license",
                    "quantity": 1,
                    "description": description,
                }],
                "payment_method_types": methods,
                "description": description,
                "reference_number": reference,
                "send_email_receipt": True,
                "show_description": True,
                "show_line_items": True,
                "success_url": f"{PUBLIC_BASE_URL}/pay/thanks",
                "cancel_url": f"{PUBLIC_BASE_URL}/pay/cancelled",
                # Everything needed to fulfil the order rides along with the
                # payment, so the webhook can work even if the row is somehow
                # unreadable.
                "metadata": {k: str(v) for k, v in metadata.items() if v},
            }
        }
    }

    try:
        r = requests.post(
            f"{PAYMONGO_API}/checkout_sessions",
            headers=_auth_header(), json=payload, timeout=15,
        )
    except Exception as e:
        logger.error("[PAYMENTS] checkout_sessions call failed: %s\n%s", e, traceback.format_exc())
        raise RuntimeError("could not reach PayMongo")

    if r.status_code >= 400:
        detail = r.text[:400]
        logger.error("[PAYMENTS] checkout_sessions HTTP %s: %s", r.status_code, detail)
        if r.status_code in (400, 422) and "payment_method" in detail:
            raise _MethodRefused(detail)
        raise RuntimeError(f"PayMongo rejected the checkout (HTTP {r.status_code})")

    try:
        data = r.json()["data"]
        return data["attributes"]["checkout_url"], data["id"]
    except Exception as e:
        logger.error("[PAYMENTS] unreadable checkout_sessions response: %s — %s", e, r.text[:400])
        raise RuntimeError("unreadable response from PayMongo")


def _signature_ok(raw_body, header):
    """PayMongo signs as 't=<unix>,te=<test hmac>,li=<live hmac>', each hmac
    being SHA256 of '<t>.<raw body>' keyed with the webhook's signing secret."""
    if os.environ.get("PAYMONGO_WEBHOOK_INSECURE") == "1":
        logger.warning("[PAYMENTS] PAYMONGO_WEBHOOK_INSECURE=1 — signature not checked")
        return True
    if not PAYMONGO_WEBHOOK_SECRET or not header:
        return False

    parts = {}
    for piece in header.split(","):
        name, _, value = piece.strip().partition("=")
        if name and value:
            parts[name] = value

    ts = parts.get("t")
    if not ts:
        return False
    try:
        if abs(time.time() - int(ts)) > SIGNATURE_MAX_AGE_SECS:
            logger.warning("[PAYMENTS] webhook signature timestamp too old: %s", ts)
            return False
    except ValueError:
        return False

    expected = hmac.new(
        PAYMONGO_WEBHOOK_SECRET.encode(),
        f"{ts}.".encode() + raw_body,
        hashlib.sha256,
    ).hexdigest()

    # te is the test-mode signature, li the live one. Accepting either keeps a
    # test webhook working without a second deploy.
    return any(
        hmac.compare_digest(expected, parts.get(field, ""))
        for field in ("li", "te")
    )


def _paid_amount(attrs):
    """Centavos actually paid, as far as the event says. 0 when the payload
    doesn't carry it (then the amount check is skipped)."""
    payments = attrs.get("payments")
    if isinstance(payments, list) and payments:
        total = 0
        for p in payments:
            pa = (p or {}).get("attributes") or {}
            if pa.get("status") in (None, "paid"):
                total += int(pa.get("amount") or 0)
        return total
    if isinstance(attrs.get("amount"), int):
        return attrs["amount"]
    return 0


def _billing_email(attrs):
    for holder in (attrs.get("billing"), attrs.get("customer")):
        if isinstance(holder, dict) and holder.get("email"):
            return holder["email"].strip()
    payments = attrs.get("payments")
    if isinstance(payments, list):
        for p in payments:
            pa = (p or {}).get("attributes") or {}
            billing = pa.get("billing") or {}
            if billing.get("email"):
                return billing["email"].strip()
    return None


def _billing_name(attrs):
    for holder in (attrs.get("billing"), attrs.get("customer")):
        if isinstance(holder, dict) and holder.get("name"):
            return holder["name"].strip()
    return None


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def _send_key_email(to_email, license_key):
    """A bought license gets the same onboarding email (download link + key) as
    one created by hand in the panel — admin.py owns that template, so there is
    one copy of it."""
    if not to_email:
        return {"ok": False, "error": "no email on the payment"}
    try:
        from admin import _send_onboarding_email
        return _send_onboarding_email(to_email, license_key)
    except Exception as e:
        logger.error("[PAYMENTS] onboarding email failed: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


def _send_renewal_email(to_email, license_key, expires_at):
    if not to_email:
        return {"ok": False, "error": "no email on the payment"}
    try:
        import resend
        from email_templates import render_renewal_email
        from admin import RESEND_API_KEY, RESEND_FROM
        if not RESEND_API_KEY:
            return {"ok": False, "error": "RESEND_API_KEY not configured"}
        resend.Emails.send({
            "from": RESEND_FROM,
            "to": to_email,
            "subject": f"Shiro NC — {LICENSE_DAYS} more days added",
            "html": render_renewal_email(license_key, expires_at, LICENSE_DAYS),
        })
        return {"ok": True}
    except Exception as e:
        logger.error("[PAYMENTS] renewal email failed: %s\n%s", e, traceback.format_exc())
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------
# Routes — the app
# --------------------------------------------------------------------------

@payments_bp.route("/purchase", methods=["POST"])
def purchase():
    """Start a checkout. Body: {"mode": "new"|"renew", "key": ..., "device": ...}

    "renew" needs the key it is topping up (and, once a license is bound to a
    device, that same device — otherwise anyone holding a key could be charged
    for someone else's). "new" needs nothing: the key is created when the
    payment lands.
    """
    ip = _ip()
    if _checkout_limiter.blocked(ip):
        return jsonify({"error": "Too many checkout attempts. Try again later."}), 429

    if not PAYMONGO_SECRET_KEY:
        return jsonify({"error": "Payments are not configured on the server."}), 503

    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip().upper()
    device = (data.get("device") or "").strip() or None
    mode = (data.get("mode") or ("renew" if key else "new")).strip().lower()
    if mode not in ("new", "renew"):
        return jsonify({"error": "Invalid request."}), 400

    if mode == "renew":
        if not key:
            return jsonify({"error": "No license key to renew."}), 400
        lic = db_execute("SELECT * FROM licenses WHERE key = %s", (key,), fetch="one")
        if not lic:
            _unknown_refs.add(ip)
            return jsonify({"error": "That license key is not on record."}), 404
        if lic["days"] == 0:
            return jsonify({"error": "This license never expires — nothing to renew."}), 400
        if lic["device_id"] and device and lic["device_id"] != device:
            return jsonify({"error": "That license is active on another device."}), 403
    else:
        key = None

    reference = "SNC" + secrets.token_hex(6).upper()
    description = (
        f"Shiro NC renewal — {key}" if mode == "renew"
        else f"Shiro NC {LICENSE_DAYS}-day license"
    )

    try:
        checkout_url, checkout_id = _create_checkout_session(
            reference, description,
            {"reference": reference, "mode": mode, "license_key": key or "", "device": device or ""},
        )
    except RuntimeError as e:
        return jsonify({"error": f"Could not open checkout: {e}"}), 502

    _checkout_limiter.add(ip)
    db_execute(
        "INSERT INTO license_orders (reference, checkout_id, mode, license_key, device_id,"
        " amount, days, status, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)",
        (reference, checkout_id, mode, key, device, PRICE_CENTAVOS, LICENSE_DAYS, int(time.time())),
    )
    logger.info("[PAYMENTS] checkout %s opened (%s%s)", reference, mode, f" {key}" if key else "")

    return jsonify({
        "ok": True,
        "checkout_url": checkout_url,
        "reference": reference,
        "amount": PRICE_CENTAVOS,
        "currency": "PHP",
        "days": LICENSE_DAYS,
    })


@payments_bp.route("/purchase/status", methods=["GET"])
def purchase_status():
    """Poll a checkout. ?reference=...&device=...

    Answers "pending" until the webhook has granted the time, then "paid" with
    the key to activate (for a purchase) and the new expiry. The key is only
    handed back to the device the checkout was started from, so knowing a
    reference isn't enough to collect someone else's license.
    """
    ip = _ip()
    if _status_limiter.blocked(ip) or _unknown_refs.blocked(ip):
        return jsonify({"error": "Too many requests."}), 429
    _status_limiter.add(ip)

    reference = (request.args.get("reference") or "").strip()
    device = (request.args.get("device") or "").strip() or None
    if not reference:
        return jsonify({"error": "reference required"}), 400

    order = db_execute(
        "SELECT * FROM license_orders WHERE reference = %s", (reference,), fetch="one"
    )
    if not order:
        _unknown_refs.add(ip)
        return jsonify({"error": "unknown reference"}), 404

    if order["device_id"] and device and order["device_id"] != device:
        return jsonify({"error": "unknown reference"}), 404

    if order["status"] != "paid":
        if int(time.time()) - order["created_at"] > ORDER_TTL_SECS:
            return jsonify({"status": "expired"})
        return jsonify({"status": order["status"], "note": order.get("note") or ""})

    out = {"status": "paid", "mode": order["mode"], "days": order["days"]}
    lic = None
    if order["license_key"]:
        lic = db_execute("SELECT * FROM licenses WHERE key = %s", (order["license_key"],), fetch="one")
        # Only released to the device that paid; a renewal already has the key.
        if not order["device_id"] or (device and order["device_id"] == device):
            out["key"] = order["license_key"]
    if lic:
        out["expires_at"] = _expires_at(lic)
    return jsonify(out)


# --------------------------------------------------------------------------
# Routes — PayMongo
# --------------------------------------------------------------------------

_HANDLED_EVENTS = ("checkout_session.payment.paid", "payment.paid", "link.payment.paid")


@payments_bp.route("/paymongo/webhook", methods=["POST"])
def paymongo_webhook():
    raw = request.get_data()
    if not _signature_ok(raw, request.headers.get("Paymongo-Signature", "")):
        logger.warning("[PAYMENTS] webhook rejected: bad or missing signature (ip=%s)", _ip())
        return jsonify({"error": "bad signature"}), 401

    try:
        body = json.loads(raw.decode("utf-8"))
        envelope = body["data"]["attributes"]
        event = envelope.get("type", "")
        resource = envelope.get("data") or {}
        attrs = resource.get("attributes") or {}
    except Exception:
        logger.error("[PAYMENTS] unreadable webhook body\n%s", traceback.format_exc())
        return jsonify({"error": "unreadable"}), 400

    if event not in _HANDLED_EVENTS:
        logger.info("[PAYMENTS] webhook %s ignored", event)
        return jsonify({"ok": True, "ignored": event}), 200

    order = _find_order(resource, attrs)
    if not order:
        logger.warning("[PAYMENTS] webhook %s matched no order (resource %s)",
                       event, resource.get("id"))
        # 200 on purpose: nothing here will change on a retry, and a 4xx would
        # have PayMongo redelivering it for days.
        return jsonify({"ok": True, "matched": False}), 200

    paid = _paid_amount(attrs)
    if paid and paid < order["amount"]:
        db_execute(
            "UPDATE license_orders SET status = 'underpaid', note = %s WHERE reference = %s",
            (f"paid {paid} of {order['amount']} centavos", order["reference"]),
        )
        logger.error("[PAYMENTS] %s underpaid: %s of %s centavos",
                     order["reference"], paid, order["amount"])
        return jsonify({"ok": True, "underpaid": True}), 200

    # Claim the order in one statement: PayMongo can deliver the same event
    # more than once (and two events can describe one payment), and only the
    # first UPDATE to move it off 'pending' gets a row back.
    claimed = db_execute(
        "UPDATE license_orders SET status = 'paid', paid_at = %s, payment_id = %s"
        " WHERE reference = %s AND status IN ('pending', 'underpaid') RETURNING *",
        (int(time.time()), resource.get("id"), order["reference"]),
        fetch="one",
    )
    if not claimed:
        logger.info("[PAYMENTS] %s already fulfilled, webhook ignored", order["reference"])
        return jsonify({"ok": True, "duplicate": True}), 200

    try:
        _fulfil(claimed, attrs)
    except Exception:
        # The money is in and the order is marked paid, so the user must not be
        # left with nothing: put it back to pending so the next delivery (or a
        # manual replay from the PayMongo dashboard) tries again.
        db_execute(
            "UPDATE license_orders SET status = 'pending', note = %s WHERE reference = %s",
            ("fulfilment failed, will retry", claimed["reference"]),
        )
        logger.error("[PAYMENTS] fulfilment of %s failed\n%s",
                     claimed["reference"], traceback.format_exc())
        return jsonify({"error": "fulfilment failed"}), 500

    return jsonify({"ok": True}), 200


def _find_order(resource, attrs):
    """The order this event belongs to: by the reference we put in metadata,
    then the reference number on the session, then the checkout id."""
    metadata = attrs.get("metadata") or {}
    for candidate in (metadata.get("reference"), attrs.get("reference_number")):
        if candidate:
            row = db_execute(
                "SELECT * FROM license_orders WHERE reference = %s", (str(candidate).strip(),),
                fetch="one",
            )
            if row:
                return row
    if resource.get("id"):
        return db_execute(
            "SELECT * FROM license_orders WHERE checkout_id = %s", (resource["id"],), fetch="one"
        )
    return None


def _fulfil(order, attrs):
    """Grant the time that was paid for. Raises on failure — the caller puts the
    order back to pending so a redelivery retries."""
    email = _billing_email(attrs) or order.get("email")
    name = _billing_name(attrs)

    if order["mode"] == "renew":
        expires_at, problem = _add_days(order["license_key"], order["days"])
        if problem:
            raise RuntimeError(f"cannot renew {order['license_key']}: {problem}")
        db_execute(
            "UPDATE license_orders SET email = %s, note = %s WHERE reference = %s",
            (email, f"renewed to {expires_at}", order["reference"]),
        )
        logger.info("[PAYMENTS] %s renewed %s (+%s days)",
                    order["reference"], order["license_key"], order["days"])
        sent = _send_renewal_email(email, order["license_key"], expires_at)
    else:
        key = order["license_key"] or _new_key()
        db_execute(
            "INSERT INTO licenses (key, days, activated_at, device_id, customer_name)"
            " VALUES (%s, %s, NULL, NULL, %s) ON CONFLICT (key) DO NOTHING",
            (key, order["days"], name or email),
        )
        db_execute(
            "UPDATE license_orders SET license_key = %s, email = %s WHERE reference = %s",
            (key, email, order["reference"]),
        )
        logger.info("[PAYMENTS] %s issued %s (%s days)", order["reference"], key, order["days"])
        sent = _send_key_email(email, key)

    if not sent.get("ok"):
        # The license is granted either way — the app picks it up by polling —
        # so a failed email is logged, not raised.
        logger.error("[PAYMENTS] %s: could not email %s: %s",
                     order["reference"], email, sent.get("error"))


# --------------------------------------------------------------------------
# Routes — the two pages the browser lands on
# --------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shiro NC</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#0b0b0c; color:#e8e8ea;
         font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; padding:24px; }}
  .card {{ width:100%; max-width:420px; background:#17171a; border:1px solid #2b2b2f;
          border-radius:16px; padding:36px 28px; text-align:center; }}
  .mark {{ font-size:12px; letter-spacing:.18em; color:#8a8a90; margin:0 0 20px; }}
  h1 {{ font-size:21px; margin:0 0 12px; }}
  p {{ font-size:14px; line-height:1.65; color:#b4b4ba; margin:0; }}
  .tick {{ font-size:34px; margin:0 0 14px; }}
</style></head>
<body><div class="card">
  <p class="mark">SHIRO NC</p>
  <p class="tick">{icon}</p>
  <h1>{title}</h1>
  <p>{body}</p>
</div></body></html>
"""


@payments_bp.route("/pay/thanks", methods=["GET"])
def pay_thanks():
    return _PAGE.format(
        icon="✓",
        title="Payment received",
        body="You can close this tab and go back to Shiro NC — it unlocks on its own "
             "within a few seconds. Your license key is also on its way to your email.",
    ), 200, {"Content-Type": "text/html; charset=utf-8"}


@payments_bp.route("/pay/cancelled", methods=["GET"])
def pay_cancelled():
    return _PAGE.format(
        icon="—",
        title="Checkout cancelled",
        body="Nothing was charged. You can close this tab and start again from the app "
             "whenever you like.",
    ), 200, {"Content-Type": "text/html; charset=utf-8"}
