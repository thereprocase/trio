"""Tests for tailnet-owner enforcement and guest survival across a retry.

Two independent properties, both about who the web side is willing to trust.

1. OWNER EQUALITY. A tailnet peer is not automatically this hub's operator.
   Without the check, any account the tailnet resolves -- a second person on a
   shared tailnet, a device handed to someone else -- gets the same rights as a
   local shell: reveal a path, remove a member, write into the operator's home
   directory. The comparison is by ACCOUNT, so the owner's own several machines
   all still pass.

2. A RETRY MUST NOT DOWNGRADE. The untrusted-identity retry re-runs the ladder
   for `pending` and `guest`. A guest exists precisely BECAUSE whois could not
   name them, so that retry fails for every guest by definition -- and if a
   failed retry parks them as `pending`, every guest is silently un-named once
   per retry window, forever, and told to identify again mid-session.

Usage: python tests/test-identity-owner.py
"""
import sys
import types
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_web as web       # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def with_whois(login):
    """Stub tailscale_whois to answer as `login` (or not at all)."""
    if login is None:
        web.tailscale_whois = lambda ip: None
    else:
        web.tailscale_whois = lambda ip: {
            "login": login, "display": login.split("@")[0], "node": "node"}


def with_owner(owner):
    """Pin the derived hub owner without shelling out to tailscale."""
    web._tailnet_owner_cache = owner
    web._tailnet_owner_warned = True        # keep the test output quiet


_real_whois = web.tailscale_whois
try:
    # ── owner equality ──────────────────────────────────────────────────────
    with_owner("operator@example.com")

    reg = web.OperatorRegistry()
    with_whois("operator@example.com")
    ident = reg.resolve_from_tailscale("tok-owner", "100.64.0.1")
    check("owner's own account resolves as tailscale tier",
          ident is not None and ident.source == web.IDENTITY_SOURCE_TAILSCALE)

    reg = web.OperatorRegistry()
    with_whois("someone-else@example.com")
    ident = reg.resolve_from_tailscale("tok-other", "100.64.0.2")
    check("a DIFFERENT tailnet account is refused the tailscale tier",
          ident is None)

    # A second machine of the owner's carries the same login, so multi-device
    # use must be unaffected -- this is the regression the check could plausibly
    # cause, and the reason it compares accounts rather than nodes.
    reg = web.OperatorRegistry()
    with_whois("operator@example.com")
    ident = reg.resolve_from_tailscale("tok-laptop", "100.64.0.99")
    check("the owner's second device still resolves (account, not node)",
          ident is not None and ident.source == web.IDENTITY_SOURCE_TAILSCALE)

    # ── unknown owner: FAIL CLOSED by default ──────────────────────────────
    # The window this closes is the realistic one: `status --json` is a
    # different subcommand from `whois`, and a TAGGED node (any hub brought up
    # with an auth key) has no user account, so the owner lookup comes back
    # empty while whois keeps working perfectly. Failing open there hands
    # reveal/cull/upload to every account on the tailnet.
    import os
    with_owner("")
    reg = web.OperatorRegistry()
    with_whois("anyone@example.com")
    ident = reg.resolve_from_tailscale("tok-unknown", "100.64.0.3")
    check("undeterminable owner is REFUSED by default (fails closed)",
          ident is None)

    os.environ["NTH_TAILNET_PERMISSIVE"] = "1"
    try:
        reg = web.OperatorRegistry()
        web._tailnet_owner_warned = True
        ident = reg.resolve_from_tailscale("tok-permissive", "100.64.0.4")
        check("NTH_TAILNET_PERMISSIVE=1 opts back into accepting anyone",
              ident is not None)
        # ...but that grant must NOT be cached. A tailscale identity is never
        # re-checked once cached, so caching a permissive grant would keep
        # operator rights alive for the cookie's 30-day life even after owner
        # resolution starts working and says they are not the owner.
        check("a permissive grant is provisional, not cached",
              reg.get("tok-permissive") is None)
    finally:
        os.environ.pop("NTH_TAILNET_PERMISSIVE", None)

    # An explicit owner still enforces even in permissive mode -- permissive
    # only covers the "cannot determine" case, not "determined and mismatched".
    os.environ["NTH_TAILNET_PERMISSIVE"] = "1"
    try:
        with_owner("operator@example.com")
        reg = web.OperatorRegistry()
        with_whois("intruder@example.com")
        ident = reg.resolve_from_tailscale("tok-perm-mismatch", "100.64.0.5")
        check("permissive does NOT excuse a known-owner mismatch",
              ident is None)
    finally:
        os.environ.pop("NTH_TAILNET_PERMISSIVE", None)

    # ── a failed retry must not downgrade a guest ──────────────────────────
    # Register a guest, then force the retry window open and re-resolve with
    # whois still failing -- exactly the state every guest is permanently in.
    with_owner("operator@example.com")
    reg = web.OperatorRegistry()
    with_whois(None)
    guest = reg.register_guest("tok-guest", "Mallory")
    check("guest registers", guest.source == web.IDENTITY_SOURCE_GUEST)

    handler = types.SimpleNamespace()
    saved_registry = web.OPERATOR_REGISTRY
    web.OPERATOR_REGISTRY = reg
    try:
        # Eligibility is asserted on a SEPARATE token, because
        # should_retry_untrusted() *reserves* the retry -- it stamps the rate
        # limiter as a side effect. Calling it here on tok-guest would consume
        # the window, _resolve_identity would take the rate-limited early
        # return, and this test would pass without ever exercising the retry
        # path. (It did exactly that on the first run.)
        reg2 = web.OperatorRegistry()
        reg2.register_guest("tok-probe", "Probe")
        reg2._last_retry_at["tok-probe"] = 0.0
        check("a guest IS eligible for the retry",
              reg2.should_retry_untrusted("tok-probe") is True)

        # Now open the window on the real token and leave it open.
        reg._last_retry_at["tok-guest"] = 0.0
        handler._get_or_mint_cookie = lambda: ("tok-guest", False)
        handler._client_ip = lambda: "203.0.113.9"      # not loopback, no whois
        handler._resolve_identity = web.NthWebHandler._resolve_identity.__get__(handler)
        _tok, after, _new = handler._resolve_identity()

        check("guest survives a failed retry (still guest)",
              after.source == web.IDENTITY_SOURCE_GUEST)
        check("guest keeps the same member_id across the retry",
              after.member_id == guest.member_id)
        check("guest keeps their declared name",
              after.name == guest.name)
    finally:
        web.OPERATOR_REGISTRY = saved_registry

finally:
    web.tailscale_whois = _real_whois
    web._tailnet_owner_cache = None

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
