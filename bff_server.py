#!/usr/bin/env python3
"""Homelab dashboard BFF (backend-for-frontend).

Serves the static dashboard AND proxies PocketBase server-side so the
browser never talks to the database directly (no CORS, no direct DB
exposure, creds stay on this box). Standard pattern: browser -> Caddy ->
BFF -> PocketBase.
"""
import json, os, socket, threading, time, ipaddress
import urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("BFF_PORT", "18791"))
STATIC_DIR  = os.environ.get("STATIC_DIR", "/home/vidar/homelab-dashboard")
PB_BASE     = os.environ.get("PB_BASE_URL", "").rstrip("/")
STATS_UP    = os.environ.get("STATS_UPSTREAM", "http://127.0.0.1:18790").rstrip("/")

# SSRF policy for /api/probe: scheme must be http/https, port allowlisted,
# and the target must be trusted — a *.tailda8422.ts.net name, localhost,
# or (A+ rule) a private/loopback IP literal that matches the resolved
# address of an allowlisted `host` override. Names are additionally resolved
# and every resolved address must be private (DNS-rebinding guard).
PROBE_SUFFIX = ".tailda8422.ts.net"
PROBE_PORTS = {None, 80, 443, 9119, 18790, 8090}
PROBE_TIMEOUT = 4.0
_DNS_TIMEOUT = 20.0   # cold DNS can stall ~15s; bounded per miss
_DNS_TTL = 300        # seconds a resolution stays cached
_DNS_CACHE = {}       # host -> (expiry_monotonic, is_private)
# Tailscale CGNAT range is this homelab's internal network; ipaddress's
# is_private does not cover it, so allow it explicitly.
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _host_allowed(host):
    """Static host check: trusted suffix or localhost. Used for both the
    url_param host and the host_param override (A+ rule). Bare IP literals
    are rejected here; a private IP-literal target is only accepted through
    the A+ path in _do_probe (literal must equal the resolved host_param)."""
    if not host:
        return False
    h = host.rstrip(".").lower()
    if h.endswith(PROBE_SUFFIX) or h == "localhost":
        return True
    return False


def _resolve_private(host):
    """True iff `host` resolves and every resolved address is private.

    AF_INET only — see _resolved_addresses for why (AAAA black-hole stalls)."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip in _TAILNET):
            return False
    return True


def _resolved_addresses(host):
    """Set of resolved IPs for `host` (empty on unresolvable), private-only.

    Uses AF_INET only: this homelab's names have no AAAA records and
    the upstream DNS black-holes AAAA queries (10-15s timeout), so a
    family-unspecified getaddrinfo stalls the whole probe path on every cache
    miss (orchestrator-verified 15s probe latencies). A/AAAA parity is not a
    goal here; policy is IPv4 private-space plus IPv6 literals parsed inline."""
    out = set()
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
    except Exception:
        return out
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip in _TAILNET:
            out.add(ip)
    return out



def _resolve_private_limited(host):
    """_resolve_private with a wall-clock cap (getaddrinfo ignores urlopen
    timeouts). Results are cached (slow DNS: up to ~15s on cold
    misses); cache misses may block up to _DNS_TIMEOUT once, then hit cache."""
    now = time.monotonic()
    hit = _DNS_CACHE.get(host)
    if hit and hit[0] > now:
        return hit[1]
    box = []
    t = threading.Thread(target=lambda: box.append(_resolve_private(host)), daemon=True)
    t.start()
    t.join(_DNS_TIMEOUT)
    if box:
        _DNS_CACHE[host] = (time.monotonic() + _DNS_TTL, box[0])
        return box[0]
    return False  # unresolved: treated as unhealthy, retried after TTL


def _resolved_addresses_limited(host):
    """_resolved_addresses with the same wall-clock cap + cache as
    _resolve_private_limited. Critical: _do_probe_ip_literal must never do
    raw getaddrinfo — DNS can block 15s+ on cold misses, which
    stalled the whole A+ IP-literal path (orchestrator-verified timeouts).
    A cache miss returns empty -> 400 immediately, no blocking network I/O."""
    now = time.monotonic()
    hit = _DNS_CACHE.get("set:" + host)
    if hit and hit[0] > now:
        return hit[1]
    box = []
    t = threading.Thread(target=lambda: box.append(_resolved_addresses(host)), daemon=True)
    t.start()
    t.join(_DNS_TIMEOUT)
    if box:
        _DNS_CACHE["set:" + host] = (time.monotonic() + _DNS_TTL, box[0])
        return box[0]
    return set()  # unresolved within cap: reject, retried after TTL


def _warm_dns_cache():
    """Pre-populate the resolution cache in the background so the first
    dashboard load after a BFF restart doesn't wait on slow DNS."""
    for host in ("dashboard.hermes.tailda8422.ts.net",
                 "hermes.tailda8422.ts.net"):
        if host in _DNS_CACHE:
            continue
        box = []
        t = threading.Thread(target=lambda h=host: box.append(_resolve_private(h)), daemon=True)
        t.start()
        t.join(_DNS_TIMEOUT)
        if box:
            _DNS_CACHE[host] = (time.monotonic() + _DNS_TTL, box[0])


def _probe_fetch(url):
    """HEAD first, GET fallback on 400/405/501. Returns the upstream status
    int, or None on network error/timeout. Redirects are reported, never
    followed (_NoRedirect turns 3xx into HTTPError)."""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method)
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(req, timeout=PROBE_TIMEOUT) as r:
                r.read(1)  # touch body so error paths surface; discard rest
                return r.status
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (400, 405, 501):
                continue  # HEAD unsupported -> fall back to GET
            e.close()
            return e.code
        except Exception:
            return None
    return None


def _do_probe(url_param, host_param):
    """Validate + probe. Returns (http_status, body_dict) for the BFF reply."""
    try:
        parts = urlsplit(url_param)
        port = parts.port  # raises ValueError on malformed port
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return 400, {"error": "invalid url"}
    if parts.scheme not in ("http", "https"):
        return 400, {"error": "scheme not allowed"}
    if "@" in (parts.netloc or ""):
        return 400, {"error": "userinfo not allowed"}   # user@host SSRF trick
    if port not in PROBE_PORTS:
        return 400, {"error": "port not allowed"}
    if not _host_allowed(host):
        # Bare IP literals are rejected here; the A+ path below may re-admit
        # a private literal that matches the resolved host_param.
        if host_param:
            return _do_probe_ip_literal(url_param, parts, host_param)
        return 400, {"error": "target not allowed"}
    if host_param and not _host_allowed(host_param.rstrip(".").lower()):
        return 400, {"error": "host override not allowed"}
    if not _resolve_private_limited(host):
        # valid input, unresolvable/rebinding target -> unhealthy, not an error
        return 200, {"ok": False, "status": None}
    upstream = _probe_fetch(url_param)
    if upstream is None:
        return 200, {"ok": False, "status": None}       # network error/timeout
    return 200, {"ok": 200 <= upstream < 400, "status": upstream}  # 2xx+3xx healthy


def _do_probe_ip_literal(url_param, parts, host_param):
    """A+ rule: admit an IP-literal url_param target only when (a) host_param
    is allowlisted, (b) the literal is private/loopback, and (c) it equals an
    address host_param actually resolves to (no second resolution)."""
    hp = host_param.rstrip(".").lower()
    if not _host_allowed(hp):
        return 400, {"error": "target not allowed"}
    try:
        lit = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return 400, {"error": "target not allowed"}
    if not (lit.is_private or lit.is_loopback or lit.is_link_local
            or lit in _TAILNET):
        return 400, {"error": "target not allowed"}
    if lit not in _resolved_addresses_limited(hp):
        return 400, {"error": "target not allowed"}
    upstream = _probe_fetch(url_param)
    if upstream is None:
        return 200, {"ok": False, "status": None}       # network error/timeout
    return 200, {"ok": 200 <= upstream < 400, "status": upstream}  # 2xx+3xx healthy

PB_ADMIN_EMAIL = os.environ.get("PB_ADMIN_EMAIL", "")
PB_ADMIN_PASS  = os.environ.get("PB_ADMIN_PASSWORD", "")

_token = {"value": None}

MIME = {".html":"text/html; charset=utf-8", ".js":"text/javascript; charset=utf-8",
        ".css":"text/css; charset=utf-8", ".json":"application/json",
        ".svg":"image/svg+xml", ".png":"image/png", ".ico":"image/x-icon",
        ".map":"application/json", ".txt":"text/plain; charset=utf-8"}

def _log(*a):
    if os.environ.get("BFF_VERBOSE"): print(*a, flush=True)

def pb_token():
    """Superuser token for PB >=0.23; None when creds are absent (anon mode)."""
    if _token["value"]:
        return _token["value"]
    if not (PB_BASE and PB_ADMIN_EMAIL and PB_ADMIN_PASS):
        return None
    try:
        req = urllib.request.Request(
            PB_BASE + "/api/collections/_superusers/auth-with-password",
            data=json.dumps({"identity": PB_ADMIN_EMAIL, "password": PB_ADMIN_PASS}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=6) as r:
            _token["value"] = json.load(r).get("token")
            _log("pb: obtained superuser token")
    except Exception as e:
        _log("pb: auth failed:", e)
    return _token["value"]

def drop_token():
    _token["value"] = None

def proxy(upstream_url, method, body, extra_headers=None, inject_auth=False):
    headers = {}
    if extra_headers:
        for k in ("Content-Type", "Accept"):
            if k in extra_headers: headers[k] = extra_headers[k]
    if body is not None and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    if inject_auth:
        tok = pb_token()
        if tok: headers["Authorization"] = tok
    req = urllib.request.Request(upstream_url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        if e.code == 401 and inject_auth:      # stale token -> retry once fresh
            drop_token(); tok = pb_token()
            if tok:
                headers["Authorization"] = tok
                req = urllib.request.Request(upstream_url, data=body, headers=headers, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=8) as r:
                        return r.status, r.read(), r.headers.get("Content-Type", "application/json")
                except urllib.error.HTTPError as e2:
                    return e2.code, e2.read(), e2.headers.get("Content-Type", "application/json")
                except Exception:
                    return 502, b'{"error":"upstream unreachable"}', "application/json"
        return e.code, e.read(), e.headers.get("Content-Type", "application/json")
    except Exception:
        return 502, b'{"error":"upstream unreachable"}', "application/json"

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "homelab-bff/1.0"

    # ---- API ----
    def _api(self):
        p = urlsplit(self.path).path
        if p == "/api/stats":
            st, body, ct = proxy(STATS_UP + "/api/stats", "GET", None)
        elif p == "/api/probe":
            qs = urllib.parse.parse_qs(urlsplit(self.path).query)
            _pst, _pbody = _do_probe(qs.get("url", [""])[0],
                                       qs.get("host", [""])[0])
            st, body, ct = _pst, json.dumps(_pbody).encode(), "application/json"
        elif p.startswith("/api/pb/"):
            sub = p[len("/api/pb"):]                      # keeps query string below
            url = PB_BASE + sub + ("?" + urlsplit(self.path).query if urlsplit(self.path).query else "")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            st, body, ct = proxy(url, self.command, body, self.headers,
                                 inject_auth=("/collections/_superusers" not in sub))
        else:
            st, body, ct = 404, b'{"error":"not found"}', "application/json"
        self.send_response(st)
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- static ----
    def _static(self):
        rel = urlsplit(self.path).path
        if rel in ("/", ""): rel = "/index.html"
        fs = os.path.normpath(STATIC_DIR + rel)
        if not fs.startswith(os.path.abspath(STATIC_DIR) + os.sep) and fs != os.path.abspath(STATIC_DIR):
            self.send_error(403); return
        if os.path.isdir(fs): fs = os.path.join(fs, "index.html")
        if not os.path.isfile(fs):
            body = b'{"error":"not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        ext = os.path.splitext(fs)[1].lower()
        with open(fs, "rb") as f: body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _dispatch(self):
        p = urlsplit(self.path).path
        if p.startswith("/api/"): self._api()
        elif self.command in ("GET", "HEAD"): self._static()
        else: self.send_error(405)

    do_GET = do_POST = do_PATCH = do_DELETE = _dispatch

    def log_message(self, fmt, *args):
        pass  # keep journal quiet; never log auth headers

if __name__ == "__main__":
    if not PB_BASE:
        raise SystemExit("PB_BASE_URL is required (bff.env)")
    threading.Thread(target=_warm_dns_cache, daemon=True).start()
    _log(f"bff listening on {LISTEN_HOST}:{LISTEN_PORT}; pb={PB_BASE}; anon={not (PB_ADMIN_EMAIL and PB_ADMIN_PASS)}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
