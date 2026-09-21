def render_onboarding_email(version, download_url, license_key=None):
    key_block = f"""
              <tr>
                <td style="padding-top: 28px;">
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td align="center"
                          style="background:#3D3D40; border:1px solid #4A4A4E; border-radius:8px;
                                 padding:14px 20px;">
                        <p style="margin:0 0 4px; font-size:11px; letter-spacing:0.08em;
                                  text-transform:uppercase; color:#8A8A90; font-family:Arial, sans-serif;">
                          Your license key
                        </p>
                        <p style="margin:0; font-family:'SF Mono','Cascadia Code','Roboto Mono',Consolas,monospace;
                                  font-size:16px; letter-spacing:0.03em; color:#FFFFFF; font-weight:700;">
                          {license_key}
                        </p>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
    """ if license_key else ""

    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Welcome to Shiro NC</title>
</head>
<body style="margin:0; padding:0;
             background: linear-gradient(180deg, #0B0B0C 0%, #4A4A4E 55%, #E8E8EA 100%);">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td align="center" style="padding: 48px 16px;">

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;">

          <!-- Brand mark -->
          <tr>
            <td align="center" style="padding-bottom:24px;">
              <table role="presentation" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="padding-right:10px;">
                    <svg width="40" height="20" viewBox="0 0 48 24" xmlns="http://www.w3.org/2000/svg">
                      <rect x="0"  y="9"  width="3" height="6"  fill="#FFFFFF"></rect>
                      <rect x="6"  y="4"  width="3" height="16" fill="#E8E8EA"></rect>
                      <rect x="12" y="0"  width="3" height="24" fill="#FFFFFF"></rect>
                      <rect x="18" y="6"  width="3" height="12" fill="#E8E8EA"></rect>
                      <rect x="24" y="10" width="3" height="4"  fill="#FFFFFF"></rect>
                      <rect x="30" y="2"  width="3" height="20" fill="#E8E8EA"></rect>
                      <rect x="36" y="7"  width="3" height="10" fill="#FFFFFF"></rect>
                      <rect x="42" y="10" width="3" height="4"  fill="#E8E8EA"></rect>
                    </svg>
                  </td>
                  <td>
                    <span style="font-family:Arial, sans-serif; font-size:15px; font-weight:700;
                                 color:#FFFFFF; letter-spacing:0.01em;">SHIRO NC</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Card -->
          <tr>
            <td style="background:#2A2A2D; border:1px solid #404044; border-radius:16px;
                       padding:40px 36px; box-shadow: 0 20px 60px rgba(0,0,0,0.35);">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0">

                <tr>
                  <td>
                    <p style="margin:0 0 6px; font-family:Arial, sans-serif; font-size:12px;
                              letter-spacing:0.08em; text-transform:uppercase; color:#8A8A90;">
                      You're in
                    </p>
                    <h1 style="margin:0 0 16px; font-family:Arial, sans-serif; font-size:26px;
                               font-weight:700; color:#F1F1F3; letter-spacing:-0.01em;">
                      Welcome to Shiro NC
                    </h1>
                    <p style="margin:0; font-family:Arial, sans-serif; font-size:14.5px;
                              line-height:1.65; color:#ACACB2;">
                      Real-time noise cancellation for your calls and recordings —
                      no extra hardware, no fiddling with mic settings. Install it,
                      pick your mic, and background noise is gone.
                    </p>
                  </td>
                </tr>

                <!-- Feature strip -->
                <tr>
                  <td style="padding-top:24px;">
                    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                      <tr>
                        <td style="padding:14px 0; border-top:1px solid #404044; font-family:Arial, sans-serif;
                                   font-size:13.5px; color:#F1F1F3;">
                          <span style="color:#FFFFFF; font-weight:700;">·</span>&nbsp; Works with any mic, in any app
                        </td>
                      </tr>
                      <tr>
                        <td style="padding:14px 0; border-top:1px solid #404044; font-family:Arial, sans-serif;
                                   font-size:13.5px; color:#F1F1F3;">
                          <span style="color:#FFFFFF; font-weight:700;">·</span>&nbsp; Real-time — nothing to render or wait for
                        </td>
                      </tr>
                      <tr>
                        <td style="padding:14px 0; border-top:1px solid #404044; border-bottom:1px solid #404044;
                                   font-family:Arial, sans-serif; font-size:13.5px; color:#F1F1F3;">
                          <span style="color:#FFFFFF; font-weight:700;">·</span>&nbsp; One license, one device — activates in seconds
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>

                <!-- Installer note -->
                <tr>
                  <td align="center" style="padding-top:32px;">
                    <p style="margin:0; font-family:Arial, sans-serif; font-size:13.5px;
                              line-height:1.5; font-weight:700; color:#F1F1F3;">
                      Click Yes when VB-Audio displays its prompt during installation
                    </p>
                  </td>
                </tr>

                <!-- CTA -->
                <tr>
                  <td align="center" style="padding-top:16px;">
                    <table role="presentation" cellpadding="0" cellspacing="0">
                      <tr>
                        <td align="center" style="border-radius:8px; background:#E8E8EA;">
                          <a href="{download_url}"
                             style="display:inline-block; padding:15px 36px; font-family:Arial, sans-serif;
                                    font-size:14.5px; font-weight:700; color:#14020D; text-decoration:none;
                                    letter-spacing:0.01em;">
                            Download Shiro NC →
                          </a>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>

                {key_block}

              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td align="center" style="padding-top:24px;">
              <p style="margin:0; font-family:Arial, sans-serif; font-size:12px; line-height:1.6; color:#111;">
                If the button doesn't work, copy this link:<br>
                <a href="{download_url}" style="color:#111;">{download_url}</a>
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
