"""
Claude Trio MCP Server — multi-participant async communication for Claude Code sessions.

Unlike Duo (2 members, turn-based), Trio supports N participants with fully async
messaging. Anyone can post anytime. Coordination happens through a shared message
log and an atomic task claim system.

Each Claude session spawns its own instance of this server (via mcp.json).
All instances share state through a SQLite database at ~/.claude/trio/trio.db.
"""

import json
import os
import random
import sqlite3
import time
import re
import hashlib
import string
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DB_DIR = Path.home() / ".claude" / "trio"
DB_PATH = DB_DIR / "trio.db"

CHANNEL_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]{0,31}$")
MAX_MESSAGE_LENGTH = 4000
MAX_MEMBERS = 20
STALE_THRESHOLD_SECONDS = 300  # 5 minutes without heartbeat = stale

mcp = FastMCP("trio")


def generate_channel_code(topic: str = "") -> str:
    """Generate a short channel code, optionally from a topic string."""
    if topic:
        slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:24]
        if slug and CHANNEL_CODE_PATTERN.match(slug):
            return slug
        h = hashlib.sha256(topic.encode()).hexdigest()[:8]
        return f"trio-{h}"
    return "trio-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def generate_member_id() -> str:
    """Short unique member identifier."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            code        TEXT PRIMARY KEY,
            status      TEXT NOT NULL DEFAULT 'active',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            ended_at    TEXT,
            ended_by    TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id          TEXT NOT NULL,
            channel     TEXT NOT NULL,
            name        TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '',
            skills      TEXT NOT NULL DEFAULT '',
            last_seen   TEXT,
            last_read   INTEGER NOT NULL DEFAULT 0,
            joined_at   TEXT NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (id, channel),
            FOREIGN KEY (channel) REFERENCES channels(code)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel     TEXT NOT NULL,
            member_id   TEXT NOT NULL,
            member_name TEXT,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (channel) REFERENCES channels(code)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel     TEXT NOT NULL,
            posted_by   TEXT NOT NULL,
            claimed_by  TEXT,
            status      TEXT NOT NULL DEFAULT 'open',
            description TEXT NOT NULL,
            result      TEXT,
            blocked_by  TEXT NOT NULL DEFAULT '[]',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            FOREIGN KEY (channel) REFERENCES channels(code)
        )
    """)
    # Index for efficient unread-message queries in trio_poll
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_messages_channel_id
        ON messages (channel, id)
    """)
    # Migration: add pinned_message_id column (v2 feature)
    for col, table, defn in [
        ("pinned_message_id", "channels", "INTEGER"),
        ("mentions", "messages", "TEXT NOT NULL DEFAULT ''"),
        ("blocked_by", "tasks", "TEXT NOT NULL DEFAULT '[]'"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


MAX_SUMMARY_LENGTH = 200
MAX_SKILLS_LENGTH = 200

CONVERSATIONS_DIR = DB_DIR / "conversations"


def export_conversation(db: sqlite3.Connection, channel: str) -> Path | None:
    """Export a channel's conversation to a markdown file."""
    try:
        row = db.execute(
            "SELECT * FROM channels WHERE code = ?", (channel,)
        ).fetchone()
        if not row:
            return None

        members = db.execute(
            "SELECT * FROM members WHERE channel = ? ORDER BY joined_at",
            (channel,),
        ).fetchall()

        messages = db.execute(
            "SELECT * FROM messages WHERE channel = ? ORDER BY id",
            (channel,),
        ).fetchall()

        tasks = db.execute(
            "SELECT * FROM tasks WHERE channel = ? ORDER BY id",
            (channel,),
        ).fetchall()

        CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = CONVERSATIONS_DIR / f"{channel}.md"

        lines = [
            f"# Trio: {channel}",
            f"",
            f"**Created:** {row['created_at']}",
            f"**Ended:** {row['ended_at'] or 'still active'}",
            f"",
            f"## Members",
            f"",
        ]
        for m in members:
            status = "active" if _is_member_active(m["last_seen"]) else "stale"
            lines.append(f"- **{m['name']}** ({status}): {m['summary']}")
            if m["skills"]:
                lines.append(f"  Skills: {m['skills']}")
        lines.extend(["", "---", ""])

        if tasks:
            lines.extend(["## Tasks", ""])
            for t in tasks:
                lines.append(f"- **#{t['id']}** [{t['status']}] {t['description']}")
                if t["claimed_by"]:
                    lines.append(f"  Claimed by: {t['claimed_by']}")
                if t["result"]:
                    lines.append(f"  Result: {t['result']}")
            lines.extend(["", "---", ""])

        for msg in messages:
            label = msg["member_name"] or msg["member_id"]
            lines.append(f"### [{label}]")
            lines.append(f"")
            lines.append(msg["content"])
            lines.append(f"")
            lines.append(f"---")
            lines.append(f"")

        log_path.write_text("\n".join(lines), encoding="utf-8")
        return log_path
    except Exception:
        return None


def validate_channel_code(code: str) -> str | None:
    """Return an error message if invalid, None if valid."""
    if not code:
        return "Channel code is required."
    if not CHANNEL_CODE_PATTERN.match(code):
        return (
            f'Invalid channel code "{code}". '
            "Must be lowercase alphanumeric with hyphens, 1-32 chars."
        )
    return None


def _channel_exists(db, code):
    return db.execute("SELECT 1 FROM channels WHERE code = ?", (code,)).fetchone()


def _get_channel(db, code):
    return db.execute("SELECT * FROM channels WHERE code = ?", (code,)).fetchone()


def _get_member(db, channel, member_id):
    return db.execute(
        "SELECT * FROM members WHERE channel = ? AND id = ?",
        (channel, member_id),
    ).fetchone()


def _is_member_active(last_seen: str | None) -> bool:
    """Compute liveness from last_seen timestamp vs wall clock."""
    if not last_seen:
        return False
    try:
        seen = datetime.fromisoformat(last_seen)
        return (datetime.now(timezone.utc) - seen).total_seconds() < STALE_THRESHOLD_SECONDS
    except (ValueError, TypeError):
        return False


# ── MCP Tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def trio_connect(
    summary: str,
    name: str = "",
    channel: str = "",
    topic: str = "",
    skills: str = "",
    pin_topic: bool = False,
) -> str:
    """Join a trio channel. Creates the channel if it doesn't exist.

    Unlike duo, trio channels support any number of participants.
    All participants see all messages. There are no turns — anyone
    can send at any time.

    Set pin_topic=True to auto-pin the topic as the channel objective
    when creating a new channel. Ignored when joining an existing channel.

    Returns a JSON object with:
      - "channel": the channel code (remember this for all subsequent calls)
      - "member_id": your unique ID (remember this too)
      - "action": "created" or "joined"
      - "members": list of current members (names, skills, summaries)
      - "recent_messages": last few messages for context

    Args:
        summary: Brief description of who you are and what you're working on
        name: Display name (e.g. "CAD-Agent", "Code-Reviewer")
        channel: Channel code to join. If empty, generates from topic or randomly.
        topic: Used to generate a readable channel code (ignored if channel given)
        skills: Comma-separated list of your skills/capabilities
    """
    if channel:
        err = validate_channel_code(channel)
        if err:
            return json.dumps({"error": err})
    else:
        channel = generate_channel_code(topic)

    if not name:
        name = f"Agent-{generate_member_id()[:4]}"

    # Cap input lengths to prevent bloated join messages and status renders
    summary = summary[:MAX_SUMMARY_LENGTH] if summary else ""
    skills = skills[:MAX_SKILLS_LENGTH] if skills else ""

    member_id = generate_member_id()
    now = now_iso()
    db = get_db()

    try:
        existing = _get_channel(db, channel)

        if existing:
            if existing["status"] == "ended":
                return json.dumps({"error": f'Channel "{channel}" has ended.'})

            # Check member count (all members who ever joined)
            count = db.execute(
                "SELECT COUNT(*) FROM members WHERE channel = ?",
                (channel,),
            ).fetchone()[0]
            if count >= MAX_MEMBERS:
                return json.dumps({"error": f"Channel is full ({MAX_MEMBERS} members)."})

            # Join existing channel
            db.execute(
                "INSERT INTO members (id, channel, name, summary, skills, last_seen, joined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (member_id, channel, name, summary, skills, now, now),
            )
            db.execute(
                "UPDATE channels SET updated_at = ? WHERE code = ?",
                (now, channel),
            )
            # Post a system-style join message
            db.execute(
                "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel, member_id, name, f"[joined] {name} — {summary}" + (f" (skills: {skills})" if skills else ""), now),
            )
            db.commit()
            action = "joined"
        else:
            # Create new channel
            db.execute(
                "INSERT INTO channels (code, status, created_at, updated_at) "
                "VALUES (?, 'active', ?, ?)",
                (channel, now, now),
            )
            db.execute(
                "INSERT INTO members (id, channel, name, summary, skills, last_seen, joined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (member_id, channel, name, summary, skills, now, now),
            )
            db.execute(
                "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel, member_id, name, f"[joined] {name} — {summary}" + (f" (skills: {skills})" if skills else ""), now),
            )
            # Pin the topic as the channel objective if requested
            if pin_topic and topic:
                pin_cur = db.execute(
                    "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (channel, member_id, name, f"[objective] {topic}", now),
                )
                db.execute(
                    "UPDATE channels SET pinned_message_id = ? WHERE code = ?",
                    (pin_cur.lastrowid, channel),
                )
            db.commit()
            action = "created"

        # Gather current state for the joiner
        members = db.execute(
            "SELECT id, name, summary, skills, last_seen FROM members WHERE channel = ? ORDER BY joined_at",
            (channel,),
        ).fetchall()

        recent = db.execute(
            "SELECT id, member_id, member_name, content, created_at FROM messages "
            "WHERE channel = ? ORDER BY id DESC LIMIT 10",
            (channel,),
        ).fetchall()

        # Set watermark to current latest message
        latest_id = recent[0]["id"] if recent else 0
        db.execute(
            "UPDATE members SET last_read = ? WHERE id = ? AND channel = ?",
            (latest_id, member_id, channel),
        )
        db.commit()

        # Fetch objective (pinned message) if any
        ch_row = _get_channel(db, channel)
        objective = None
        if ch_row and ch_row["pinned_message_id"]:
            pin_msg = db.execute(
                "SELECT content FROM messages WHERE id = ? AND channel = ?",
                (ch_row["pinned_message_id"], channel),
            ).fetchone()
            if pin_msg:
                objective = pin_msg["content"]

        resp = {
            "ok": True,
            "channel": channel,
            "member_id": member_id,
            "name": name,
            "action": action,
            "members": [
                {"id": m["id"], "name": m["name"], "summary": m["summary"],
                 "skills": m["skills"], "active": _is_member_active(m["last_seen"])}
                for m in members
            ],
            "recent_messages": [
                {"id": m["id"], "from": m["member_name"] or m["member_id"],
                 "content": m["content"], "at": m["created_at"]}
                for m in reversed(list(recent))
            ],
        }
        if objective:
            resp["objective"] = objective
        return json.dumps(resp)

    finally:
        db.close()


@mcp.tool()
def trio_send(channel: str, member_id: str, message: str, task: bool = False, pin: bool = False, blocked_by: str = "") -> str:
    """Send a message to the trio channel. No turns — send anytime.

    All members will see this message on their next poll.

    Set task=True to simultaneously post the message as a claimable task.
    Set pin=True to pin this message as the channel objective (shown in
    trio_status and trio_connect for new joiners). Only one pin per channel.

    Use blocked_by with task=True to declare dependencies. Pass a
    comma-separated list of task IDs (e.g. "3,5"). The task cannot be
    claimed until all blockers are done. This enforces critical-path
    sequencing — agents can only claim work whose prerequisites are complete.

    Args:
        channel: Channel code
        member_id: Your member ID (from trio_connect)
        message: Your message (max 4000 chars)
        task: If True, also create a claimable task from this message
        pin: If True, pin this message as the channel objective
        blocked_by: Comma-separated task IDs this task depends on (requires task=True)
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    if not message or not message.strip():
        return json.dumps({"error": "Message cannot be empty."})
    if len(message) > MAX_MESSAGE_LENGTH:
        return json.dumps({"error": f"Message too long ({len(message)} > {MAX_MESSAGE_LENGTH})."})

    db = get_db()
    try:
        ch = _get_channel(db, channel)
        if not ch:
            return json.dumps({"error": f'Channel "{channel}" not found.'})
        if ch["status"] == "ended":
            return json.dumps({"error": f'Channel "{channel}" has ended.'})

        member = _get_member(db, channel, member_id)
        if not member:
            return json.dumps({"error": "You are not a member of this channel."})

        now = now_iso()
        task_id = None

        if task:
            # Parse blocked_by into a validated list of task IDs
            blocker_ids = []
            if blocked_by and blocked_by.strip():
                try:
                    blocker_ids = [int(x.strip()) for x in blocked_by.split(",") if x.strip()]
                except ValueError:
                    return json.dumps({"error": "blocked_by must be comma-separated task IDs (e.g. '3,5')."})
                # Verify all blocker tasks exist in this channel
                for bid in blocker_ids:
                    exists = db.execute(
                        "SELECT id FROM tasks WHERE id = ? AND channel = ?",
                        (bid, channel),
                    ).fetchone()
                    if not exists:
                        return json.dumps({"error": f"Blocker task #{bid} not found in this channel."})
            blocked_by_json = json.dumps(blocker_ids) if blocker_ids else "[]"

            # Determine initial status: 'blocked' if has unfinished blockers, else 'open'
            initial_status = "open"
            if blocker_ids:
                done_count = db.execute(
                    f"SELECT COUNT(*) FROM tasks WHERE id IN ({','.join('?' * len(blocker_ids))}) "
                    "AND channel = ? AND status = 'done'",
                    (*blocker_ids, channel),
                ).fetchone()[0]
                if done_count < len(blocker_ids):
                    initial_status = "blocked"

            # Insert task row first to get the task_id for the message prefix
            msg_stripped = message.strip()
            cur = db.execute(
                "INSERT INTO tasks (channel, posted_by, status, description, blocked_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (channel, member_id, initial_status, msg_stripped, blocked_by_json, now, now),
            )
            task_id = cur.lastrowid
            suffix = ""
            if blocker_ids:
                suffix = f" (blocked by #{', #'.join(str(b) for b in blocker_ids)})"
            content = f"[task #{task_id}] {msg_stripped}{suffix}"
        elif pin:
            content = f"[pinned] {message.strip()}"
        else:
            content = message

        # Detect @mentions in content
        mention_ids = []
        if "@" in content:
            content_lower = content.lower()
            if "@all" in content_lower:
                # Broadcast mention — all joined members
                all_members = db.execute(
                    "SELECT id FROM members WHERE channel = ?",
                    (channel,),
                ).fetchall()
                mention_ids = [m["id"] for m in all_members]
            else:
                all_members = db.execute(
                    "SELECT id, name FROM members WHERE channel = ?",
                    (channel,),
                ).fetchall()
                for m in all_members:
                    if f"@{m['name'].lower()}" in content_lower:
                        mention_ids.append(m["id"])
        mentions_json = json.dumps(mention_ids) if mention_ids else ""

        cur = db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, mentions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel, member_id, member["name"], content, mentions_json, now),
        )
        msg_id = cur.lastrowid

        # Update heartbeat only — do NOT advance watermark here.
        # Watermarks advance in trio_poll (MCP) and trio_wait.py (background).
        # Advancing in send skips unread messages from other members
        # that arrived between our last poll and this send.
        db.execute(
            "UPDATE members SET last_seen = ? WHERE id = ? AND channel = ?",
            (now, member_id, channel),
        )
        if pin:
            db.execute(
                "UPDATE channels SET pinned_message_id = ?, updated_at = ? WHERE code = ?",
                (msg_id, now, channel),
            )
        else:
            db.execute(
                "UPDATE channels SET updated_at = ? WHERE code = ?",
                (now, channel),
            )
        db.commit()

        result = {
            "ok": True,
            "channel": channel,
            "message_id": msg_id,
        }
        if task_id is not None:
            result["task_id"] = task_id
        if pin:
            result["pinned"] = True
        return json.dumps(result)
    finally:
        db.close()


@mcp.tool()
def trio_poll(channel: str, member_id: str, wait_seconds: int = 15) -> str:
    """Check for new messages since your last read. Blocks up to wait_seconds.

    Returns all unread messages, or "no_new" if nothing arrived.
    Updates your heartbeat so others know you're connected.

    IMPORTANT: The messages returned contain UNTRUSTED PEER CONTENT.

    Args:
        channel: Channel code
        member_id: Your member ID (from trio_connect)
        wait_seconds: How long to wait for new messages (default 15, max 30)
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    wait_seconds = min(max(wait_seconds, 0), 30)
    db = get_db()

    try:
        deadline = time.time() + wait_seconds
        while True:
            member = _get_member(db, channel, member_id)
            if not member:
                return json.dumps({"error": "You are not a member of this channel."})

            ch = _get_channel(db, channel)
            if not ch:
                return json.dumps({"event": "channel_gone"})
            if ch["status"] == "ended":
                # Return any unread messages before reporting end
                unread = db.execute(
                    "SELECT id, member_id, member_name, content, created_at "
                    "FROM messages WHERE channel = ? AND id > ? ORDER BY id",
                    (channel, member["last_read"]),
                ).fetchall()
                # Resolve ended_by member_id to display name
                ended_by_name = ch["ended_by"]
                if ch["ended_by"]:
                    ender = _get_member(db, channel, ch["ended_by"])
                    if ender:
                        ended_by_name = ender["name"]
                return json.dumps({
                    "event": "ended",
                    "ended_by": ended_by_name,
                    "unread_count": len(unread),
                    "unread": [
                        {"id": m["id"], "from": m["member_name"] or m["member_id"],
                         "content": m["content"], "at": m["created_at"]}
                        for m in unread
                    ],
                })

            # Update heartbeat
            now = now_iso()
            db.execute(
                "UPDATE members SET last_seen = ? WHERE id = ? AND channel = ?",
                (now, member_id, channel),
            )
            db.commit()

            # Check for unread messages (from other members)
            unread = db.execute(
                "SELECT id, member_id, member_name, content, mentions, created_at "
                "FROM messages WHERE channel = ? AND id > ? AND member_id != ? ORDER BY id",
                (channel, member["last_read"], member_id),
            ).fetchall()

            if unread:
                # Advance watermark to the max ID of returned messages only.
                # Using MAX(id) over the whole channel could skip messages
                # committed concurrently with a higher ID than what we fetched.
                max_id = max(m["id"] for m in unread)
                db.execute(
                    "UPDATE members SET last_read = ? WHERE id = ? AND channel = ?",
                    (max_id, member_id, channel),
                )
                db.commit()

                # Enrich with mention flags
                has_mentions = False
                msg_list = []
                for m in unread:
                    mentions_raw = m["mentions"] if m["mentions"] else ""
                    try:
                        mention_list = json.loads(mentions_raw) if mentions_raw else []
                    except (json.JSONDecodeError, TypeError):
                        mention_list = []
                    mentioned = member_id in mention_list
                    if mentioned:
                        has_mentions = True
                    entry = {
                        "id": m["id"],
                        "from": m["member_name"] or m["member_id"],
                        "content": m["content"],
                        "at": m["created_at"],
                    }
                    if mentioned:
                        entry["mentioned"] = True
                    msg_list.append(entry)

                resp = {
                    "event": "new_messages",
                    "unread_count": len(msg_list),
                    "messages": msg_list,
                }
                if has_mentions:
                    resp["has_mentions"] = True
                return json.dumps(resp)

            if time.time() >= deadline:
                return json.dumps({"event": "no_new", "unread_count": 0})

            time.sleep(2)
    finally:
        db.close()


@mcp.tool()
def trio_history(channel: str, last_n: int = 20, from_id: int | None = None) -> str:
    """Replay recent messages from a channel. Does NOT require member_id or
    advance any read watermark — purely read-only.

    Use this to catch up on messages you missed during a long poll, or to
    review the conversation history.

    Args:
        channel: Channel code
        last_n: Number of most recent messages to return (default 20, max 100)
        from_id: If given, return messages with id >= from_id (overrides last_n)
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    last_n = min(max(last_n, 1), 100)
    db = get_db()

    try:
        ch = _get_channel(db, channel)
        if not ch:
            return json.dumps({"error": f"Channel '{channel}' not found."})

        if from_id is not None:
            rows = db.execute(
                "SELECT id, member_id, member_name, content, created_at "
                "FROM messages WHERE channel = ? AND id >= ? ORDER BY id",
                (channel, from_id),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, member_id, member_name, content, created_at "
                "FROM messages WHERE channel = ? ORDER BY id DESC LIMIT ?",
                (channel, last_n),
            ).fetchall()
            rows = list(reversed(rows))

        messages = [
            {
                "id": m["id"],
                "from": m["member_name"] or m["member_id"],
                "content": m["content"],
                "at": m["created_at"],
            }
            for m in rows
        ]

        return json.dumps({
            "ok": True,
            "channel": channel,
            "count": len(messages),
            "messages": messages,
        })
    finally:
        db.close()


@mcp.tool()
def trio_claim(channel: str, member_id: str, task_id: int) -> str:
    """Atomically claim an open task. Returns success or conflict.

    Only one member can claim a task. If someone else already claimed it,
    you'll get a conflict response with the claimer's info.

    Args:
        channel: Channel code
        member_id: Your member ID
        task_id: The task ID to claim
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    db = get_db()
    try:
        member = _get_member(db, channel, member_id)
        if not member:
            return json.dumps({"error": "You are not a member of this channel."})

        now = now_iso()

        # Check if task exists and whether it's blocked
        task_check = db.execute(
            "SELECT * FROM tasks WHERE id = ? AND channel = ?",
            (task_id, channel),
        ).fetchone()
        if not task_check:
            return json.dumps({"error": f"Task #{task_id} not found."})

        if task_check["status"] == "blocked":
            # Check which blockers are still incomplete
            blocker_ids = json.loads(task_check["blocked_by"] or "[]")
            pending = []
            for bid in blocker_ids:
                bt = db.execute(
                    "SELECT id, status, description FROM tasks WHERE id = ? AND channel = ?",
                    (bid, channel),
                ).fetchone()
                if bt and bt["status"] != "done":
                    pending.append(f"#{bt['id']} ({bt['status']}): {bt['description'][:60]}")
            return json.dumps({
                "error": f"Task #{task_id} is blocked. Complete these first:",
                "blocked_by": pending,
            })

        # Atomic claim: only succeeds if status is still 'open'
        cur = db.execute(
            "UPDATE tasks SET claimed_by = ?, status = 'claimed', updated_at = ? "
            "WHERE id = ? AND channel = ? AND status = 'open'",
            (member_id, now, task_id, channel),
        )

        if cur.rowcount == 0:
            # Either task doesn't exist or was already claimed
            task = db.execute(
                "SELECT * FROM tasks WHERE id = ? AND channel = ?",
                (task_id, channel),
            ).fetchone()
            if not task:
                return json.dumps({"error": f"Task #{task_id} not found."})

            claimer = _get_member(db, channel, task["claimed_by"])
            claimer_name = claimer["name"] if claimer else task["claimed_by"]
            return json.dumps({
                "conflict": True,
                "task_id": task_id,
                "claimed_by": claimer_name,
                "status": task["status"],
            })

        # Read back the task description to include in the claim message
        task_row = db.execute(
            "SELECT description FROM tasks WHERE id = ? AND channel = ?",
            (task_id, channel),
        ).fetchone()
        task_desc = task_row["description"] if task_row else ""

        # Post claim message
        db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, member_id, member["name"],
             f"[claimed #{task_id}] {task_desc}", now),
        )
        db.commit()

        return json.dumps({
            "ok": True,
            "task_id": task_id,
            "claimed_by": member["name"],
        })
    finally:
        db.close()


@mcp.tool()
def trio_complete(channel: str, member_id: str, task_id: int, result: str = "") -> str:
    """Mark a claimed task as done.

    Only the member who claimed the task can complete it.

    Args:
        channel: Channel code
        member_id: Your member ID
        task_id: The task ID to complete
        result: Summary of what was done / the result
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    db = get_db()
    try:
        member = _get_member(db, channel, member_id)
        if not member:
            return json.dumps({"error": "You are not a member of this channel."})

        now = now_iso()
        cur = db.execute(
            "UPDATE tasks SET status = 'done', result = ?, updated_at = ? "
            "WHERE id = ? AND channel = ? AND claimed_by = ? AND status = 'claimed'",
            (result.strip() if result else None, now, task_id, channel, member_id),
        )

        if cur.rowcount == 0:
            task = db.execute(
                "SELECT * FROM tasks WHERE id = ? AND channel = ?",
                (task_id, channel),
            ).fetchone()
            if not task:
                return json.dumps({"error": f"Task #{task_id} not found."})
            if task["status"] == "done":
                return json.dumps({"error": f"Task #{task_id} is already done."})
            if task["status"] == "open":
                return json.dumps({"error": f"Task #{task_id} is not claimed yet. Claim it first."})
            if task["claimed_by"] != member_id:
                return json.dumps({"error": f"Task #{task_id} is claimed by someone else."})
            return json.dumps({"error": f"Task #{task_id} cannot be completed (status: {task['status']})."})

        # Read back the task description for the done message
        task_row = db.execute(
            "SELECT description FROM tasks WHERE id = ? AND channel = ?",
            (task_id, channel),
        ).fetchone()
        task_desc = task_row["description"] if task_row else ""

        # Unblock downstream tasks whose blockers are now all done
        unblocked = []
        blocked_tasks = db.execute(
            "SELECT id, blocked_by, description FROM tasks WHERE channel = ? AND status = 'blocked'",
            (channel,),
        ).fetchall()
        for bt in blocked_tasks:
            blocker_ids = json.loads(bt["blocked_by"] or "[]")
            if task_id not in blocker_ids:
                continue
            # Check if ALL blockers for this task are now done
            all_done = True
            for bid in blocker_ids:
                blocker = db.execute(
                    "SELECT status FROM tasks WHERE id = ? AND channel = ?",
                    (bid, channel),
                ).fetchone()
                if not blocker or blocker["status"] != "done":
                    all_done = False
                    break
            if all_done:
                db.execute(
                    "UPDATE tasks SET status = 'open', updated_at = ? WHERE id = ? AND channel = ?",
                    (now, bt["id"], channel),
                )
                unblocked.append(f"#{bt['id']}")

        # Post completion message
        msg = f"[done #{task_id}] {task_desc}"
        if result:
            msg += f" — {result.strip()}"
        if unblocked:
            msg += f" — unblocked: {', '.join(unblocked)}"
        db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, member_id, member["name"], msg, now),
        )
        db.commit()

        resp = {
            "ok": True,
            "task_id": task_id,
        }
        if unblocked:
            resp["unblocked"] = unblocked
        return json.dumps(resp)
    finally:
        db.close()


@mcp.tool()
def trio_release(channel: str, member_id: str, task_id: int) -> str:
    """Release a claimed task back to open. Self-release only.

    Only the member who claimed the task can release it.
    To free another member's tasks, use trio_cull (requires user permission).

    Args:
        channel: Channel code
        member_id: Your member ID
        task_id: The task ID to release
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    db = get_db()
    try:
        member = _get_member(db, channel, member_id)
        if not member:
            return json.dumps({"error": "You are not a member of this channel."})

        task = db.execute(
            "SELECT * FROM tasks WHERE id = ? AND channel = ?",
            (task_id, channel),
        ).fetchone()
        if not task:
            return json.dumps({"error": f"Task #{task_id} not found."})
        if task["status"] == "open":
            return json.dumps({"error": f"Task #{task_id} is already open."})
        if task["status"] == "done":
            return json.dumps({"error": f"Task #{task_id} is already done. Cannot release."})

        # Self-release only — no releasing other members' tasks
        if task["claimed_by"] != member_id:
            claimer = _get_member(db, channel, task["claimed_by"])
            claimer_name = claimer["name"] if claimer else task["claimed_by"]
            return json.dumps({
                "error": f"Task #{task_id} is claimed by {claimer_name}. "
                         f"Only the claimer can release a task. Use trio_cull to remove a member and free their tasks."
            })

        now = now_iso()
        db.execute(
            "UPDATE tasks SET claimed_by = NULL, status = 'open', updated_at = ? "
            "WHERE id = ? AND channel = ?",
            (now, task_id, channel),
        )

        # Post release message
        task_desc = task["description"]
        claimer = _get_member(db, channel, task["claimed_by"])
        claimer_name = claimer["name"] if claimer else task["claimed_by"]
        db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, member_id, member["name"],
             f"[released #{task_id}] {task_desc} (was claimed by {claimer_name})", now),
        )
        db.commit()

        return json.dumps({
            "ok": True,
            "task_id": task_id,
            "released_from": claimer_name,
        })
    finally:
        db.close()


@mcp.tool()
def trio_status(channel: str) -> str:
    """Get full details for a trio channel: members, all tasks, message count.

    Args:
        channel: Channel code
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    db = get_db()
    try:
        ch = _get_channel(db, channel)
        if not ch:
            return json.dumps({"error": f'Channel "{channel}" not found.'})

        members = db.execute(
            "SELECT id, name, summary, skills, active, last_seen "
            "FROM members WHERE channel = ? ORDER BY joined_at",
            (channel,),
        ).fetchall()

        msg_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel = ?",
            (channel,),
        ).fetchone()[0]

        tasks = db.execute(
            "SELECT * FROM tasks WHERE channel = ? ORDER BY id",
            (channel,),
        ).fetchall()

        task_list = []
        for t in tasks:
            entry = {
                "id": t["id"],
                "status": t["status"],
                "description": t["description"],
                "posted_by": t["posted_by"],
                "created_at": t["created_at"],
                "updated_at": t["updated_at"],
            }
            if t["claimed_by"]:
                claimer = _get_member(db, channel, t["claimed_by"])
                entry["claimed_by"] = claimer["name"] if claimer else t["claimed_by"]
            if t["result"]:
                entry["result"] = t["result"]
            blocker_ids = json.loads(t["blocked_by"] or "[]")
            if blocker_ids:
                entry["blocked_by"] = blocker_ids
            task_list.append(entry)

        # Fetch objective (pinned message) if any
        objective = None
        if ch["pinned_message_id"]:
            pin_msg = db.execute(
                "SELECT content FROM messages WHERE id = ? AND channel = ?",
                (ch["pinned_message_id"], channel),
            ).fetchone()
            if pin_msg:
                objective = pin_msg["content"]

        resp = {
            "channel": channel,
            "status": ch["status"],
            "created_at": ch["created_at"],
            "members": [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "summary": m["summary"],
                    "skills": m["skills"],
                    "active": _is_member_active(m["last_seen"]),
                    "last_seen": m["last_seen"],
                }
                for m in members
            ],
            "message_count": msg_count,
            "tasks": task_list,
        }
        if objective:
            resp["objective"] = objective
        return json.dumps(resp)
    finally:
        db.close()


@mcp.tool()
def trio_end(channel: str, member_id: str) -> str:
    """End a trio channel. Exports the conversation to a markdown file.

    Any member can end the channel. All members will see the 'ended' event
    on their next poll.

    Args:
        channel: Channel code
        member_id: Your member ID
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    db = get_db()
    try:
        member = _get_member(db, channel, member_id)
        if not member:
            return json.dumps({"error": "You are not a member of this channel."})

        ch = _get_channel(db, channel)
        if not ch:
            return json.dumps({"error": f'Channel "{channel}" not found.'})
        if ch["status"] == "ended":
            return json.dumps({"error": "Channel already ended."})

        now = now_iso()
        db.execute(
            "UPDATE channels SET status = 'ended', ended_at = ?, ended_by = ?, updated_at = ? "
            "WHERE code = ?",
            (now, member_id, now, channel),
        )
        db.commit()

        log_path = export_conversation(db, channel)

        msg_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE channel = ?",
            (channel,),
        ).fetchone()[0]

        return json.dumps({
            "ok": True,
            "channel": channel,
            "ended_by": member["name"],
            "total_messages": msg_count,
            "log_file": str(log_path) if log_path else None,
        })
    finally:
        db.close()


@mcp.tool()
def trio_list() -> str:
    """List all trio channels on this machine."""
    db = get_db()
    try:
        channels = db.execute(
            "SELECT c.code, c.status, c.created_at, c.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.channel = c.code) as message_count "
            "FROM channels c ORDER BY c.updated_at DESC",
        ).fetchall()

        # Compute active member counts in Python to avoid SQLite ISO 8601 parsing issues
        result_list = []
        for c in channels:
            members = db.execute(
                "SELECT last_seen FROM members WHERE channel = ?",
                (c["code"],),
            ).fetchall()
            active_count = sum(1 for m in members if _is_member_active(m["last_seen"]))
            result_list.append({
                "channel": c["code"],
                "status": c["status"],
                "members": active_count,
                "messages": c["message_count"],
                "updated_at": c["updated_at"],
            })

        return json.dumps({"channels": result_list})
    finally:
        db.close()


@mcp.tool()
def trio_cull(channel: str, member_id: str, target_member_id: str) -> str:
    """Remove a member from a channel entirely.

    Deletes the target from the members table, releases their claimed
    tasks back to open, and posts a system message.

    IMPORTANT: Claudes must NEVER call this autonomously. Only on
    explicit user instruction.

    Args:
        channel: Channel code
        member_id: Your member ID (the caller)
        target_member_id: The member ID to remove
    """
    err = validate_channel_code(channel)
    if err:
        return json.dumps({"error": err})

    db = get_db()
    try:
        caller = _get_member(db, channel, member_id)
        if not caller:
            return json.dumps({"error": "You are not a member of this channel."})

        target = _get_member(db, channel, target_member_id)
        if not target:
            return json.dumps({"error": f"Member {target_member_id} not found in this channel."})

        if target_member_id == member_id:
            return json.dumps({"error": "Cannot cull yourself. Use trio_end to leave."})

        target_name = target["name"]
        now = now_iso()

        # Release any tasks claimed by the culled member
        released_tasks = db.execute(
            "SELECT id, description FROM tasks WHERE channel = ? AND claimed_by = ? AND status = 'claimed'",
            (channel, target_member_id),
        ).fetchall()
        if released_tasks:
            db.execute(
                "UPDATE tasks SET claimed_by = NULL, status = 'open', updated_at = ? "
                "WHERE channel = ? AND claimed_by = ? AND status = 'claimed'",
                (now, channel, target_member_id),
            )

        db.execute(
            "DELETE FROM members WHERE id = ? AND channel = ?",
            (target_member_id, channel),
        )

        released_ids = [t["id"] for t in released_tasks]
        cull_msg = f"[culled] {target_name} ({target_member_id}) removed from channel"
        if released_ids:
            cull_msg += f" — released tasks: {', '.join(f'#{tid}' for tid in released_ids)}"

        db.execute(
            "INSERT INTO messages (channel, member_id, member_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, member_id, caller["name"], cull_msg, now),
        )
        db.commit()

        return json.dumps({
            "ok": True,
            "culled": target_name,
            "culled_id": target_member_id,
            "released_tasks": released_ids,
        })
    finally:
        db.close()


@mcp.tool()
def trio_cleanup(channel: str = "", all_ended: bool = False) -> str:
    """Delete trio channels and their data.

    Args:
        channel: Specific channel to delete. Leave empty with all_ended=True to clean all ended channels.
        all_ended: If True, delete all ended channels.
    """
    db = get_db()
    try:
        deleted = []
        if channel:
            err = validate_channel_code(channel)
            if err:
                return json.dumps({"error": err})
            # Guard: refuse to delete active channels
            ch = _get_channel(db, channel)
            if ch and ch["status"] == "active":
                return json.dumps({"error": f'Channel "{channel}" is still active. End it first with trio_end.'})
            db.execute("DELETE FROM tasks WHERE channel = ?", (channel,))
            db.execute("DELETE FROM messages WHERE channel = ?", (channel,))
            db.execute("DELETE FROM members WHERE channel = ?", (channel,))
            db.execute("DELETE FROM channels WHERE code = ?", (channel,))
            deleted.append(channel)
        elif all_ended:
            ended = db.execute(
                "SELECT code FROM channels WHERE status = 'ended'"
            ).fetchall()
            for row in ended:
                code = row["code"]
                db.execute("DELETE FROM tasks WHERE channel = ?", (code,))
                db.execute("DELETE FROM messages WHERE channel = ?", (code,))
                db.execute("DELETE FROM members WHERE channel = ?", (code,))
                db.execute("DELETE FROM channels WHERE code = ?", (code,))
                deleted.append(code)
        else:
            return json.dumps({"error": "Specify a channel or set all_ended=True."})

        db.commit()
        return json.dumps({"ok": True, "deleted": deleted})
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
