# One container for three sites (see deploy/README.md):
#   shironc.com   -> this repo's Flask server
#   smmshiro.com  -> apps/smm  (smm-panel submodule, FastAPI)
#   shirochat.com -> apps/chat (Shiro submodule, Node server in server/)
# nginx listens on $PORT and routes each request to its app.

# The chat server's node_modules (sharp ships a native binary, so it is
# installed on the same Debian release as the final image).
FROM node:22-bookworm-slim AS chat-deps
WORKDIR /srv/chat
COPY apps/chat/server/package.json apps/chat/server/package-lock.json ./
RUN npm ci --omit=dev

FROM python:3.13-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends nginx-light \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/nginx/sites-enabled/default
COPY --from=chat-deps /usr/local/bin/node /usr/local/bin/node

# Each Python app gets its own virtualenv, so their pins can't clash.
COPY requirements.txt /tmp/nc-requirements.txt
RUN python -m venv /opt/venv-nc && /opt/venv-nc/bin/pip install --no-cache-dir -r /tmp/nc-requirements.txt
COPY apps/smm/requirements.txt /tmp/smm-requirements.txt
RUN python -m venv /opt/venv-smm && /opt/venv-smm/bin/pip install --no-cache-dir -r /tmp/smm-requirements.txt
RUN pip install --no-cache-dir python-dotenv==1.1.1

COPY --from=chat-deps /srv/chat/node_modules /srv/chat/node_modules
COPY apps/chat/server/package.json /srv/chat/
COPY apps/chat/server/src /srv/chat/src
COPY apps/chat/server/public /srv/chat/public
COPY apps/smm /srv/smm
COPY server.py admin.py email_templates.py /srv/nc/
COPY admin_static /srv/nc/admin_static
COPY deploy /srv/deploy

ENV PYTHONUNBUFFERED=1 PORT=10000
CMD ["python", "/srv/deploy/launch.py"]
