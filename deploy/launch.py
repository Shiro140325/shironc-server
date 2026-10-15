"""Start the three apps and nginx in one container, restarting any app that exits.

Environment variables:
  shironc.com uses the service's own environment variables, as before.
  smm-panel and the Shiro chat server each get their own settings, so the same
  name (DATABASE_URL, RESEND_API_KEY, ...) can hold a different value per app.
  They come from the service's environment, which only this launcher (root)
  can read:
    SMM_ENV / CHAT_ENV   the app's whole .env file pasted as one value, and/or
    SMM__NAME / CHAT__NAME   one setting each (becomes NAME; wins over *_ENV)
  None of these reach shironc, and each app sees only its own.
  Older setup: /etc/secrets/smm.env and /etc/secrets/chat.env (Render "Secret
  Files"). Still read when there's nothing in the environment for that app,
  but Render mounts them readable by every user, so prefer the variables.

Users: this launcher runs as root (to read the settings and start nginx), but
each app runs as its own unprivileged user (app-nc, app-smm, app-chat, made in
the Dockerfile), and a process's environment can only be read by its own user.
So a bug in one app can't read another app's secrets.
"""

import io
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


def own_env(name):
    """The environment for smm or chat: the basics plus its own settings."""
    env = {k: v for k, v in os.environ.items() if k in SHARED or k.startswith("RENDER")}
    pasted = os.environ.get(f"{name.upper()}_ENV", "")
    prefix = f"{name.upper()}__"
    single = {k[len(prefix):]: v for k, v in os.environ.items() if k.startswith(prefix) and len(k) > len(prefix)}
    path = os.path.join(SECRETS_DIR, f"{name}.env")
    if pasted or single:
        env.update({k: v for k, v in dotenv_values(stream=io.StringIO(pasted)).items() if v is not None})
        env.update(single)
        if os.path.exists(path):
            print(f"[launch] {name}: using the environment; {path} is ignored and can be deleted", flush=True)
    elif os.path.exists(path):
        env.update({k: v for k, v in dotenv_values(path).items() if v is not None})
        print(f"[launch] {name}: read {path}, which every app can read; move it to {name.upper()}_ENV", flush=True)
    else:
        print(f"[launch] {name}: no settings found, starting without them", flush=True)
    return env


def is_other_apps(key):
    return key in ("SMM_ENV", "CHAT_ENV") or key.startswith(("SMM__", "CHAT__"))


APPS = {
    "nc": dict(
        cwd="/srv/nc",
        env={k: v for k, v in os.environ.items() if not is_other_apps(k)},
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
        procs[name] = subprocess.Popen(["/bin/sh", os.path.join(HERE, "run_app.sh"), *app["cmd"]],
                                       cwd=app["cwd"], env=app["env"], stdin=subprocess.PIPE, **drop)
        code = procs[name].wait()
        procs[name].stdin.close()
        if stopping:
            return
        delay = 1 if time.monotonic() - started > 60 else min(delay * 2, 30)
        print(f"[launch] {name} exited with {code}; restarting in {delay}s", flush=True)
        time.sleep(delay)


def stop(*_):
    """Ask every process to exit; main() waits for them once nginx is gone."""
    global stopping
    stopping = True
    for name, p in list(procs.items()):
        if p.poll() is not None:
            continue
        if name == "nginx":
            p.terminate()
        else:
            p.stdin.close()   # run_app.sh stops the app (root can't signal it)


def wait_all():
    deadline = time.monotonic() + 20
    for p in list(procs.values()):
        try:
            p.wait(max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except OSError:
                pass   # another user's process; it goes when the container does


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
