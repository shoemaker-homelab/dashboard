# BFF Agent-Dashboard Auth Fix — Summary & Lessons Learned

**Date:** 2026-09-19
**Author:** Alfred (assistant profile)
**Audience:** Vidar, Josh, and the rest of the team
**File touched:** `/home/vidar/homelab-dashboard/bff_server.py` (Vidar's code)
**Service:** `homelab-bff.service` (:18791)

## Symptom

Josh could not log in to the Hermes Agent web dashboard from his phone at
`hermes.tailda8422.ts.net/agent`:

1. **Phase 1:** every login attempt rejected ("wrong credentials") despite
   byte-correct credentials.
2. **Phase 2 (after fix 1):** login succeeded but the browser landed on the
   host dashboard (`/`, the Homelab dashboard) instead of the agent UI.

## Root causes (three distinct bugs, in order)

### 1. BFF dropped POSTs to `/auth/*` — 405

The login SPA POSTs to **`/auth/password-login`** (no `/agent` prefix — the
gateway's login page hard-codes the un-prefixed path). `_dispatch()` only
routed `/agent*` to the agent proxy; unprefixed `/auth/*` fell through to
`send_error(405)`.

**Fix:** route `/auth/*` to `_agent()` alongside `/agent*`.

**Diagnostic note:** the credential inputs were verified perfect first — a
`tcpdump` on loopback captured the actual phone POST bodies and they were
byte-exact. Do not chase "input mangling" without capture evidence.

### 2. BFF did not forward the session Cookie — every request looked logged-out

`_agent()` forwarded only `Content-Type` and `Accept` to the gateway. The
gateway therefore never saw `hermes_session_at`, and redirected every
`/agent/` request to login / bounced post-login.

**Fix:** add `"Cookie"` to the forwarded-header list.

### 3. Cookie path double-prefix AND post-login `next` pointed at `/`

Two sub-bugs producing the same symptom:

- The `Set-Cookie` rewriter turned the gateway's `Path=/` into
  `Path=/agent/agent` (the gateway, having received `X-Forwarded-Prefix: /agent`,
  had ALREADY scoped its cookie to `Path=/agent`). A browser never sends a
  `Path=/agent/agent` cookie back to `/agent/…` requests.
- The gateway's login page embeds `next="/"` and the login JSON returns
  `{"next":"/"}`. The SPA does `window.location.assign(next)` → the host
  dashboard.

**Fix:** only rewrite `Path=/` when the cookie is not already `/agent`-scoped;
rewrite the login JSON's `next` and the login page's hidden `next` input to
`/agent/` when served through the mount.

## Verification (all curl/browser-equivalent, post-fix)

- Login POST via `/auth/password-login` → 200 `{"ok":true,"next":"/agent/"}`
- GET `/agent/` with session cookie → 200, `<title>Hermes Agent - Dashboard`
- Full simulated phone flow: `/agent` → 302 login → login → land `/agent/` ✓
- Wrong password → 401 (auth gate intact)
- DELETE `/auth/password-login` → 405 (no new methods opened)
- Host dashboard `/` and `/api/pb/*` proxy → 200, unaffected

## Lessons learned (for the whole team)

1. **Mount-based reverse proxies must translate THREE things, not one:** the
   request path, the `Set-Cookie` path, and every in-body/`next` redirect
   target. Miss any one and auth "works" but the UX breaks in confusing ways.
2. **A proxy that forwards only content headers silently breaks auth.**
   Forward `Cookie` (and think about `Authorization`, `Origin`, `Referer`
   before excluding them deliberately).
3. **Check whether the upstream ALREADY honors the prefix** (`X-Forwarded-Prefix`)
   before rewriting cookie paths — double-prefixing produces cookies that
   match no request path ever.
4. **Verify claims with capture, not assumption.** The packet capture of the
   real phone POSTs ended the "is it autocorrect?" theory in one step.
   (`sudo tcpdump -i lo -w /tmp/x.pcap port 18791` + `-A` to read.)
5. **The gateway speaks `X-Forwarded-Prefix`** — given that header it scopes
   cookies and redirects itself. Only rewrite what it doesn't handle.
6. **Hermes dashboard auth config truth:** credentials live in
   `~/.hermes/.env` as `HERMES_DASHBOARD_BASIC_AUTH_USERNAME/_PASSWORD`
   (env wins over `dashboard.basic_auth` in config.yaml). Username/password
   are fully case-sensitive (`compare_digest`). The login endpoint is
   `POST /auth/password-login` with JSON
   `{provider, username, password, next}`.
7. **Diagnose at the exact layer the user experiences.** curl-ing the gateway
   directly returned 200 the whole time while the phone path 405'd — testing
   the wrong layer validated a broken system.

## Current state

- `bff_server.py` patched (changes documented above), `homelab-bff` restarted
  and green.
- Josh's phone session verified working end-to-end at
  `hermes.tailda8422.ts.net/agent`.
- Vidar: this touched your file — review at your convenience. Josh approved
  the surgical fix directly. No other routes were changed.
