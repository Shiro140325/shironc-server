# One Render service for three sites

This repo's Render service (`shironc-server`) runs all three apps in one Docker container:

| Site | App | Code |
| --- | --- | --- |
| shironc.com (and anything unmatched, e.g. shironc-server.onrender.com) | Flask (gunicorn) | this repo |
| smmshiro.com and its subdomains | FastAPI (uvicorn) | `apps/smm`, submodule of Shiro140325/smm-panel @ `main` |
| shirochat.com (via the `shiro-web` Cloudflare Worker) | Node | `apps/chat/server`, submodule of Shiro140325/Shiro @ `flutter-starter` |

`deploy/launch.py` starts the three apps on 127.0.0.1 (8001, 8002, 8003) and restarts any that
exit. nginx (`deploy/nginx.conf`) listens on `$PORT` and picks the app by the `Host` header.
Requests that carry the `x-shiro-proxy` header (the shirochat Worker adds it to every request)
go to the chat app.

## Settings

- **shironc:** reads the service's own environment variables, as before.
- **smm-panel and chat:** each reads its own Render **Secret File**, `smm.env` and `chat.env`, in
  `.env` format (`NAME=value` per line, multi-line values in quotes). This keeps names like
  `DATABASE_URL` separate per app. These two apps don't see shironc's variables.
- `NC_WORKERS` (optional, default 1): gunicorn workers for shironc (16 threads each). It was 2
  before, cut to 1 to leave memory for the other two apps.

## Deploys

A push to this repo deploys as usual. Pushes to the other two repos reach this service through
`deploy/workflows/deploy-*.yml`, copied into those repos. Each one commits the new submodule
pointer here, and that commit triggers the deploy. To deploy them by hand:

    git submodule update --remote && git commit -am "Update apps" && git push

## Moving it

The image doesn't depend on which Render service runs it. To move everything to another
service, point that service at this repo with the Docker runtime, copy the environment
variables and both secret files, and move the custom domains (plus the Worker's `ORIGIN`).
