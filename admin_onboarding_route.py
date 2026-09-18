# --- Add near the top of admin.py, with the other imports ---
import os
import resend
from email_templates import render_onboarding_email

resend.api_key = os.environ["RESEND_API_KEY"]


# --- Add this route inside admin.py, alongside your other @admin_bp routes ---
@admin_bp.route("/admin/send-onboarding", methods=["POST"])
def send_onboarding():
    data = request.get_json()
    to_email = data.get("email")
    product_name = data.get("product_name", "ShiroNC")

    # Reuse whatever function your Release tab already uses to read
    # the stored latest_version / object_key — replace these two lines
    # with your actual lookups instead of duplicating the source of truth.
    version = data.get("version") or get_latest_version()
    object_key = data.get("object_key") or get_latest_object_key()

    if not to_email:
        return jsonify({"error": "email required"}), 400

    download_url = f"{os.environ['R2_PUBLIC_BASE']}/{object_key}"
    html = render_onboarding_email(product_name, version, download_url)

    try:
        resend.Emails.send({
            "from": os.environ["RESEND_FROM"],
            "to": to_email,
            "subject": f"Welcome to {product_name} — your download is ready",
            "html": html,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "sent"})
