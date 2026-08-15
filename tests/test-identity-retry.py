"""Regression test: transient whois failure must not permanently pin a cookie."""
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_web as web  # noqa: E402

failures = []


def check(name, condition):
    print(("PASS" if condition else "FAIL") + f": {name}")
    if not condition:
        failures.append(name)


class Handler:
    def _get_or_mint_cookie(self):
        return "retry-token", False

    def _client_ip(self):
        return "100.64.0.2"


real_registry = web.OPERATOR_REGISTRY
real_whois = web.tailscale_whois
# The owner-equality check (8.1.1) refuses any tailnet login that is not this
# hub's owner, and on a real machine tailnet_owner() resolves to the actual
# account -- which would refuse the stub below and make the upgrade assertions
# fail for a reason unrelated to retrying. Pin the owner to the stubbed login
# so this test keeps testing the retry.
web._tailnet_owner_cache = "operator@example.test"
web._tailnet_owner_warned = True
registry = web.OperatorRegistry()
web.OPERATOR_REGISTRY = registry
handler = Handler()
calls = []

try:
    web.tailscale_whois = lambda _ip: calls.append("down") or None
    _token, ident, _ = web.NthWebHandler._resolve_identity(handler)
    check("initial whois failure yields pending", ident.source == web.IDENTITY_SOURCE_PENDING)
    registry.register_guest("retry-token", "Operator")

    web.tailscale_whois = lambda _ip: calls.append("up") or {
        "login": "operator@example.test", "display": "Operator", "node": "laptop"}
    _token, ident, _ = web.NthWebHandler._resolve_identity(handler)
    check("guest remains cached during retry interval", ident.source == web.IDENTITY_SOURCE_GUEST)
    check("no immediate repeated whois", calls == ["down"])

    registry._last_retry_at["retry-token"] = time.time() - web.OP_IDENTITY_RETRY_S
    _token, ident, _ = web.NthWebHandler._resolve_identity(handler)
    check("recovered whois upgrades cached guest", ident.source == web.IDENTITY_SOURCE_TAILSCALE)
    check("retry runs once after interval", calls == ["down", "up"])

    _token, ident, _ = web.NthWebHandler._resolve_identity(handler)
    check("trusted identity stays cached", ident.source == web.IDENTITY_SOURCE_TAILSCALE)
    check("trusted identity does not re-run whois", calls == ["down", "up"])
finally:
    web.OPERATOR_REGISTRY = real_registry
    web.tailscale_whois = real_whois
    web._tailnet_owner_cache = None

print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
