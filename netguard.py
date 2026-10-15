"""The visitor's real IP address, and a small in-memory rate limiter.

The visitor's address: requests reach the container through Render's proxy and
then nginx, so X-Forwarded-For ends with "<what Render saw>, <Render's proxy>".
Everything before those two can be made up by the client. When what Render saw
is a Cloudflare address, the request came through Cloudflare and its
CF-Connecting-IP header is the visitor. Otherwise someone reached the origin
directly, and the address Render saw is the visitor (any CF-Connecting-IP they
sent is ignored).
"""
import ipaddress
import threading
import time

# https://www.cloudflare.com/ips/ (changes rarely)
_CLOUDFLARE = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)]


def _is_cloudflare(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _CLOUDFLARE)


def real_ip(headers, remote_addr=None):
    """[headers]: a mapping with .get (Flask's request.headers)."""
    hops = [h.strip() for h in (headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
    seen = hops[-2] if len(hops) >= 2 else (hops[-1] if hops else (remote_addr or "unknown"))
    if _is_cloudflare(seen):
        return (headers.get("CF-Connecting-IP") or seen).strip()
    return seen


class Limiter:
    """At most [limit] hits per key within [window] seconds (this process only)."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self._hits = {}
        self._lock = threading.Lock()

    def _recent(self, key, now):
        recent = [t for t in self._hits.get(key, ()) if now - t < self.window]
        if recent:
            self._hits[key] = recent
        else:
            self._hits.pop(key, None)
        return recent

    def blocked(self, key):
        with self._lock:
            return len(self._recent(key, time.time())) >= self.limit

    def add(self, key):
        with self._lock:
            now = time.time()
            if len(self._hits) > 50_000:   # never grows without bound
                self._hits.clear()
            self._hits[key] = self._recent(key, now) + [now]

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)
