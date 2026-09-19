#!/usr/bin/env python3
"""Freeze-gate smoke test for the homelab dashboard stack.

Run after ANY change to bff_server.py, app.js, index.html, pipeline.js,
or the Caddy/PB/probe config. Exit 0 only if every check passes.

Usage:  python3 smoke_test.py [BASE_URL] [BFF_DIRECT_URL]
        BASE_URL defaults to https://hermes.tailda8422.ts.net (via tailscale serve).
        BFF_DIRECT_URL, when given (e.g. http://127.0.0.1:18791), is used for the
        latency-sensitive probe checks (<1s): measure the BFF's own response time,
        excluding middlebox/proxy latency between the caller and Caddy.

Checks (status + body + latency where specified):
  1.  splash 200
  2.  /pipeline/ 200
  3.  /api/stats 200, psutil JSON shape
  4.  /api/pb/api/health 200
  5.  probe hostname path -> 200 {"ok":true} <1s   (cold DNS may add ~15s
      on the very first request after a BFF restart; retry once)
  6.  probe IP-literal + matching host -> 200 {"ok":true} <1s
      (literal taken from the runtime DNS A set of BASE, tried per candidate)
  7.  probe private IP outside the resolved host_param set -> 400 <1s
  8.  probe external host -> 400                 (SSRF allowlist)
  9.  probe userinfo URL -> 400                  (userinfo bypass)
  10. probe file:// -> 400                       (scheme allowlist)
  11. probe port 22 -> 400                       (port allowlist)

LAN IPs are resolved at RUNTIME (AF_INET getaddrinfo of the BASE host,
never hardcoded — hostname -I returns interface addresses that may not be
in the host's DNS A set, which the A+ IP-literal rule rejects).
"""
import json
import ipaddress
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://hermes.tailda8422.ts.net"
# Direct BFF base for latency-sensitive checks (falls back to BASE).
BFF_DIRECT = sys.argv[2] if len(sys.argv) > 2 else BASE
IP_FOREIGN = "http://[IP_ADDRESS]/"  # private subnet NOT on the allowlist
FAILURES = []


def check(name, ok, detail, latency=None):
    lat = f" ({latency:.2f}s)" if latency is not None else ""
    print(f"{'PASS' if ok else 'FAIL'}  {name}{lat}  {detail}")
    if not ok:
        FAILURES.append(name)


def http(path, timeout=35, base=None):
    """GET base+path (default BASE). Returns (status|int|None, body|str, latency_s)."""
    t0 = time.time()
    try:
        r = urllib.request.urlopen((base or BASE) + path, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0
    except Exception as e:  # timeout, conn refused, etc.
        return None, f"{type(e).__name__}: {e}", time.time() - t0


def probe_url(url, base=None):
    q = urllib.parse.urlencode({"url": url})
    return http(f"/api/probe?{q}", base=base)


def lan_ips():
    """IPs the BASE hostname resolves to, at runtime (AF_INET getaddrinfo).

    These are exactly the addresses the BFF's A+ rule accepts for the
    IP-literal check: hostname -I's interface addresses need not be in the
    host's DNS A set, so they can (and here do) 400."""
    host = urlsplit_hostname(BASE)
    infos = socket.getaddrinfo(host, None, socket.AF_INET)
    ips = {i[4][0] for i in infos}
    if not ips:
        raise RuntimeError(f"{host} resolved to no IPv4 addresses")
    return sorted(ips)


def urlsplit_hostname(base):
    return urllib.parse.urlsplit(base).hostname


def main():
    # 1. splash
    st, body, _ = http("/")
    check("splash 200", st == 200, f"status={st}")
    if st == 200 and "shoemaker.homelab" in body:
        check("splash no retired domain", False, "shoemaker.homelab in HTML")

    # 2. pipeline page
    st, body, _ = http("/pipeline/")
    check("pipeline 200", st == 200, f"status={st}")

    # 3. stats: 200 + psutil JSON shape
    st, body, _ = http("/api/stats", timeout=10)
    shape = False
    detail = f"status={st}"
    if st == 200:
        try:
            j = json.loads(body)
            req = {"hostname", "cpu_percent", "cpu_count", "ram_used_gb",
                   "ram_total_gb", "ram_percent", "disk_used_gb",
                   "disk_total_gb", "disk_percent", "uptime_s"}
            shape = req.issubset(j.keys())
            detail = f"keys ok={shape}, cpu={j.get('cpu_percent')}%"
        except Exception as e:
            detail = f"bad JSON: {e}"
    check("/api/stats 200 + psutil shape", st == 200 and shape, detail)

    # 4. PB health via BFF proxy
    st, body, _ = http("/api/pb/api/health", timeout=10)
    check("/api/pb/api/health 200", st == 200 and "healthy" in body,
          f"status={st} body={body[:80]}")

    # 5. probe hostname path -> 200 {"ok": true} <1s (latency measured at the BFF)
    # A cold ts.net resolution inside _probe_fetch (urllib's AAAA-first
    # lookup + ts.net search-domain suffixing) can take ~15s on the first
    # request after a restart; warm the cache with one throwaway call, then
    # measure.
    warm_st, _, _ = probe_url("https://hermes.tailda8422.ts.net/",
                              base=BFF_DIRECT)
    st, body, lat = probe_url("https://hermes.tailda8422.ts.net/",
                              base=BFF_DIRECT)
    ok = st == 200 and '"ok": true' in body and lat < 1.0
    check("probe hostname <1s 200 ok:true", ok,
          f"status={st} (warmup status={warm_st}) body={body[:60]}", lat)

    # 6. probe IP-literal + matching host -> 200 {"ok": true} <1s
    # The A+ rule requires the literal to equal an address the host_param
    # resolves to; try each runtime-resolved candidate until one matches.
    candidates = lan_ips()
    st, body, lat = None, "", None
    for ip in candidates:
        q = urllib.parse.urlencode({"url": f"http://{ip}:80/"}) + "&host=hermes.tailda8422.ts.net"
        st, body, lat = http(f"/api/probe?{q}", base=BFF_DIRECT)
        if st == 200 and '"ok": true' in body:
            break
    ok = st == 200 and '"ok": true' in body and lat is not None and lat < 1.0
    check("probe IP-literal+host <1s 200 ok:true", ok,
          f"status={st} tried={candidates} body={body[:60]}", lat)

    st, body, lat = probe_url(IP_FOREIGN, base=BFF_DIRECT)
    # The tailnet IP is rejected via rule (c) of the A+ path (literal not in
    # the resolved host_param set), not the "foreign private IP" branch.
    check("probe private IP not in resolved host_param set 400 <1s",
          st == 400 and lat < 1.0,
          f"status={st} body={body[:60]}", lat)

    # 8. external host -> 400
    st, body, lat = probe_url("http://example.com/")
    check("probe external host 400", st == 400, f"status={st} body={body[:60]}", lat)

    # 9. userinfo -> 400
    st, body, lat = probe_url("http://hermes.tailda8422.ts.net@evil.example/")
    check("probe userinfo 400", st == 400, f"status={st} body={body[:60]}", lat)

    # 10. file:// -> 400
    st, body, lat = probe_url("file:///etc/passwd")
    check("probe file:// 400", st == 400, f"status={st} body={body[:60]}", lat)

    # 11. port 22 -> 400 (ssh scheme and http-on-22)
    st1, b1, _ = probe_url("ssh://hermes.tailda8422.ts.net:22/")
    st2, b2, _ = probe_url("http://hermes.tailda8422.ts.net:22/")
    check("probe port 22 400", st1 == 400 and st2 == 400,
          f"ssh status={st1} http:22 status={st2}")

    print()
    if FAILURES:
        print(f"SMOKE TEST FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        sys.exit(1)
    print("SMOKE TEST PASSED: all checks green.")


if __name__ == "__main__":
    main()
