# Shared constants for the nth MCP server and event monitor.
# Both nth_server.py and nth_monitor.py import from here.

SLEEPING_KEYWORDS = ("idle", "standing by", "tier 3", "agent-monitor")

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
    """Return (name, emoji) for a member_id. Deterministic and stable."""
    h = 0
    for c in member_id or "":
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return ANIMAL_EMOJIS[h % len(ANIMAL_EMOJIS)]
