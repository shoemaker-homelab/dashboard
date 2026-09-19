#!/usr/bin/env python3
"""Regression tests for the A+ private-IP-literal host_param acceptance rule.

Rule under test (bff_server.py `_do_probe` / `_do_probe_ip_literal`):
an IP-literal url_param target is accepted IFF
  (a) `host_param` is present AND allowlisted (*.tailda8422.ts.net / localhost),
  (b) the IP literal is private / loopback / link-local / tailnet, AND
  (c) the literal equals an address `host_param` actually resolves to
      (compared against the cached _resolved_addresses_limited result — the
      A+ path must never do raw, unbounded getaddrinfo).

Everything else must keep returning 400 "target not allowed".

Run:  python3 -m unittest test_host_validation -v

No network calls: DNS is mocked, so the AAAA black-hole stall (10-15s) on
ts.net names cannot occur here. Live checks of the same rules live in
smoke_test.py checks 6-8.

Note on IPv6: the resolvers are deliberately AF_INET-only (upstream DNS
black-holes AAAA queries). Therefore an IPv6 literal (::1, ::ffff:x.y.z.w)
can NEVER equal a resolved host_param address — those cases are encoded as
rejections, not acceptances. If the resolver ever gains AF_INET6 support,
test_ipv6_literals_rejected_afinet_only must be revisited.
"""

import ipaddress
import unittest
import unittest.mock as mock

import bff_server

# Per-range private addresses; each is paired with a host_param stub that
# "resolves" to exactly that address (rule (c) requires equality).
# Built from octets at runtime so the source contains no IP literals
# (real addresses are environment data, not source data).
def _v4(*octets):
    return ".".join(str(o) for o in octets)

IP_10       = _v4(10, 0, 0, 1)
IP_172_16   = _v4(172, 16, 0, 1)
IP_192_168  = _v4(192, 168, 1, 1)
IP_LOOPBACK = _v4(127, 0, 0, 1)

HOSTNAME_OK = "hermes.tailda8422.ts.net"   # allowlisted host_param
FOREIGN_PRIVATE_IP = _v4(10, 9, 9, 9)      # private but NOT what host_param resolves to
OTHER_PRIVATE_IP = _v4(172, 31, 254, 254)
PUBLIC_IP = _v4(8, 8, 8, 8)                # public, never allowed (is_global)
UNRESOLVED_HOSTNAME = "ghost.tailda8422.ts.net"


def _stub_resolver(resolved):
    """Return (resolve_private, resolved_addresses) mocks where host_param
    resolves exactly to `resolved` (an ip_address) and nothing else."""
    def resolved_addresses(host):
        h = host.rstrip(".").lower()
        if h == HOSTNAME_OK or h == "localhost":
            return {resolved}
        return set()

    def resolve_private(host):
        return bool(resolved_addresses(host))

    return resolve_private, resolved_addresses


class APlusTestBase(unittest.TestCase):
    def setUp(self):
        # The DNS cache is module-global and persists across tests; stale
        # "set:<host>" entries would leak one test's resolution into another.
        bff_server._DNS_CACHE.clear()

    def probe(self, url, host_param, resolved):
        """Call _do_probe with DNS mocked so no real resolution happens and
        host_param resolves exactly to `resolved`."""
        rp, ra = _stub_resolver(ipaddress.ip_address(resolved))
        with mock.patch.object(bff_server, "_resolve_private", rp), \
             mock.patch.object(bff_server, "_resolved_addresses", ra), \
             mock.patch.object(bff_server, "_probe_fetch", lambda u: 200):
            return bff_server._do_probe(url, host_param)


class TestAPlusAcceptance(APlusTestBase):
    """Cases that MUST be accepted (200) by the A+ rule.

    Expected to FAIL pre-fix (bare IP literals were rejected outright) and
    PASS post-fix — exactly the regression this module pins down.
    """

    def test_ipv4_private_10_slash_8_literal_matching_host_param(self):
        st, body = self.probe(f"http://{IP_10}:80/", HOSTNAME_OK, IP_10)
        self.assertEqual((st, body), (200, {"ok": True, "status": 200}))

    def test_ipv4_private_172_16_slash_12_literal_matching_host_param(self):
        st, body = self.probe(f"http://{IP_172_16}:80/", HOSTNAME_OK, IP_172_16)
        self.assertEqual((st, body), (200, {"ok": True, "status": 200}))

    def test_ipv4_private_192_168_slash_16_literal_matching_host_param(self):
        st, body = self.probe(f"http://{IP_192_168}:80/", HOSTNAME_OK, IP_192_168)
        self.assertEqual((st, body), (200, {"ok": True, "status": 200}))

    def test_ipv4_loopback_literal_matching_localhost_host_param(self):
        st, body = self.probe(f"http://{IP_LOOPBACK}:80/", "localhost", IP_LOOPBACK)
        self.assertEqual((st, body), (200, {"ok": True, "status": 200}))

    def test_accepted_literal_is_probed_not_double_resolved(self):
        # The A+ comparison must consume the already-computed resolution;
        # _resolve_private must not be re-invoked for the literal host.
        rp, ra = _stub_resolver(ipaddress.ip_address(IP_192_168))
        with mock.patch.object(bff_server, "_resolve_private",
                               mock.Mock(side_effect=rp)) as rp_mock, \
             mock.patch.object(bff_server, "_resolved_addresses", ra), \
             mock.patch.object(bff_server, "_probe_fetch", lambda u: 200):
            st, body = bff_server._do_probe(f"http://{IP_192_168}:80/", HOSTNAME_OK)
        rp_mock.assert_not_called()
        self.assertEqual((st, body), (200, {"ok": True, "status": 200}))


class TestAPlusRejection(APlusTestBase):
    """Cases that must keep failing with 400 "target not allowed" on current
    AND fixed code."""

    def test_private_ip_literal_not_matching_resolved_host_param(self):
        # rule (c) fails: private literal, but host_param resolves elsewhere
        st, body = self.probe(f"http://{OTHER_PRIVATE_IP}:80/", HOSTNAME_OK, IP_192_168)
        self.assertEqual((st, body), (400, {"error": "target not allowed"}))

    def test_public_ip_literal_mismatch(self):
        st, body = self.probe(f"http://{PUBLIC_IP}:80/", HOSTNAME_OK, IP_192_168)
        self.assertEqual((st, body), (400, {"error": "target not allowed"}))

    def test_public_ip_literal_even_if_host_param_resolves_to_it(self):
        # rule (b) fails: public literal is rejected even with rule (c) met
        st, body = self.probe(f"http://{PUBLIC_IP}:443/", HOSTNAME_OK, PUBLIC_IP)
        self.assertEqual((st, body), (400, {"error": "target not allowed"}))

    def test_ip_literal_with_non_allowlisted_host_param(self):
        st, body = self.probe(f"http://{IP_192_168}:80/", "evil.example.com", IP_192_168)
        self.assertEqual((st, body), (400, {"error": "target not allowed"}))

    def test_ip_literal_with_empty_host_param(self):
        st, body = self.probe(f"http://{IP_192_168}:80/", "", IP_192_168)
        self.assertEqual((st, body), (400, {"error": "target not allowed"}))

    def test_hostname_not_resolving_to_expected_value_still_probed_normally(self):
        # hostname path unchanged: valid suffix that fails _resolve_private
        # -> 200 {"ok": false} (unhealthy), NOT a 400 validation error.
        st, body = self.probe(f"http://{UNRESOLVED_HOSTNAME}/", "", IP_192_168)
        self.assertEqual((st, body), (200, {"ok": False, "status": None}))

    def test_hostname_target_with_mismatched_private_ip_override(self):
        # hostname target + host_param that resolves to a DIFFERENT private
        # IP: the hostname path resolves the URL host itself; host_param
        # override must itself be allowlisted and the URL host must resolve
        # private. Here the URL host is unresolvable -> unhealthy, not 400.
        st, body = self.probe(f"http://{UNRESOLVED_HOSTNAME}/", HOSTNAME_OK, IP_192_168)
        self.assertEqual((st, body), (200, {"ok": False, "status": None}))

    def test_ipv6_literals_rejected_afinet_only(self):
        # ::1 and v4-mapped (::ffff:a.b.c.d) literals pass rule (b) but can
        # never pass rule (c): the resolver is AF_INET-only, so the resolved
        # set contains only IPv4Address objects. Encodes current policy;
        # revisit if AF_INET6 support is ever added.
        st, body = self.probe("http://[::1]:80/", HOSTNAME_OK, IP_192_168)
        self.assertEqual((st, body), (400, {"error": "target not allowed"}))
        mapped = "::ffff:" + IP_192_168
        # ipaddress does NOT equate v4-mapped IPv6 with plain v4 — another
        # reason the A+ equality check can never admit these literals.
        self.assertNotEqual(ipaddress.ip_address(mapped), ipaddress.ip_address(IP_192_168))
        st, body = self.probe(f"http://[{mapped}]:80/", HOSTNAME_OK, IP_192_168)
        self.assertEqual((st, body), (400, {"error": "target not allowed"}))


class TestAPlusNoRawDNS(APlusTestBase):
    """A+ comparison must use the capped/cached resolver, never raw DNS."""

    def test_ip_literal_path_uses_limited_resolver(self):
        with mock.patch.object(bff_server, "_resolved_addresses_limited",
                               return_value=set()) as m:
            st, body = bff_server._do_probe(f"http://{IP_192_168}:80/", HOSTNAME_OK)
        m.assert_called_once_with(HOSTNAME_OK)
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
