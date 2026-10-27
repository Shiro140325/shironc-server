"""End-to-end test of payments.py against sqlite + a stubbed PayMongo.

Run it with:  python test_payments.py
Needs flask + requests; no database, no network and no PayMongo account.
Exits non-zero if anything regressed.
"""
import base64, hashlib, hmac, json, os, re, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))  # next to payments.py
sys.path.insert(0, HERE)

os.environ["PAYMONGO_SECRET_KEY"] = "sk_test_abc123"
os.environ["PAYMONGO_WEBHOOK_SECRET"] = "whsk_test_secret"
os.environ["NEON_DATABASE_URL"] = "postgres://unused"

import payments

# ---------------------------------------------------------------- fake DB
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("""CREATE TABLE licenses (
    key TEXT PRIMARY KEY, days INTEGER, activated_at INTEGER, device_id TEXT,
    customer_name TEXT, unique_identifier TEXT, app_version TEXT, last_seen INTEGER,
    logs_requested_at INTEGER)""")


def fake_db(query, params=None, fetch=None, retries=2):
    q = query.replace("%s", "?").replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    cur = conn.execute(q, tuple(params or ()))
    if fetch == "one":
        row = cur.fetchone()
        out = dict(row) if row else None
    elif fetch == "all":
        out = [dict(r) for r in cur.fetchall()]
    else:
        out = None
    conn.commit()
    return out


payments.db_execute = fake_db

# ------------------------------------------------------------ stub PayMongo
created = []


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, json.dumps(body)

    def json(self):
        return self._body


def fake_post(url, headers=None, json=None, timeout=None):
    created.append(json)
    auth = headers["Authorization"].split()[1]
    assert base64.b64decode(auth).decode() == "sk_test_abc123:", "basic auth is key + ':'"
    ref = json["data"]["attributes"]["reference_number"]
    return Resp(200, {"data": {"id": "cs_" + ref, "attributes": {
        "checkout_url": "https://checkout.paymongo.com/" + ref}}})


payments.requests.post = fake_post

emails = []
payments._send_key_email = lambda to, key: (emails.append(("new", to, key)), {"ok": True})[1]
payments._send_renewal_email = lambda to, key, exp: (emails.append(("renew", to, key)), {"ok": True})[1]

# ------------------------------------------------------------------ app
from flask import Flask

app = Flask(__name__)
payments.init_payments(app)
app.register_blueprint(payments.payments_bp)
client = app.test_client()
DEVICE = "AAAAAA-BBBBBB-CCCCCC"
fails = []


def check(label, ok, extra=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (f"   {extra}" if extra and not ok else ""))
    if not ok:
        fails.append(label)


def sign(body, secret="whsk_test_secret", ts=None):
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},te={mac},li=notthisone"


def webhook(event, resource, header=None, body_override=None):
    body = body_override or json.dumps({"data": {"id": "evt_1", "attributes": {
        "type": event, "data": resource}}}).encode()
    return client.post("/paymongo/webhook", data=body, headers={
        "Paymongo-Signature": header if header is not None else sign(body),
        "Content-Type": "application/json"})


print("\n=== new purchase ===")
r = client.post("/purchase", json={"mode": "new", "device": DEVICE})
check("POST /purchase 200", r.status_code == 200, r.get_data(as_text=True))
ref = r.get_json()["reference"]
check("returns a checkout_url", r.get_json()["checkout_url"].endswith(ref))
check("amount is 12900 centavos", created[-1]["data"]["attributes"]["line_items"][0]["amount"] == 12900)
check("metadata carries the reference", created[-1]["data"]["attributes"]["metadata"]["reference"] == ref)

r = client.get(f"/purchase/status?reference={ref}&device={DEVICE}")
check("status is pending before payment", r.get_json()["status"] == "pending")

r = webhook("checkout_session.payment.paid", {"id": "cs_" + ref, "attributes": {
    "reference_number": ref, "metadata": {"reference": ref, "mode": "new", "device": DEVICE},
    "billing": {"email": "buyer@example.com", "name": "A Buyer"},
    "payments": [{"id": "pay_1", "attributes": {"amount": 12900, "status": "paid"}}]}})
check("webhook accepted", r.status_code == 200, r.get_data(as_text=True))

r = client.get(f"/purchase/status?reference={ref}&device={DEVICE}")
body = r.get_json()
check("status is paid", body["status"] == "paid", str(body))
key = body.get("key", "")
check("a key was issued", bool(re.fullmatch(r"[0-9A-F]{4}(-[0-9A-F]{4}){3}", key or "")), key)
lic = fake_db("SELECT * FROM licenses WHERE key = ?", (key,), fetch="one")
check("license is 30 days, unactivated", lic["days"] == 30 and lic["activated_at"] is None, str(lic))
check("key was emailed to the payer", emails[-1] == ("new", "buyer@example.com", key), str(emails))
check("customer name recorded", lic["customer_name"] == "A Buyer")

r = client.get(f"/purchase/status?reference={ref}&device=SOMEONE-ELSE")
check("another device cannot collect the key", r.status_code == 404)

print("\n=== duplicate webhook delivery ===")
before = len(fake_db("SELECT key FROM licenses", fetch="all"))
r = webhook("checkout_session.payment.paid", {"id": "cs_" + ref, "attributes": {
    "reference_number": ref, "metadata": {"reference": ref},
    "payments": [{"id": "pay_1", "attributes": {"amount": 12900, "status": "paid"}}]}})
after = len(fake_db("SELECT key FROM licenses", fetch="all"))
check("redelivery is a no-op", r.get_json().get("duplicate") is True and before == after)

print("\n=== renewal of a live license ===")
now = int(time.time())
fake_db("INSERT INTO licenses (key, days, activated_at, device_id) VALUES (?,?,?,?)",
        ("LIVE-LIVE-LIVE-LIVE", 30, now - 20 * 86400, DEVICE))
r = client.post("/purchase", json={"mode": "renew", "key": "LIVE-LIVE-LIVE-LIVE", "device": DEVICE})
check("renew checkout opens", r.status_code == 200, r.get_data(as_text=True))
ref2 = r.get_json()["reference"]
webhook("checkout_session.payment.paid", {"id": "cs_" + ref2, "attributes": {
    "reference_number": ref2, "billing": {"email": "old@example.com"},
    "payments": [{"id": "pay_2", "attributes": {"amount": 12900, "status": "paid"}}]}})
lic = fake_db("SELECT * FROM licenses WHERE key = ?", ("LIVE-LIVE-LIVE-LIVE",), fetch="one")
check("days stacked on the 10 left (30 -> 60)", lic["days"] == 60, str(lic))
check("activated_at untouched", lic["activated_at"] == now - 20 * 86400)
check("renewal emailed", emails[-1][0] == "renew")
r = client.get(f"/purchase/status?reference={ref2}&device={DEVICE}")
check("status reports the new expiry",
      r.get_json()["expires_at"] == now - 20 * 86400 + 60 * 86400, str(r.get_json()))

print("\n=== renewal of a lapsed license ===")
fake_db("INSERT INTO licenses (key, days, activated_at, device_id) VALUES (?,?,?,?)",
        ("DEAD-DEAD-DEAD-DEAD", 30, now - 90 * 86400, DEVICE))
r = client.post("/purchase", json={"mode": "renew", "key": "DEAD-DEAD-DEAD-DEAD", "device": DEVICE})
ref3 = r.get_json()["reference"]
webhook("checkout_session.payment.paid", {"id": "cs_" + ref3, "attributes": {
    "reference_number": ref3, "payments": [{"id": "p3", "attributes": {"amount": 12900, "status": "paid"}}]}})
lic = fake_db("SELECT * FROM licenses WHERE key = ?", ("DEAD-DEAD-DEAD-DEAD",), fetch="one")
check("lapsed license restarts at 30 days from now",
      lic["days"] == 30 and abs(lic["activated_at"] - now) < 10, str(lic))

print("\n=== refusals ===")
r = client.post("/purchase", json={"mode": "renew", "key": "NOPE-NOPE-NOPE-NOPE", "device": DEVICE})
check("unknown key for renewal -> 404", r.status_code == 404)
fake_db("INSERT INTO licenses (key, days, activated_at, device_id) VALUES (?,?,?,?)",
        ("LIFE-LIFE-LIFE-LIFE", 0, now, DEVICE))
r = client.post("/purchase", json={"mode": "renew", "key": "LIFE-LIFE-LIFE-LIFE", "device": DEVICE})
check("lifetime license cannot be renewed -> 400", r.status_code == 400, r.get_data(as_text=True))
r = client.post("/purchase", json={"mode": "renew", "key": "LIVE-LIVE-LIVE-LIVE", "device": "OTHER-DEVICE"})
check("renewing from another device -> 403", r.status_code == 403)

print("\n=== webhook signature ===")
r = webhook("checkout_session.payment.paid", {"id": "cs_x", "attributes": {}}, header="t=1,te=bad,li=bad")
check("bad signature -> 401", r.status_code == 401)
r = webhook("checkout_session.payment.paid", {"id": "cs_x", "attributes": {}}, header="")
check("missing signature -> 401", r.status_code == 401)
body = json.dumps({"data": {"attributes": {"type": "checkout_session.payment.paid",
                                           "data": {"id": "cs_x", "attributes": {}}}}}).encode()
r = webhook(None, None, header=sign(body, ts=int(time.time()) - 30 * 86400), body_override=body)
check("stale timestamp -> 401", r.status_code == 401)
r = webhook("payment.failed", {"id": "pay_x", "attributes": {}})
check("unhandled event -> 200 ignored", r.status_code == 200 and r.get_json().get("ignored"))
r = webhook("checkout_session.payment.paid", {"id": "cs_unknown", "attributes": {"reference_number": "SNCZZZZ"}})
check("unmatched order -> 200, not retried", r.status_code == 200 and r.get_json()["matched"] is False)

print("\n=== underpayment ===")
r = client.post("/purchase", json={"mode": "new", "device": DEVICE})
ref4 = r.get_json()["reference"]
webhook("checkout_session.payment.paid", {"id": "cs_" + ref4, "attributes": {
    "reference_number": ref4, "payments": [{"id": "p4", "attributes": {"amount": 5000, "status": "paid"}}]}})
r = client.get(f"/purchase/status?reference={ref4}&device={DEVICE}")
check("underpaid grants nothing", r.get_json()["status"] == "underpaid", str(r.get_json()))

print("\n=== fulfilment failure is retried ===")
r = client.post("/purchase", json={"mode": "new", "device": DEVICE})
ref5 = r.get_json()["reference"]
real_new_key = payments._new_key
payments._new_key = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
r = webhook("checkout_session.payment.paid", {"id": "cs_" + ref5, "attributes": {
    "reference_number": ref5, "payments": [{"id": "p5", "attributes": {"amount": 12900, "status": "paid"}}]}})
check("failure answers 500 so PayMongo redelivers", r.status_code == 500)
check("order went back to pending",
      fake_db("SELECT status FROM license_orders WHERE reference = ?", (ref5,), fetch="one")["status"] == "pending")
payments._new_key = real_new_key
r = webhook("checkout_session.payment.paid", {"id": "cs_" + ref5, "attributes": {
    "reference_number": ref5, "payments": [{"id": "p5", "attributes": {"amount": 12900, "status": "paid"}}]}})
r = client.get(f"/purchase/status?reference={ref5}&device={DEVICE}")
check("redelivery fulfils it", r.get_json()["status"] == "paid" and r.get_json().get("key"), str(r.get_json()))

print("\n=== payment methods the account has not activated ===")
# PayMongo refuses the whole request by name when a method is not activated,
# so the checkout has to walk down to one that is.
def picky_post(accepts):
    def post(url, headers=None, json=None, timeout=None):
        asked = json["data"]["attributes"]["payment_method_types"]
        if asked != accepts:
            return Resp(400, {"errors": [{"detail":
                f"payment_method_types {asked} is not enabled on this account"}]})
        ref = json["data"]["attributes"]["reference_number"]
        return Resp(200, {"data": {"id": "cs_" + ref, "attributes": {
            "checkout_url": "https://checkout.paymongo.com/" + ref}}})
    return post

payments.requests.post = picky_post(["gcash"])
r = client.post("/purchase", json={"mode": "new", "device": DEVICE})
check("falls through qrph to a method that is activated", r.status_code == 200,
      r.get_data(as_text=True))

payments.requests.post = picky_post(["nothing_we_try"])
r = client.post("/purchase", json={"mode": "new", "device": DEVICE})
check("all refused -> 502 naming what was tried",
      r.status_code == 502 and "payment methods" in r.get_json()["error"],
      r.get_data(as_text=True))
check("and no order row is left behind for it",
      len(fake_db("SELECT id FROM license_orders WHERE status = 'pending'", fetch="all")) == 1,
      str(fake_db("SELECT reference, status FROM license_orders", fetch="all")))

payments.requests.post = fake_post

print("\n=== landing pages ===")
r = client.get("/pay/thanks")
check("/pay/thanks renders", r.status_code == 200 and b"Payment received" in r.data)
r = client.get("/pay/cancelled")
check("/pay/cancelled renders", r.status_code == 200 and b"Nothing was charged" in r.data)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
