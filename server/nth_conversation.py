"""Conversation identity — one stable name for one conversation.

A workspace has three kinds of conversation: a channel, a direct message, and
an agent-to-agent thread the operator can audit but is not part of. The router,
the timeline, drafts, read watermarks, search results and archive rows all need
to agree on what to call each one.

WHY THIS EXISTS: the DM key used to be derived RELATIVE TO THE VIEWER. One
conversation between alice and bob produced three different names — "bob" when
alice asked, "alice" when bob asked, and "alice,bob" to an auditing operator.
Per-viewer state tolerates that. Shared deep links, a search index, and any
aggregate over conversations do not: you could not paste a thread URL to a
colleague, and a search hit could not be attributed to a canonical thread.

So the KEY is viewer-independent and the VIEW is not. Who the counterparts are,
what the thread is called on screen, and whether it belongs in "your DMs" or
the audit list are all computed per viewer from the same stable key.

The key is also PERSISTED (dm_archives.thread_key is half of a primary key), so
its format is data, not an implementation detail. Changing it later means a
backfill; changing it before the table has ever shipped costs nothing, which is
why it was worth doing up front.

Pure functions over ids — no DB access, no HTTP, no state.
"""
from __future__ import annotations

from typing import Iterable, List, NamedTuple, Sequence

# Kinds of conversation. `audit` is NOT a separate identity: an agent-to-agent
# thread has the same canonical key whoever looks at it, and "audit" only
# describes the relationship of a particular viewer to it. It is spelled out
# here because the router needs a word for that read-only view.
KIND_CHANNEL = "channel"
KIND_DM = "dm"

DM_PREFIX = "dm:"
CHANNEL_PREFIX = "channel:"


class ConversationId(NamedTuple):
    """A conversation's stable identity: its kind, and its key within that
    kind. `str(cid)` is the wire/URL form."""
    kind: str
    key: str

    def __str__(self) -> str:
        prefix = CHANNEL_PREFIX if self.kind == KIND_CHANNEL else DM_PREFIX
        return f"{prefix}{self.key}"


def canonical_dm_key(participants: Iterable[str]) -> str:
    """The stable key for a DM among `participants`, from anyone's point of view.

    Sorted and de-duplicated, so the same set of people always yields the same
    key no matter who is asking or who sent the message being looked at.
    Returns "" for a set that cannot be a conversation (fewer than two
    distinct participants) — a self-addressed row is not a thread.
    """
    ids = sorted({p for p in participants if p})
    if len(ids) < 2:
        return ""
    return ",".join(ids)


def dm_conversation_id(participants: Iterable[str]) -> ConversationId:
    """`canonical_dm_key` wrapped as a ConversationId. Key "" if not a thread."""
    return ConversationId(KIND_DM, canonical_dm_key(participants))


def channel_conversation_id(code: str) -> ConversationId:
    return ConversationId(KIND_CHANNEL, code)


def participants_in_key(thread_key: str) -> List[str]:
    """The member ids named by a DM key.

    Accepts the bare key ("a,b") or the prefixed form ("dm:a,b"), because the
    key travels both as a query parameter and inside a ConversationId string.

    Callers used to do `key.split(",")` inline against a format that had three
    different shapes, one of which ("group:a,b") produced "group:a" as an id
    and silently matched no member. Parsing lives here so that cannot recur.
    """
    if not thread_key:
        return []
    if thread_key.startswith(DM_PREFIX):
        thread_key = thread_key[len(DM_PREFIX):]
    return [part for part in thread_key.split(",") if part]


def parse_conversation_id(value: str) -> ConversationId:
    """Parse the wire form back into a ConversationId.

    An unprefixed value is read as a channel code, which is what the legacy
    `?channel=` parameter carries.
    """
    value = (value or "").strip()
    if value.startswith(DM_PREFIX):
        return ConversationId(KIND_DM, value[len(DM_PREFIX):])
    if value.startswith(CHANNEL_PREFIX):
        return ConversationId(KIND_CHANNEL, value[len(CHANNEL_PREFIX):])
    return ConversationId(KIND_CHANNEL, value)


def counterparts(thread_key: str, viewer_id: str) -> List[str]:
    """The participants of `thread_key` OTHER than the viewer.

    This is the viewer-relative part, kept deliberately separate from the key:
    it drives the display name and the "is every counterpart an archived agent"
    test, neither of which may leak back into the identity.
    """
    return [p for p in participants_in_key(thread_key) if p != viewer_id]


def viewer_is_participant(thread_key: str, viewer_id: str) -> bool:
    """Whether `viewer_id` is in the conversation — i.e. whether this is one of
    their own DMs rather than a thread they are auditing."""
    return bool(viewer_id) and viewer_id in participants_in_key(thread_key)


def message_participants(sender_id: str, recipients: Sequence[str]) -> List[str]:
    """Everyone involved in one DM row: its recipients plus its sender."""
    people = {r for r in (recipients or []) if r}
    if sender_id:
        people.add(sender_id)
    return sorted(people)
