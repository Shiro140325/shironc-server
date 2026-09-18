def render_onboarding_email(product_name, version, download_url):
    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
      <h2 style="color:#111;">Welcome to {product_name}</h2>
      <p style="color:#444; line-height:1.5;">
        Thanks for getting {product_name}. Real-time noise cancellation for your calls and recordings — no extra hardware needed.
      </p>
      <p style="color:#888; font-size:13px;">Version {version}</p>
      <div style="text-align:center; margin: 32px 0;">
        <a href="{download_url}"
           style="background:#1a1a1a; color:#fff; padding:14px 28px; border-radius:6px;
                  text-decoration:none; font-weight:bold; display:inline-block;">
          Download {product_name}
        </a>
      </div>
      <p style="color:#aaa; font-size:12px;">If the button doesn't work, copy this link: {download_url}</p>
    </div>
    """
