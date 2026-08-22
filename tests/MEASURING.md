# Measuring the web UI

`tests/dom-harness.js` is a fake DOM. It has no layout engine, so **no test in this
repo can observe geometry** — overflow, occlusion, touch-target size, stacking.
That is not a gap in the tests, it is a gap in what the tests *can* see, and it
means responsive work has to be checked against a real browser.

This file is the list of ways that checking has produced confident wrong answers.
Every entry was hit for real during the 2026-08-22 mobile sprint, and every one
was caught by a result contradicting something already known — never by the
measurement announcing its own failure. Read it before trusting a number.

---

## The harness lies about the viewport, quietly

`dom-harness.js` stubs `matchMedia` as `{ matches: false }` with listeners that
never fire. So it reports **desktop** to every query and cannot be made to report
mobile. A test asserting mobile-conditional JS through `matchMedia` passes while
exercising the desktop branch, and looks green doing it.

That is worse than the missing layout engine, which fails loudly with zeros.
This one fails plausibly.

**Do instead:** push viewport-dependent logic into a pure function that takes the
limit as an *argument*, export it, and test it directly. `facePileModel(members,
agents, operator, limit)` exists in that shape for exactly this reason — the
regression passes `limit` rather than faking a viewport.

## Headless Chromium is not a desktop

By default it answers **`(hover:none)` true at every width**, and *both*
`(pointer:coarse)` and `(pointer:fine)` **false**. So a "desktop is
byte-identical" claim about any rule inside `@media (hover:none)` is, by default,
measured on a phone.

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

`Emulation.setTouchEmulationEnabled` **permanently clobbers hover and pointer
types for the rest of the browser session — including when called with
`enabled:false`.** A loop that does `browser.touch(width < 700)` and then measures
a `(hover:none)` rule at 1440px is measuring a phone and cannot tell.

**Use one browser per pointer mode.** Never reuse a browser that has touched
touch emulation for a desktop reading.

## Geometry APIs cannot see paint

Three different probes, three different lies, on one question — "does the title
paint over the toolbar buttons?":

| probe | what it actually reports | result |
|---|---|---|
| `el.getBoundingClientRect()` | the **clipped box** | title 14px *clear* of the controls — wrong |
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
someone else's work, **worktrees** stop you measuring it. Bisect an unexpected
red with `git stash push -- <the other person's path>` before believing it.

## `PY=` is an environment variable, and it applies to the JS tests too

```bash
PY=~/.claude/nth/venv/bin/python bash tests/run-all.sh
```

Passed positionally it is silently ignored and a large number of tests skip.
`test-served-page-boots.js` spawns a real server and needs the venv interpreter
like the Python tests do, so a bare `node tests/test-*.js` sweep shows a red that
is not real.

## Clean up your browsers

One browser per agent is fine; a 57-agent verification sweep left **16 orphaned
Chromium instances holding ~3GB** and **960 temp profile directories**, because
`close()` was only reached on the happy path. Remove the profile directory when
you reap the process, and use a context manager so an exception mid-measurement
still cleans up.
