# Shared constants for the nth MCP server and event monitor.
# Both nth_server.py and nth_monitor.py import from here.

# Single source of truth for the release version. Surfaces in the startup
# banner, the hub's /healthz + /fleet endpoints, node check-ins, and
# nth_doctor's local-vs-hub version match.
NTH_VERSION = "8.0.2-beta.1"

SLEEPING_KEYWORDS = ("idle", "standing by", "tier 3", "agent-monitor")

# ── Context snapshot projection ───────────────────────────────────────
# Statusline/publisher snapshots carry far more than the UI renders:
# transcript paths, working directories, project dirs and cumulative API
# spend. Those reach unauthenticated viewers over /api/events and
# /api/landing, and the relayed blob is caller-controlled, so both the
# store side (nth_server) and the read side (nth_web) project onto this
# allowlist. Add a key here only when the UI actually renders it.
CONTEXT_ALLOWED_KEYS = (
    "session_id", "session_name", "used_pct", "cw_size",
    "model", "effort", "source", "ts", "last_event_ts", "data_age_s",
    "_age_s", "_relayed_at",
)
# Nested under "harness": only the two subtrees the drill-down reads.
CONTEXT_ALLOWED_HARNESS = ("context_window", "rate_limits")
# Longest string we keep for any scalar field — a relayed snapshot is
# untrusted input, and these all render into a narrow stats column.
CONTEXT_MAX_STR = 200


def project_context(ctx):
    """Return a copy of a context snapshot with only renderable fields.

    Drops unknown keys entirely (so a hostile or future-expanded payload
    cannot ride the relay into the page), coerces scalars to short
    strings/numbers, and keeps the two harness subtrees the UI reads.
    Returns None if the input is not a dict.
    """
    if not isinstance(ctx, dict):
        return None

    def _scalar(v):
        if v is None or isinstance(v, (bool, int, float)):
            return v
        return str(v)[:CONTEXT_MAX_STR]

    def _clean(sub, depth):
        """Scalars, plus one more level of dict-of-scalars.

        rate_limits nests as {five_hour: {used_percentage: N}}, so a
        scalars-only filter here silently drops the 5h/7d rows.
        """
        cleaned = {}
        for k, v in sub.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                cleaned[k] = _scalar(v)
            elif isinstance(v, dict) and depth > 0:
                cleaned[k] = _clean(v, depth - 1)
        return cleaned

    out = {}
    for key in CONTEXT_ALLOWED_KEYS:
        if key in ctx:
            out[key] = _scalar(ctx[key])

    harness = ctx.get("harness")
    if isinstance(harness, dict):
        keep = {}
        for key in CONTEXT_ALLOWED_HARNESS:
            sub = harness.get(key)
            if isinstance(sub, dict):
                keep[key] = _clean(sub, 1)
        if keep:
            out["harness"] = keep
    return out

# ── Animal emoji avatars ──────────────────────────────────────────────
# Stable, per-member visual identity. Curated to avoid confusable pairs
# (e.g. only one dog, one cat). Member_id hashes into this list to pick
# an avatar that stays the same for the life of the member row.
ANIMAL_EMOJIS = [
    ("fox",      "🦊"), ("panda",    "🐼"), ("owl",      "🦉"),
    ("octopus",  "🐙"), ("koala",    "🐨"), ("tiger",    "🐯"),
    ("lion",     "🦁"), ("wolf",     "🐺"), ("bear",     "🐻"),
    ("raccoon",  "🦝"), ("badger",   "🦡"), ("otter",    "🦦"),
    ("skunk",    "🦨"), ("deer",     "🦌"), ("bison",    "🦬"),
    ("goat",     "🐐"), ("ram",      "🐏"), ("horse",    "🐴"),
    ("unicorn",  "🦄"), ("zebra",    "🦓"), ("giraffe",  "🦒"),
    ("camel",    "🐪"), ("elephant", "🐘"), ("rhino",    "🦏"),
    ("hippo",    "🦛"), ("kangaroo", "🦘"), ("sloth",    "🦥"),
    ("hedgehog", "🦔"), ("bat",      "🦇"), ("rabbit",   "🐰"),
    ("mouse",    "🐭"), ("chipmunk", "🐿️"), ("beaver",   "🦫"),
    ("dog",      "🐶"), ("cat",      "🐱"), ("boar",     "🐗"),
    ("cow",      "🐮"), ("pig",      "🐷"), ("frog",     "🐸"),
    ("monkey",   "🐵"), ("gorilla",  "🦍"), ("orangutan","🦧"),
    ("rooster",  "🐓"), ("penguin",  "🐧"), ("duck",     "🦆"),
    ("swan",     "🦢"), ("eagle",    "🦅"), ("peacock",  "🦚"),
    ("parrot",   "🦜"), ("flamingo", "🦩"), ("dove",     "🕊️"),
    ("crocodile","🐊"), ("turtle",   "🐢"), ("snake",    "🐍"),
    ("lizard",   "🦎"), ("dragon",   "🐉"), ("trex",     "🦖"),
    ("whale",    "🐳"), ("dolphin",  "🐬"), ("shark",    "🦈"),
    ("fish",     "🐟"), ("crab",     "🦀"), ("lobster",  "🦞"),
    ("squid",    "🦑"),
]
# Invariant: list is a stable canonical order. Never reorder or insert
# in the middle — only append new animals at the end. Reordering would
# reassign every member's avatar.


def animal_for(member_id: str) -> tuple[str, str]:
    """Return (name, emoji) for a member_id. Deterministic and stable.

    No collision avoidance — two different member_ids can hash to the
    same slot. Use animal_for_channel() when rendering a roster where
    per-channel uniqueness matters.
    """
    h = 0
    for c in member_id or "":
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return ANIMAL_EMOJIS[h % len(ANIMAL_EMOJIS)]


def animal_for_channel(member_ids):
    """Assign a unique (name, emoji) to every member in a channel.

    Returns a dict {member_id: (name, emoji)}. Members are resolved in
    sorted member_id order (stable across reorderings of the input) and
    each collision linearly probes to the next free slot. When the
    roster exceeds the avatar pool (currently 63), later members wrap
    and collisions are unavoidable — they fall back to the plain hash
    pick for those overflow members only.
    """
    pool_size = len(ANIMAL_EMOJIS)
    taken = set()
    result = {}
    for mid in sorted(set(member_ids or [])):
        h = 0
        for c in mid or "":
            h = (h * 31 + ord(c)) & 0xFFFFFFFF
        start = h % pool_size
        pick = start
        for _ in range(pool_size):
            if pick not in taken:
                break
            pick = (pick + 1) % pool_size
        taken.add(pick)
        result[mid] = ANIMAL_EMOJIS[pick]
    return result
