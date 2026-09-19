# Vidar session ledger — 2026-09-14 (dashboard project close-out)

## Governance (Josh's orders)
- Board FROZEN. No agent spawns cards without Vidar approval; Vidar is sole arbiter of real vs noise.
- Working cadence: compile issues -> one legit-problem card -> Brokkr builds -> Vidar tests/validates -> close -> next card.
- New-project lifecycle saved as skill: production-workflow (spec -> build -> review -> fix -> content -> polish -> security audit -> launch; per-function testing; one owning agent per file).

## Verified dashboard state (live-audited 09-14)
- All routes 200 (splash, pipeline, /api/stats, /api/pb/api/health)
- Hermes Dashboard dot GREEN: {"ok": true, "status": 200}, A+ IP-literal rule enforced, sweep protection 400s, SSRF attack matrix all rejected
- Tests on disk (survive board wipe): ~/homelab-dashboard/smoke_test.py (11 checks) + test_host_validation.py (14/14, mutation-verified)
- Only code leftover: smoke test expects "target not allowed" but gets "invalid url" on one mismatch case (cosmetic)

## DNS root cause (verified 09-14)
- Tailnet DNS resolver (100.x:53 path via tailscale0 -> msi dnsmasq/upstream) intermittently times out; AAAA queries black-hole entirely.
- Effect: EVERY Caddy-fronted route +5-15s; BFF hit directly answers in 0.02s (dashboard code innocent).
- This is the true root of all "15s stall" symptoms seen during the night.
- FIRST UNFREEZE CARD: Tyr fixes DNS infrastructure. Until then, all dashboard latency signals are noise.

- DNS RULING (Josh 09-14): 5-15s Caddy/DNS latency = accepted cost of tailscale + old hw + dnsmasq + wireguard. NOT a defect; mitigation = expect slow, cache-friendly. First probe after TTL expiry ~5s is normal.
- Dashboard CLOSED by Josh 09-14. Verified: routes 200, dot GREEN, A+ SSRF rule enforced, 14/14 tests pass, smoke_test.py + test_host_validation.py on disk. Only cosmetic leftover: one smoke expectation checks "target not allowed" but gets "invalid url".
- GOVERNANCE: no agent spawns cards without Vidar approval; Vidar is sole arbiter and triage/decomposer. One card at a time: issues -> card -> Brokkr builds -> Vidar tests -> close -> next.
- New projects run the production-workflow skill (spec -> build -> review -> fix -> content -> polish -> security audit -> launch).
- Redaction layer corrupts literally-typed IPs in agent test code; always fetch IPs at runtime via hostname -I.
