"""Tests for NTH_TAILNET_DEVICE_OWNERS: delegating a tagged tailnet node to a login.

A tagged node has no user, so whois reports the placeholder login
"tagged-devices". The hub owner may map such a node's tailnet address to a
login (`ip=login`). The mapped login then goes through the ordinary
NTH_TAILNET_OWNER / NTH_TAILNET_MEMBERS gate, so the device gets exactly that
login's tier. Everything here is mocked: no network, no tailscale binary.

Usage: python tests/test-identity-device-owner.py
"""
import io
import os
import sys
import types
from contextlib import redirect_stderr
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_web as web       # noqa: E402

failures = []
OWNER = "owner@example.com"
MEMBER = "member@example.com"
DEVICE = "100.64.0.10"
OTHER = "100.64.0.11"


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def tagged(node_id="nodeA", tags=("tag:server",), addresses=None):
    return {"login": "tagged-devices", "display": "Tagged Devices", "node": "box",
            "node_id": node_id, "tags": list(tags),
            "addresses": addresses if addresses is not None else []}


def user_device(login):
    return {"login": login, "display": "User", "node": "laptop", "node_id": "nodeU",
            "tags": [], "addresses": []}


def configure(owners="", members="", owner=OWNER, name=None):
    for key, val in (("NTH_TAILNET_DEVICE_OWNERS", owners),
                     ("NTH_TAILNET_MEMBERS", members),
                     ("NTH_TAILNET_DEVICE_OWNER_NAME", name)):
        if val is None or val == "":
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    web._device_owner_cache = None
    web._tailnet_owner_cache = owner
    web._tailnet_owner_warned = True


def whois_returns(info):
    web.tailscale_whois = lambda ip: info


def resolve(ip=DEVICE, token="tok"):
    reg = web.OperatorRegistry()
    with redirect_stderr(io.StringIO()):
        return reg, reg.resolve_from_tailscale(token, ip)


def handler_for(reg, token, ip, headers=None):
    """A bare handler whose only inputs are the cookie token and socket peer."""
    h = types.SimpleNamespace()
    h.client_address = (ip, 50000)
    h.headers = {"Cookie": f"{web.OP_COOKIE}={token}", **(headers or {})}
    h._get_or_mint_cookie = lambda: (token, False)
    h._client_ip = web.NthWebHandler._client_ip.__get__(h)
    h._resolve_identity = web.NthWebHandler._resolve_identity.__get__(h)
    return h


def can_operate(ident):
    """Whether the identity passes the same source gates the operator actions use."""
    return (ident.source in web.CULL_ALLOWED_SOURCES
            and ident.source in web.LOCAL_PATH_ALLOWED_SOURCES)


_real_whois = web.tailscale_whois
_real_registry = web.OPERATOR_REGISTRY
try:
    # ── resolution ──────────────────────────────────────────────────────────
    configure(owners=f"{DEVICE}={OWNER}")
    whois_returns(tagged())
    _, ident = resolve()
    check("mapped owner resolves at the tailscale tier",
          ident is not None and ident.source == web.IDENTITY_SOURCE_TAILSCALE
          and ident.login == OWNER)
    check("mapped owner passes the operator gates", ident is not None and can_operate(ident))
    check("delegation provenance is recorded",
          ident.delegated_from == "tagged-devices" and ident.delegated_node == "nodeA"
          and ident.delegated_peer == DEVICE and "delegated" in ident.summary)
    check("the placeholder display name is not used",
          "tagged" not in ident.name.lower() and ident.name == "owner")
    check("delivery role is owner", web.delivery_role(ident) == "owner")

    configure(owners=f"{DEVICE}={OWNER}", name="Owner Name")
    _, ident = resolve()
    check("NTH_TAILNET_DEVICE_OWNER_NAME labels the owner", ident.name == "Owner Name")

    configure(owners=f"{DEVICE}={MEMBER}", members=f"{MEMBER}=Sam", name="Owner Name")
    _, ident = resolve()
    check("mapped listed member is a member, named by the members list",
          ident.source == web.IDENTITY_SOURCE_MEMBER and ident.name == "Sam"
          and ident.delegated_from)
    check("mapped member has no operator powers", not can_operate(ident))

    configure(owners=f"{DEVICE}=stranger@example.com", name="Owner Name")
    _, ident = resolve()
    check("mapped unlisted login stays a guest",
          ident.source == web.IDENTITY_SOURCE_GUEST and ident.tailnet_verified
          and ident.name != "Owner Name" and not can_operate(ident))

    configure(owners=f"{DEVICE}={OWNER}")
    _, ident = resolve(OTHER)
    check("an unmapped tagged device gets no delegation and no operator powers",
          ident is not None and not ident.delegated_from and not can_operate(ident))
    check("an unmapped tagged device is not the owner",
          ident is not None and ident.login != OWNER and not ident.delegated_from)

    whois_returns(user_device("someone@example.com"))
    _, ident = resolve()
    check("a user-owned device at a listed address is not overridden",
          ident.login == "someone@example.com" and not ident.delegated_from
          and not can_operate(ident))

    whois_returns(tagged(tags=()))
    _, ident = resolve()
    check("placeholder login without tags is not a supported tagged node",
          ident is None or not ident.delegated_from)

    whois_returns(tagged(addresses=["100.64.0.99/32"]))
    _, ident = resolve()
    check("a node whose addresses exclude the peer is not delegated",
          ident is None or not ident.delegated_from)
    whois_returns(tagged(addresses=[f"{DEVICE}/32", "fd7a:115c:a1e0::1/128"]))
    _, ident = resolve()
    check("a node whose addresses include the peer is delegated",
          ident is not None and ident.login == OWNER)

    configure(owners="")
    whois_returns(tagged())
    _, ident = resolve()
    check("missing mapping variable: tagged device is not the owner",
          ident is None or (ident.login != OWNER and not can_operate(ident)))

    configure(owners=f"{DEVICE}={OWNER}", owner="")
    _, ident = resolve()
    check("unknown hub owner still fails closed under a mapping", ident is None)

    configure(owners=f"{DEVICE}={OWNER}")
    whois_returns(None)
    _, ident = resolve()
    check("failed whois grants nothing", ident is None)
    web.tailscale_whois = _real_whois
    real_co = web.subprocess.check_output
    for label, out in (("non-JSON", b"not json"), ("empty identity", b'{"UserProfile": {}}')):
        web.subprocess.check_output = lambda *a, **k: out
        try:
            _, ident = resolve()
        finally:
            web.subprocess.check_output = real_co
        check(f"malformed whois ({label}) grants nothing", ident is None)
    whois_returns({"login": "tagged-devices", "display": "x", "node": "n"})
    _, ident = resolve()
    check("whois without node/tag fields grants nothing", not (ident and ident.login == OWNER))
    whois_returns({"login": "tagged-devices", "tags": "tag:x", "addresses": 7})
    _, ident = resolve()
    check("wrongly typed tag fields grant nothing", not (ident and ident.login == OWNER))

    # ── tailscale_whois stays factual ───────────────────────────────────────
    web.tailscale_whois = _real_whois
    import json
    payload = json.dumps({"UserProfile": {"LoginName": "tagged-devices", "DisplayName": "Tagged Devices"},
                          "Node": {"Name": "box.example.ts.net.", "StableID": "nABC",
                                   "Tags": ["tag:server"], "Addresses": [f"{DEVICE}/32"]}}).encode()
    real_co = web.subprocess.check_output
    web.subprocess.check_output = lambda *a, **k: payload
    configure(owners=f"{DEVICE}={OWNER}")
    try:
        info = web.tailscale_whois(DEVICE)
    finally:
        web.subprocess.check_output = real_co
    check("tailscale_whois reports the placeholder login and node facts, unmapped",
          info["login"] == "tagged-devices" and info["node_id"] == "nABC"
          and info["tags"] == ["tag:server"] and info["node"] == "box")

    # ── configuration ───────────────────────────────────────────────────────
    parse = web.parse_device_owners
    m, p = parse(f"192.0.2.1={OWNER}, 192.0.2.2 = {MEMBER} ,")
    check("valid entries parse with whitespace trimmed",
          m == {"192.0.2.1": OWNER, "192.0.2.2": MEMBER} and not p)
    cases = {
        "invalid address": "192.0.2.300=a@example.com",
        "hostname": "box.example.com=a@example.com",
        "zone id": "fe80::1%eth0=a@example.com",
        "empty login": "192.0.2.3=",
        "missing separator": "192.0.2.3",
        "extra separator": "192.0.2.3=a@example.com=b",
        "control character": "192.0.2.3=a@exa\x07mple.com",
        "inner whitespace": "192.0.2.3=a b@example.com",
        "placeholder login": "192.0.2.3=tagged-devices",
        "loopback": "127.0.0.1=a@example.com",
    }
    for label, raw in cases.items():
        m, p = parse(raw)
        check(f"rejects {label} and names the entry",
              not m and len(p) == 1 and raw[:6].encode("unicode_escape").decode() in p[0])
    m, p = parse("192.0.2.4=a@example.com,192.0.2.4=a@example.com")
    check("an exact duplicate is harmless", m == {"192.0.2.4": "a@example.com"} and not p)
    m, p = parse("192.0.2.4=a@example.com,192.0.2.4=b@example.com,192.0.2.4=a@example.com")
    check("conflicting duplicates drop the address and report it",
          "192.0.2.4" not in m and len(p) >= 1)
    m, p = parse("2001:DB8:0:0:0:0:0:1=a@example.com,::ffff:192.0.2.9=b@example.com")
    check("IPv6 and IPv4-mapped spellings are canonicalised",
          m == {"2001:db8::1": "a@example.com", "192.0.2.9": "b@example.com"})
    m, p = parse("2001:db8::1=a@example.com,2001:0db8:0::1=b@example.com")
    check("different spellings of one IPv6 address conflict", not m and p)
    bad_entry = "192.0.2.77=a@exa\x07mple.com"
    _, p = parse(bad_entry)
    check("diagnostic escapes control characters", "\x07" not in p[0])

    buf = io.StringIO()
    configure(owners="notanip=a@example.com")
    with redirect_stderr(buf):
        web.device_owners()
    check("startup diagnostic names the bad entry", "notanip" in buf.getvalue())

    configure(owners="2001:db8::5=" + OWNER)
    whois_returns(tagged())
    _, ident = resolve("2001:0db8:0000:0000:0000:0000:0000:0005")
    check("IPv6 peer matches by canonical form", ident is not None and ident.login == OWNER)
    configure(owners="192.0.2.9=" + OWNER)
    _, ident = resolve("::ffff:192.0.2.9")
    check("IPv4-mapped peer matches the IPv4 entry", ident is not None and ident.login == OWNER)

    # ── forwarded headers and cookie persistence ────────────────────────────
    configure(owners=f"{DEVICE}={OWNER}")
    whois_returns(tagged())
    seen = []
    def whois_by_ip(ip):
        seen.append(ip)
        return tagged() if ip == DEVICE else None
    web.tailscale_whois = whois_by_ip
    web.OPERATOR_REGISTRY = reg = web.OperatorRegistry()
    spoof = {"X-Forwarded-For": DEVICE, "X-Real-IP": DEVICE, "Forwarded": f"for={DEVICE}",
             "Tailscale-User-Login": OWNER}
    with redirect_stderr(io.StringIO()):
        _t, ident, _n = handler_for(reg, "tok-spoof", "203.0.113.5", spoof)._resolve_identity()
    check("forwarded headers do not select the peer",
          seen == ["203.0.113.5"] and ident.source == web.IDENTITY_SOURCE_PENDING
          and not can_operate(ident))

    with redirect_stderr(io.StringIO()):
        _t, ident, _n = handler_for(reg, "tok-dev", DEVICE)._resolve_identity()
    check("direct request from the mapped device is the operator", can_operate(ident))
    with redirect_stderr(io.StringIO()):
        _t, ident, _n = handler_for(reg, "tok-dev", DEVICE)._resolve_identity()
    check("same cookie, same device: still the operator from cache", can_operate(ident))
    with redirect_stderr(io.StringIO()):
        _t, ident, _n = handler_for(reg, "tok-dev", "203.0.113.5")._resolve_identity()
    check("the delegated cookie from another address loses its powers",
          not can_operate(ident) and not ident.delegated_from)
    reg._last_retry_at["tok-dev"] = 0.0     # open the normal once-a-minute retry window
    with redirect_stderr(io.StringIO()):
        _t, ident, _n = handler_for(reg, "tok-dev", DEVICE)._resolve_identity()
    check("the dropped cookie back on the device re-derives the operator", can_operate(ident))

    # Removing the mapping (restart with the entry gone) revokes the cached cookie.
    configure(owners="")
    web.tailscale_whois = lambda ip: None
    with redirect_stderr(io.StringIO()):
        _t, ident, _n = handler_for(reg, "tok-dev", DEVICE)._resolve_identity()
    check("removing the mapping revokes a cached delegated identity", not can_operate(ident))

    # Non-delegated cookies keep their existing behaviour.
    configure(owners="")
    web.tailscale_whois = lambda ip: user_device(OWNER) if ip == DEVICE else None
    with redirect_stderr(io.StringIO()):
        handler_for(reg, "tok-plain", DEVICE)._resolve_identity()
        _t, ident, _n = handler_for(reg, "tok-plain", "203.0.113.5")._resolve_identity()
    check("a non-delegated cached identity is unchanged from another address",
          can_operate(ident))
finally:
    web.tailscale_whois = _real_whois
    web.OPERATOR_REGISTRY = _real_registry
    web._tailnet_owner_cache = None
    for k in ("NTH_TAILNET_DEVICE_OWNERS", "NTH_TAILNET_MEMBERS", "NTH_TAILNET_DEVICE_OWNER_NAME"):
        os.environ.pop(k, None)

print()
print(f"{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
