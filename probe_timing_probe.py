#!/usr/bin/env python3
"""Throwaway harness: repeat the hostname-path probe directly against the
BFF's real listen socket to time the cold vs warm 15s DNS stall."""
import subprocess, urllib.request, urllib.parse, urllib.error, time

ip = subprocess.run(["getent", "hosts", "hermes.tailda8422.ts.net"],
                    capture_output=True, text=True).stdout.split()[0]
base = f"http://{ip}:18791"

def probe(url, host=None):
    q = urllib.parse.urlencode({"url": url})
    if host:
        q += "&host=" + host
    t = time.time()
    try:
        r = urllib.request.urlopen(base + f"/api/probe?{q}", timeout=40)
        return r.status, r.read().decode(), round(time.time()-t, 2)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), round(time.time()-t, 2)

print("warmup: ", probe("https://hermes.tailda8422.ts.net/"))
for i in range(4):
    print(f"probe {i}:", probe("https://hermes.tailda8422.ts.net/"))
print("literal:", probe(f"http://{ip}:80/", "hermes.tailda8422.ts.net"))
