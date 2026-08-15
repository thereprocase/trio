# nth v8.1.1-beta.1

**Released:** 2026-08-15 · **Previous:** v8.1.0-beta.1 (2026-08-14)
**Contents:** 4 of 5 known gaps closed, 8 commits, +891 / −25 across 7 files, 4 new test files

v8.1.0 shipped with a "Known gaps" section that listed five things it did not
fix. This release closes four of them, in the order they were listed, one
evening later.

The fifth is still open and stays open honestly — see **Still open** below.

---

## ⚠ Read this before upgrading

**On a hub whose Tailscale node is tagged, tailnet peers now become guests —
including you.**

A node brought up with an auth key has no user account, so the hub cannot
derive who its owner is. Previously that state accepted *every* tailnet account
as operator. It now refuses, because "nobody can tell who the owner is" is
exactly when handing out reveal, cull and upload is least defensible.

Check whether it affects you:

```bash
tailscale status --json | jq '.User[(.Self.UserID|tostring)].LoginName'
```

A login means nothing changes for you. Empty or `null` means set
`NTH_TAILNET_OWNER=<your-login>` before restarting the hub — or
`NTH_TAILNET_PERMISSIVE=1` to keep the old behaviour, which is not recommended
on a tailnet you share. The server prints this on startup and names the fix; it
does not fail silently.

---

## What closed

### Only the hub's owner gets the tailnet tier
`resolve_from_tailscale` accepted any login the tailnet resolved. On a shared
tailnet — or from a device handed to someone else — that granted a stranger
exactly what a local shell gets: reveal a path on the operator's disk, remove
members, upload into their home directory.

The comparison is by **account, not device**, so every one of the owner's own
machines still resolves. That is the regression this change could plausibly
have caused, so there is a test pinning it.

### A transient failure no longer pins a browser to guest
The identity ladder returned early on any cached verdict, so a single bad
moment — tailscaled restarting, the 3-second whois timeout firing under load —
persisted until the browser's cookie changed. Untrusted verdicts are now
re-checked at a bounded cadence; trusted ones stay cached.

Two defects were found in that fix *during review*, by people who did not write
it, and both are fixed here rather than shipped:

- **A retry that downgraded.** A guest exists precisely *because* whois could
  not name them, so the retry failed for every guest by definition and parked
  them back as `pending` — silently un-naming every guest once per window,
  forever, and demanding re-identification mid-session. A retry may upgrade a
  tier; it must never downgrade one.
- **A permissive grant that outlived its window.** A `tailscale` identity is
  never re-checked once cached, so a peer trusted while the owner was
  underivable kept operator rights for the cookie's **30-day** life, even after
  owner resolution began working and said they were not the owner. Permissive
  grants are now provisional — returned but not cached — so enforcement begins
  the moment the owner becomes derivable.

### The Tailscale CLI is found where it actually lives
The lookup searched `PATH` only. The Mac App Store build keeps its CLI inside
the app bundle, so on that install every tailnet peer silently degraded to
guest and the trusted-tier endpoints refused the operator on their own machine.
Adds the known absolute install locations, and a one-time warning when every
candidate misses — the warning matters as much as the paths, because this
failure degrades *closed* and is therefore invisible.

### Reveal selects the file on Linux
Via the freedesktop `FileManager1.ShowItems` D-Bus call, matching the macOS and
Windows behaviour instead of merely opening the containing folder. This was
deliberately held out of v8.1.0 for want of a real-tool test; it now has one.

### The suite is fully green
`tests/test-restart-arch.py` had been failing since before v8.0.2 on a stale
`~/.claude/roam/` path from the pre-v7 era. Fixed. There is no longer a
"known-failing" test to explain to newcomers.

---

## Still open

**A non-Apple STT engine.** Deferred to 8.1.2 with a real defect open against
it: the candidate engine was being handed audio it cannot decode, and `ffmpeg`
was not pinned as a requirement. Rather than ship that, the *test* ships now —
`tests/test-stt-audio-format.py` skips with a named reason ("no
whisper.cpp-backed worker in this tree") and starts failing the moment an engine
lands without fixing the defect. An armed tripwire is worth more than an engine
that reports itself healthy and then fails on first use.

**The identity model itself.** The web side still has no session tokens; the
MCP side has had them since v6.2. This release narrows *who* is trusted. It does
not change *how* trust is established, which remains derived from the network
address. That is a v9 project, and pretending otherwise in a patch release would
be the kind of overstatement this project's release notes exist to avoid.

---

## Verification

All 26 suites green on the **assembled** result — 22 Python, 4 Node — including
the four new files. Merge order was measured across three orderings before
anything was merged: unlike v8.1.0, every ordering produced **zero** conflicts,
so the order lesson from last release did not apply this time. Measuring it
still cost nothing.

Four new test files, and the design of two of them is the point:

| Test | Why it exists |
|---|---|
| `test-identity-owner.py` | 12 assertions. Includes "the owner's second device still resolves" — the regression the owner check could cause — and "permissive does not excuse a known-owner mismatch", so the two branches cannot later be collapsed into "no authz at all". |
| `test-identity-retry.py` | The transient-failure regression, plus guest survival across a failed retry. |
| `test-reveal-realtool.py` | Invokes the real tool and **skips loudly**, naming the gap on stderr. |
| `test-stt-audio-format.py` | Skips loudly today; fails the moment an engine lands with the decode defect unfixed. |

One test in this release initially **passed with the code it was testing
removed** — worthless, and caught only because it was run in both directions.
The cause: the retry-eligibility helper *reserves* the retry as a side effect,
so asserting eligibility consumed the window and the test never reached the path
it claimed to check. Every new assertion here has since been watched to fail.
