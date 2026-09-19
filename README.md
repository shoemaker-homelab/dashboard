# Shoemaker-Homelab Dashboards

Per-host dashboard pattern: each host serves its own dashboard at
`<host>.tailda8422.ts.net/` (path-based Tailscale serve, no port juggling).

## Layout
- `index.html`, `app.js`, `style.css`, `config.js` — static dashboard UI
- `bff_server.py` — host BFF: serves static files, proxies `/api/stats` to local stats_server, `/api/pb/*` to PocketBase on msi
- `stats_server.py` — psutil JSON stats endpoint (:18790)
- `smoke_test.py`, `test_host_validation.py`, `probe_timing_probe.py` — validation

## Enabling on a new host
1. Copy repo to host, create `bff.env` (not committed) with `PB_BASE_URL=...`
2. systemd units: `stats_server.service`, `homelab-bff.service`
3. `tailscale serve --bg --https=443 http://localhost:18791`
