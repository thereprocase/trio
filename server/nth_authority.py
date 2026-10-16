"""Owner-issued channel-creation delegation, independent of session roles.

Only authenticated owner HTTP handlers write grants. MCP callers can read a
grant after proving their existing agent identity; names never confer power.
Member-admin currently delegates channel creation, not host/agent controls or
the ability to grant roles to anyone else.
"""
import sqlite3


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS member_authority (
        member_id TEXT PRIMARY KEY,
        member_admin INTEGER NOT NULL CHECK(member_admin IN (0,1)),
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")


def is_member_admin(db, member_id):
    if not member_id:
        return False
    try:
        row = db.execute("SELECT member_admin FROM member_authority WHERE member_id=?",
                         (member_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return False
        raise
    return bool(row and row[0] == 1)


def set_member_admin(db, member_id, enabled, owner_id, now):
    if type(enabled) is not bool or not member_id or not owner_id:
        raise ValueError("invalid authority grant")
    ensure_schema(db)
    db.execute("""INSERT INTO member_authority
        (member_id,member_admin,updated_by,updated_at) VALUES (?,?,?,?)
        ON CONFLICT(member_id) DO UPDATE SET member_admin=excluded.member_admin,
        updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
        (member_id, int(enabled), owner_id, now))


# Origin is captured from the transport peer, never an MCP argument or hostname.
# A reclaim preserves the initial provenance instead of transferring ownership.
def remember_origin(db, agent_id, login, now):
    if not login:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS agent_origins (
        agent_id TEXT PRIMARY KEY, tailnet_login TEXT NOT NULL,
        verified_at TEXT NOT NULL)""")
    db.execute("INSERT OR IGNORE INTO agent_origins VALUES (?,?,?)",
               (agent_id, login.strip().lower(), now))


def owned_agent_ids(db, login):
    if not login:
        return set()
    try:
        return {row[0] for row in db.execute(
            "SELECT agent_id FROM agent_origins WHERE tailnet_login=?",
            (login.strip().lower(),))}
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return set()
        raise


def verified_peer_login(peer):
    """Bounded daemon lookup of an actual Tailscale transport address."""
    import ipaddress
    import json
    import subprocess
    try:
        address = ipaddress.ip_address(peer)
    except (ValueError, TypeError):
        return None
    if not (address in ipaddress.ip_network('100.64.0.0/10') or
            address in ipaddress.ip_network('fd7a:115c:a1e0::/48')):
        return None
    for cli in ('/usr/bin/tailscale', '/usr/local/bin/tailscale'):
        try:
            result = subprocess.run([cli, 'whois', '--json', str(address)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3,
                check=True)
            if len(result.stdout) > 262144:
                return None
            login = json.loads(result.stdout).get('UserProfile', {}).get('LoginName')
            return login.strip().lower() if isinstance(login, str) and login.strip() else None
        except FileNotFoundError:
            continue
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError, TypeError):
            return None
    return None
