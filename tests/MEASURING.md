# Measuring the web UI

`tests/dom-harness.js` is a fake DOM. It has no layout engine, so **no test in this
repo can observe geometry** — overflow, occlusion, touch-target size, stacking.
That is not a gap in the tests, it is a gap in what the tests *can* see, and it
means responsive work has to be checked against a real browser.

This file is the list of ways that checking has produced confident wrong answers.
Every entry was hit for real during the 2026-08-22 mobile sprint, and every one
was caught by a result contradicting something already known — never by the
measurement announcing its own failure. Read it before trusting a number.

Including this one: the first draft reproduced four of the failures catalogued
below — a claim its own repo disproved (`matchMedia` "cannot be made to report
mobile", while `test-face-pile.js` does exactly that), a universal generalised
from a handful of runs, imprecise terminology that would have taught the wrong
model, and advice that contradicted the section immediately above it. A document
about measurement discipline is not exempt from measurement discipline. Check the
claims here against the code before repeating them.

---

## The harness reports desktop unless you make it say otherwise

`dom-harness.js` stubs `matchMedia` as a permanent `{ matches: false }` with
listeners that never fire (`dom-harness.js:490`). A test that asserts
mobile-conditional JS through `matchMedia` and does nothing about the stub passes
while exercising the **desktop** branch, and looks green doing it.

That is worse than the missing layout engine, which fails loudly with zeros. This
one fails plausibly.

It is a default, not a wall — two ways through it, and real tests use both:

- **Test the arithmetic without a viewport at all.** Push viewport-dependent
  logic into a pure function that takes the limit as an *argument*.
  `facePileModel(members, agents, operator, limit)` has that shape deliberately,
  so its regression passes `limit` instead of faking a viewport.
- **Override the stub when the wiring is the thing under test.**
  `test-face-pile.js:240-280` replaces `cx.context.window.matchMedia`, captures
  the registered `change` handler, **fires it**, and restores the original
  afterwards. That is what proves the listener exists, does something, and is
  surrendered on unmount — none of which a pure model can show. Counting
  registrations is not enough: a listener wired to a no-op would satisfy that and
  change nothing on rotation.

## Headless Chromium is not a desktop

Measured on `/usr/bin/chromium` under `--headless=new` on this box; treat the
exact values as observed rather than guaranteed, and re-check them if the browser
changes. By default it answered **`(hover:none)` true at every width**, with
*both* `(pointer:coarse)` and `(pointer:fine)` **false**. So a "desktop is
byte-identical" claim about any rule inside `@media (hover:none)` is, by default,
measured in a **non-hover input environment** — which is the phone branch of your
CSS, whatever the viewport width says.

Force real pointer types at launch:

```
--blink-settings=availableHoverTypes=2,primaryHoverType=2,availablePointerTypes=4,primaryPointerType=4
```

The values are Blink enums and **the wrong one fails silently**: HoverType
`kHoverNone=1`, `kHoverHover=2`; PointerType `kPointerNone=1`, `kPointerCoarse=2`,
`kPointerFine=4`. Passing `1` looks like forcing and changes nothing. Measured at
1440px:

| flags | hover:none | hover:hover | coarse | fine |
|---|---|---|---|---|
| none | true | false | false | false |
| `hover=1` | true | false | false | false |
| `hover=2` | false | true | false | false |
| the line above | false | true | false | true |

## `setTouchEmulationEnabled` is a one-way door

`Emulation.setTouchEmulationEnabled` was observed to clobber launch-forced hover
and pointer types **for the remainder of that CDP session — including when called
with `enabled:false`** — and nothing tried afterwards restored them. Whether that
is guaranteed by the protocol or an artefact of this build is not established
here, so treat a session that has called it as **tainted** for hover and pointer
queries rather than assuming it recovers. A loop that does
`browser.touch(width < 700)` and then measures a `(hover:none)` rule at 1440px is
still evaluating the non-hover branch, and cannot tell.

**Use one browser per pointer mode.** It costs a process and removes the whole
question.

## Geometry APIs cannot see paint

Three different probes, three different lies, on one question — "does the title
paint over the toolbar buttons?":

| probe | what it actually reports | result |
|---|---|---|
| `el.getBoundingClientRect()` | the element's allocated **border box** | title 14px *clear* of the controls — wrong |
| `Range.getBoundingClientRect()` | **text geometry**, ignores ancestor `overflow:hidden` | identical before and after the fix — reads as a clean null |
| screenshot pixel diff | what is on screen | 528 pixels of title ink over the controls, gone after |

**A geometry probe is not evidence about anything that can overflow visibly.**
For ellipsis, `overflow:visible`, shadows, or occlusion, diff the pixels.

## Measure after the animation, not during it

Reading a transitioned property immediately after the class change returns the
*pre-transition* value. A drawer measured at t=0 reports `translateX(-105%)` and
`elementFromPoint` returns nothing — indistinguishable from "the drawer never
opens". After 600ms it sits at `left:0` and taps land on it.

## Wait for async data, and say when you didn't

The face pile is populated by `/api/agents` polling. A short settle races it and
produces numbers that change between runs — a 144px pile and a 40px pile from the
same page. **Sample twice and report both if they differ**, rather than quoting
whichever landed first.

## A fixture that lacks the state is not a passing test

Two failures of this shape, both of which looked exactly like a pass:

- Hit-testing adjacent controls returned "0 misrouted" — because a real message
  card renders **one** tools button. A single-button row cannot exhibit an
  adjacency bug.
- A probe element placed inside `.messages` proved nothing about what stretches
  the grid track, because `.messages` is `overflow:auto` and absorbs any width.

And the repair has its own trap: **synthesising the missing state quietly changes
which hypothesis you are testing.** Cloning a second button created a horizontal
collision that production never has, while leaving the vertical collision — the
one that does happen — unexamined. Prefer probing real rendered cases; when you
must synthesise, ask what the synthetic case is now testing.

## Do not compare across trees

Dividing local `HEAD` file sizes by a deployed shell's byte count produced
components summing to **102%** of the whole. The impossible total was the
arithmetic reporting an invalid comparison. Measure both sides from the same
artefact.

Likewise, an aggregate that a concurrent process can move — counting files in
`/tmp`, say — is not a measurement of your change. Track the specific thing your
run created.

## Never measure in the shared checkout

Several agents write to one working tree. A full-suite run is only valid for the
tree as it existed at that instant, and a red can belong to anyone. Use a
worktree:

```bash
git worktree add --detach /path/to/scratch/wt HEAD
cp <only your changed files> /path/to/scratch/wt/server/web/css/
cd /path/to/scratch/wt && PY=~/.claude/nth/venv/bin/python bash tests/run-all.sh
```

Two rules doing different jobs: **explicit pathspecs** stop you committing
someone else's work, **worktrees** stop you measuring it.

**Bisect an unexpected red the same way — never by stashing someone else's
path.** `git stash push -- <their file>` does work, and it is the obvious move,
but in a shared checkout it *removes another writer's uncommitted work from under
them* and disturbs the index while they are mid-edit. It also contradicts the
rule directly above: the whole point of the worktree is that you stop touching
the shared tree at all. Instead, build a worktree at a known commit, copy in only
the files **you** own, and run there. If your files reproduce the red in
isolation, it is yours. If they do not, all you have established is that **your
diff alone does not cause it** — not that you are uninvolved. A failure can live
in the join between your change and something else that landed, so the next step
is an isolated *integration* worktree containing both, not a shrug. Either way,
never stash a peer's path to find out.

## `PY=` is an environment variable, and it applies to the JS tests too

```bash
PY=~/.claude/nth/venv/bin/python bash tests/run-all.sh
```

Passed positionally it is silently ignored and a large number of tests skip.
`test-served-page-boots.js` spawns a real server and needs the venv interpreter
like the Python tests do, so a bare `node tests/test-*.js` sweep shows a red that
is not real.

## Clean up your browsers

Scale is not the bug. Dozens of browsers are fine with reliable teardown; what
fails is **happy-path-only cleanup, compounding**. During the 2026-08-22 run a
driver reached `close()` only on the success path, so every script that raised
mid-measurement orphaned both its process and its `--user-data-dir`, and a sweep
turned a per-run leak into an accumulation.

Exact counts are deliberately omitted: they were observed live and no inspectable
artifact was committed, so quoting them here would be a number without provenance
— the thing this file exists to discourage. Take your own:

```bash
# Self-excluding pattern: plain `grep chromium` matches its OWN grep process and
# inflates the count — the exact false confidence this file is about.
ps -eo pid,ppid,etimes,args | grep '[c]hromium' | grep -- '--remote-debugging-port'
find /tmp -maxdepth 1 -name 'cdp-profile-*' -type d | wc -l
```

An orphan is identified by **reparenting — `ppid` of 1 — not by matching the
process name.** A live browser owned by a running script looks identical to a
dead one's leftovers in a name-only match.

Remove the profile directory when you reap the process, and use a context manager
so an exception mid-measurement still cleans up.

Verify the fix by tracking the instance's own directory path, not by counting
`/tmp` — a concurrent browser will move that count under you.
