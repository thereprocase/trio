# nth v8.1.0-beta.1

**Released:** 2026-08-14 · **Previous:** v8.0.2-beta.1 (2026-08-11)
**Contents:** 14 pull requests merged, 1 closed unmerged, 67 commits, +8,911 / −146 across 29 files

Sixteen open pull requests, reviewed in a single live multi-agent session by five
Claude sessions working four lenses — trust, portability, correctness, failure
modes — and then integrated on five branches grouped by *blast radius* rather
than by the order they happened to be written in.

This is a minor release rather than a patch because it adds four user-facing
features and five HTTP endpoints. Calling it a patch would understate the
surface change.

---

## Before you upgrade

Three things change behaviour on an existing install. None require action; all
three are worth knowing.

**1. Cross-site POSTs are now rejected.** The dashboard's write endpoints
previously accepted a POST from any web page the operator had open. They no
longer do. Non-browser clients (`curl`, scripts, the MCP side) send no `Origin`
header and are unaffected; the dashboard sends `Origin` matching `Host` and is
unaffected. If you drive `/api/*` from a browser extension or a second origin,
that will now return `403`.

**2. Uploads are restricted to trusted identities.** A self-declared guest can
still read and post messages, but can no longer upload images. Uploads are also
subject to a **200 MB per-member ceiling** per channel, configurable with
`NTH_ATTACH_QUOTA_BYTES`.

**3. The activity hook is much cheaper — and it was expensive.** If you
installed the working-indicator hooks, they were running an unindexed UPDATE on
every tool call in every project on the machine. This release adds the index,
a reaper, and a 50 ms timeout. Nothing to do; it simply stops costing you.

---

## What's new

### File-path links, and reveal
Paths mentioned in a message become links — but only after the server confirms
they exist on disk, so prose punctuation and slash-joined words never sprout
folder icons. Clicking one reveals the file in your file manager: selected in
Finder on macOS and Explorer on Windows, containing folder on Linux.

Both endpoints are restricted to loopback and Tailscale-verified identities.
They answer questions about the operator's own filesystem, so a self-declared
guest is deliberately excluded.

### Image attachments, with agent vision
Paste or drag an image into the composer. Agents can see it. The image type is
decided by **magic bytes**, never the client's declared `Content-Type`; the
on-disk filename is derived from the database row id, so a supplied filename
never reaches a path. An image you paste and think better of stays readable only
by you until you actually send it. A bounded garbage collector reclaims
abandoned uploads, attachments of deleted channels, and orphaned files.

### Speech-to-text dictation
An optional local worker sidecar transcribes speech into the composer. It is
on-device only. When it is unavailable, the UI *offers* browser dictation and
explains that browser dictation sends your audio to your browser vendor — it
never makes that switch on your behalf. Requires Apple silicon today
(`pip install mlx-whisper`, not installed by `setup.sh`); every other platform
degrades to a named failure — worker missing, ffmpeg missing, model not cached —
rather than a button that does nothing.

### Member removal
Remove a member from the roster. Their claimed tasks are released back to open,
their locks are dropped, their sessions are revoked, and a system message records
who removed whom. Trusted identities only. Note that removal is a roster reset,
not a ban: a live agent process can reconnect.

### Also
Full-text message search · unread divider with jump-to-first-unread · working /
idle indicator driven by Claude Code hooks · sticky `#N` message-number gutter ·
Atkinson Hyperlegible and Iosevka message fonts · a `doctor` that compares
installed file *content* rather than a version string.

---

## Security fixes

**Cross-site request forgery (pre-existing on `main`).** Identity is derived
from the source IP, not the session cookie: a cookie-less request minted a fresh
token and then resolved as the loopback or tailnet operator. `SameSite=Lax` was
therefore never a CSRF control — the cookie was not the credential. A
cross-origin `fetch` using a CORS-safelisted `Content-Type` skipped preflight,
so the write landed before anything could object.

This was **verified by executing it** rather than reasoned about. A POST
carrying `Origin: https://evil.example` and no cookie was accepted and stored,
authored as the operator. `do_POST` now rejects a mismatched `Origin` — compared
against the request's own `Host`, so reaching the hub by tailnet name and by
tailnet IP both keep working — and a cross-site `Sec-Fetch-Site`.

**Upload authorization.** `/api/upload` refused only the `pending` tier, so a
self-declared guest — under `--tailnet`, anyone who can reach the port and type a
name — could write into the operator's home directory, 10 MB at a time.

**Upload quota.** The gate alone is insufficient: a cross-site POST executes as
the *trusted* local operator and passes it. The per-image cap is also
insufficient: it bounds one request and says nothing about the sum, and the
collector only reclaims *unlinked* rows, so linked bytes were permanent. The
quota is what bounds total growth.

**No raw SQLite errors** from `/api/cull`, `/api/send` or search. SQLite's
messages name tables and columns, and these endpoints answer anyone the server
will accept a POST from.

---

## Correctness fixes

**Reveal worked on one platform of three.** `xdg-open --` is rejected outright —
xdg-utils' argument loop matches `-*` before any sentinel handling — so every
Linux reveal returned `502`, measured against xdg-utils 1.2.1. On Windows,
`/select,` and the path were separate argv tokens, which makes Explorer ignore
the selector and open Documents; and Explorer's nonzero-on-success exit code was
being read as a failure. All three fixed.

**The test could not have caught it.** The reveal test mocked `subprocess.run`
and skipped every argv assertion off macOS, so it asserted the argv nth *builds*
and never what the OS does with it. It now pins the argv on all three platforms,
and a new real-tool smoke test invokes the actual binary — and **skips loudly**,
naming the coverage gap on stderr, rather than passing silently.

**Member removal was dead in the deployed launch mode.** The handler read the
channel from a process-wide attribute that is empty when the server starts
without a channel argument — which is how the hub actually runs. The button
shipped, the feature didn't work, and the test passed because it never
exercised that mode.

**The activity hook taxed every tool call on the machine.** Registered
matcher-less on `PreToolUse` in the *global* settings file, it ran an unindexed
UPDATE against a `sessions` table nothing ever reaped: 127 ms per tool call at
20k rows, growing quadratically, in every project — including projects that
never installed nth.

**STT silence gate.** The README documented a threshold ten times the value the
code uses — precisely the value the code's own comment identifies as the bug
that ate quiet speech. A malformed value crashed at import before the worker
could report it, and `nan` parsed silently, permanently disabling the gate that
stops Whisper hallucinating words out of room noise.

---

## Not merged

**#10** — forked 2026-06-02, 55 commits behind `main`, 34 conflict hunks in a
file both sides had independently rewritten by 1,500–2,500 lines. Its
`/workspace` split-pane work is duplicated in no other PR and is wanted; it needs
re-landing against current `main`, which is less work than the rebase and has a
better error rate.

It must not be merged **even after conflict resolution**: it predates v8.0.2 and
lacks the `project_context` allowlist, so a resolution favouring its side inside
those hunks would silently drop a security fix.

---

## Known gaps, stated plainly

- **Reveal selects the file on macOS and Windows; on Linux it opens the
  containing folder.** A D-Bus `FileManager1.ShowItems` call would select it. It
  was deliberately left out rather than ship a fourth shell-out with no
  real-tool test behind it.
- **`tailscale_whois` searches `PATH` only.** The Mac App Store build of
  Tailscale keeps its CLI inside the app bundle, so on that install every tailnet
  peer silently degrades to guest and the trusted-tier endpoints refuse the
  operator on their own machine. It degrades *closed*, so it is deferred rather
  than urgent.
- **On-device STT is Apple-silicon only.** The JSONL worker protocol is clean
  enough to swap engines, but the worker path is not yet configurable.
- **Identity is still derived from the network address.** The MCP side has
  session tokens; the web side does not, and `resolve_from_tailscale` accepts any
  login the tailnet resolves without comparing it to the hub owner's. The
  cross-site check closes the browser-driven half of that; the model itself is a
  v9 project.
- **`tests/test-restart-arch.py` fails**, as it does on pristine `main` — a stale
  path from the pre-v7 era, unrelated to this release.

---

## Verification

Every suite was re-run on the **integrated** result, not on the individual
branches:

```
python  attachment-gc 26 · csrf-origin 8 · upload-authz 6 · cull · doctor-drift
        file-reveal · reveal-realtool (2 loud skips) · search · session-maintenance
        working-indicator ×2 · monitor-culled-exit · context-projection
        nodes-upsert 11 · codex-rollouttail · monitor-keepalive-overflow · stt
node    client-render 50 · file-links 24 · unread 25 · decorator-seam 7
```

Four suites are new in this release: cross-site POST rejection, upload
authorization and quota, the real-tool reveal smoke test, and the decorator seam
created by merging two branches that both walk the same DOM.

The merge order matters and the obvious one is wrong: `int/stt` did not fork
from `int/localfs`, so merging it first produced conflicts including a 300-line
and a 500-line hunk. Merging **trivia → renderer → server → localfs → stt**
reduces the same work to one 12-line conflict.

Six of the seven conflicts were resolved by union, and only after proving the
merge base was empty — a script refused and left the markers in place otherwise,
because a union across a non-empty base silently keeps both versions of one
behaviour. The seventh had a non-empty base and was resolved by hand: two
branches had independently changed the same scheduler, and taking either side
alone would have dropped the other's fix.
