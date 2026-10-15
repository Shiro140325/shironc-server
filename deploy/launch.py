"""Start the three apps and nginx in one container, restarting any app that exits.

Environment variables:
  shironc.com uses the service's own environment variables, as before.
  smm-panel and the Shiro chat server each read their own secret file instead,
  /etc/secrets/smm.env and /etc/secrets/chat.env (Render "Secret Files", in
  .env format), so the same name (DATABASE_URL, RESEND_API_KEY, ...) can hold a
  different value per app. They don't see shironc's variables.

Users: this launcher runs as root (to read the secret files and start nginx),
but each app runs as its own unprivileged user (app-nc, app-smm, app-chat,
made in the Dockerfile). A bug in one app then can't read another app's
secrets: the secret files are made readable by root only once read, and a
process's environment can only be read by its own user.
"""

import os
import pwd
import signal
import subprocess
import sys
import threading
import time

from dotenv import dotenv_values

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_DIR = os.environ.get("SECRETS_DIR", "/etc/secrets")
PORT = os.environ.get("PORT", "10000")

# Passed to every app: what a process needs to run, plus Render's own info.
SHARED = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONUNBUFFERED")


def lock_down(path):
    """Make a secret file readable by root only (it may be on a read-only mount)."""
    try:
        os.chown(path, 0, 0)
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"[launch] could not lock down {path} ({e}); apps may be able to read it", flush=True)


def own_env(name):
    """The environment for smm or chat: the basics plus its secret file."""
    env = {k: v for k, v in os.environ.items() if k in SHARED or k.startswith("RENDER")}
    path = os.path.join(SECRETS_DIR, f"{name}.env")
    if os.path.exists(path):
        env.update({k: v for k, v in dotenv_values(path).items() if v is not None})
        lock_down(path)
    else:
        print(f"[launch] {path} is missing: {name} starts without its settings", flush=True)
    return env


APPS = {
    "nc": dict(
        cwd="/srv/nc",
        env=dict(os.environ),
        user="app-nc",
        cmd=["/opt/venv-nc/bin/gunicorn", "server:app", "--bind", "127.0.0.1:8001",
             "--worker-class", "gthread", "--workers", os.environ.get("NC_WORKERS", "1"),
             "--threads", "16", "--timeout", "30"],
    ),
    "smm": dict(
        cwd="/srv/smm",
        env=own_env("smm"),
        user="app-smm",
        cmd=["/opt/venv-smm/bin/uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8002"],
    ),
    "chat": dict(
        cwd="/srv/chat",
        env={**own_env("chat"), "PORT": "8003"},
        user="app-chat",
        cmd=["/usr/local/bin/node", "src/index.js"],
    ),
}


def as_user(app):
    """Popen arguments that run [app] as its own user, with a private home."""
    if os.getuid() != 0:   # e.g. running locally: nothing to switch from
        return {}
    pw = pwd.getpwnam(app["user"])
    home = f"/tmp/home-{app['user']}"
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.chown(home, pw.pw_uid, pw.pw_gid)
    app["env"] = {**app["env"], "HOME": home, "USER": app["user"]}
    return dict(user=pw.pw_uid, group=pw.pw_gid, extra_groups=[], umask=0o077)

procs = {}
stopping = False


def supervise(name, app):
    """Run one app forever, waiting a little longer after each quick crash."""
    delay = 1
    drop = as_user(app)
    while not stopping:
        started = time.monotonic()
        procs[name] = subprocess.Popen(app["cmd"], cwd=app["cwd"], env=app["env"], **drop)
        code = procs[name].wait()
        if stopping:
            return
        delay = 1 if time.monotonic() - started > 60 else min(delay * 2, 30)
        print(f"[launch] {name} exited with {code}; restarting in {delay}s", flush=True)
        time.sleep(delay)


def stop(*_):
    """Ask every process to exit; main() waits for them once nginx is gone."""
    global stopping
    stopping = True
    for p in list(procs.values()):
        if p.poll() is None:
            p.terminate()


def wait_all():
    deadline = time.monotonic() + 20
    for p in list(procs.values()):
        try:
            p.wait(max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    with open(os.path.join(HERE, "nginx.conf")) as f:
        conf = f.read().replace("__PORT__", PORT)
    with open("/tmp/nginx.conf", "w") as f:
        f.write(conf)

    for name, app in APPS.items():
        threading.Thread(target=supervise, args=(name, app), daemon=True).start()

    # nginx is the front door: if it dies, let Render restart the container.
    procs["nginx"] = subprocess.Popen(["nginx", "-c", "/tmp/nginx.conf", "-g", "daemon off;"])
    code = procs["nginx"].wait()
    if not stopping:
        print(f"[launch] nginx exited with {code}", flush=True)
    stop()
    wait_all()


if __name__ == "__main__":
    main()
