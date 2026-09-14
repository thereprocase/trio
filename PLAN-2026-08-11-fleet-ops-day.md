# Fleet ops day — 2026-08-11, start 12:00 EDT

One-day project to make hub/spoke observable and un-breakable. Runs in the
cachy5540 Claude session that wrote it (Fable, autonomous, operator reachable by
phone). Everything lands in this repo, then deploys to both live machines the
same day.

Context: the 5-PR merge sweep this morning (2026-08-11) surfaced the failures
this plan fixes — the spoke's stdio registration died silently when Arch bumped
Python 3.12→3.14 (user-site `mcp` orphaned), and the PVE hub had drifted from
the repo for two months (hand-patched `quartet_server.py`, unit-file/drop-in
mismatch, nonstandard DB path via de-rooted HOME).

## Ground rules

- Repo is source of truth; every phase ends with a commit. Deploys go
  repo → install locations, never hand-edits on the target.
- Hub deploys: `/opt/quartet-hub/` on PVE (`root@YOUR_HUB`), backup as
  `<file>.bak-YYYYMMDD` before overwrite, `py_compile` on target before restart.
- Spoke deploys: `~/.claude/skills/{nth,trio,quartet}/` locally.
- Supply-chain: the ONLY new install is `mcp` (+deps) and `uvicorn` from PyPI
  into a fresh dedicated venv — packages already used by this project, latest
  stable, no lifecycle scripts (pip wheels). Operator pre-approved 2026-08-11.
- Every security/behavioral claim in the wrap-up labeled VERIFIED/LIKELY/etc.

## Phases

### P1 — venv registration (+ fix this box)  ~45 min
- `setup.sh`: create `~/.claude/nth/venv`, install `mcp` (hub also `uvicorn`),
  register `nth-trio` against `<venv>/bin/python`. Idempotent: re-run upgrades
  in place. Rename `remote` mode → `spoke` (keep `remote` as alias).
- Run it here: cachy5540's dead `nth-trio` comes back. VERIFY: venv python
  imports FastMCP; `claude mcp list` shows the venv path.

### P2 — version + health + fleet plumbing  ~1.5 h
- `nth_constants.py`: add `NTH_VERSION` (single source; bump to v7.3).
- `nth_server.py`: `nodes` table (hostname, transport, nth_version, python,
  last_seen, pid) + upsert on session init; lightweight check-in tool or
  piggyback on existing heartbeat writes for stdio; SSE spokes check in via
  the session-init path on the hub.
- `quartet_server.py`: plain HTTP `GET /healthz` → `{version, db_ok, channels,
  nodes}` and `GET /fleet` → nodes + channel liveness JSON. No auth needed —
  read-only counts/names, tailnet+LAN exposure only (matches nth_web posture).
- VERIFY: curl both endpoints on a locally-launched hub; unit test for the
  nodes upsert (tests/ pattern from this morning's PR #8).

### P3 — `nth doctor` (+ `--watch`)  ~1.5 h
- New `server/nth_doctor.py` (stdlib only): green/red table — registration
  present & python imports mcp; DB opens; hub URL from `~/.claude.json`
  answers `/healthz`; version match local vs hub; monitor process alive;
  last check-in age per known node. `--watch` = same table, 5s refresh,
  plain ANSI (no Rich dependency in doctor; keep it runnable anywhere).
- `setup.sh` installs a `nth-doctor` launcher into `~/.local/bin/`.
- VERIFY: run on cachy5540 (expect all green after P1) and on PVE.

### P4 — nth_web landing page  ~2 h
- `nth_web.py` with NO channel arg → serve `/`: hub health strip, fleet table
  (green/amber/red by check-in age), channel list with member/live/msg counts,
  each linking to the existing per-channel view (`/c/<code>` or `?channel=`).
  Reuse the existing embedded-JS/theme machinery; counts and ages only.
- VERIFY: `py_compile` + `node --check` on extracted JS (placeholder-substitution
  trick from this morning); manual curl of `/` and one channel page.

### P5 — hub owns its deployment  ~1 h
- `setup.sh hub`: write `quartet-hub.service` (de-rooted HOME=/var/lib/
  quartet-hub, StateDirectory, ExecStart /opt/quartet-hub — the real PVE
  shape, replacing the unit/drop-in mismatch found today) + `setup.sh upgrade`:
  rsync repo→/opt/quartet-hub, py_compile, restart, curl /healthz.
- Add persistent `nth-web.service` on PVE (landing page, `--tailnet`, port
  8765) so there is a permanent "wtf is up" URL.
- Update pve-dashboard quartet card collector to read `/healthz` (fall back
  to direct DB if unreachable) and add a spokes row. Backup-then-deploy per
  ground rules.
- VERIFY: hub restarted on new unit; /healthz 200; dashboard card shows
  version + spokes; trio/quartet round-trip message from this session.

### P6 — docs + ship  ~45 min
- CURRENT.md (v7.3 snapshot), CHANGELOG.md (rationale), TODO.md (close items,
  add follow-ups: MagicDNS/auto-discovery spoke setup, hub-version nag in
  poll footer), SKILL updates if tool surface changed. Commit, push, redeploy
  spoke skills. Final wrap-up message to operator with VERIFIED labels.

## Timeline (EDT)

12:00 P1 → 12:45 P2 → 14:15 P3 → 15:45 P4 → 17:45 P5 → 18:45 P6 → ~19:30 done.
Slack is real (estimates are Fermi); if running >1h behind by 16:00, cut scope
in this order: doctor `--watch`, dashboard-card spokes row, nth-web.service
(landing page still ships, run manually). Core that MUST ship today: P1, P2,
P3 one-shot doctor, P6.

## Risks / notes

- Hub restart drops live SSE sessions briefly — the auto-reinit shim (#4)
  absorbs it; do restarts back-to-back, not scattered.
- The dead-spoke problem is representation, not transport: a spoke that never
  checks in shows as "missing", which is the point.
- Don't touch `/opt/vmux*`, coolercontrol, or anything on the dell-fan-guard
  never-touch list; PVE work is limited to /opt/quartet-hub, /opt/pve-dashboard,
  and the two systemd units named above.
