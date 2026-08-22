"""The Claude model catalogue must have exactly one definition.

nth_agent_manager used to carry its own copy of CLAUDE_MODELS. Because the
dispatcher is what /api/agent-models actually calls, ITS copy was the one the
picker rendered — so editing nth_supervisor's list changed nothing a user
could see, and the two drifted apart silently. That is precisely the failure
this repo keeps re-learning: two definitions of one fact, with no test saying
which wins.

Also pins the two things about the catalogue that are user-visible decisions
rather than incidental: the ORDER (it is the picker's order) and the fact that
`efforts` differ per model (Haiku has no max, so a fixed low/medium/high
ladder would be a lie).

Usage: python tests/test-model-catalogue.py
"""
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))
import nth_supervisor as nsup          # noqa: E402
import nth_agent_manager as nam        # noqa: E402

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


check("the dispatcher re-exports the supervisor's catalogue rather than "
      "defining its own — the dispatcher's copy is what the picker shows",
      nam.CLAUDE_MODELS is nsup.CLAUDE_MODELS)

ids = [m["id"] for m in nsup.CLAUDE_MODELS]
check(f"the order is the picker's order, most capable first (got {ids})",
      ids == ["fable", "opus", "sonnet", "haiku"])

check("exactly one model is marked default",
      sum(1 for m in nsup.CLAUDE_MODELS if m.get("default")) == 1)

# Not decoration: the picker reads this per model, and offering a level the
# model does not have means the operator's choice is silently coerced.
efforts = {m["id"]: m["efforts"] for m in nsup.CLAUDE_MODELS}
check(f"efforts differ per model — Haiku has no 'max' (got {efforts['haiku']})",
      "max" not in efforts["haiku"] and "max" in efforts["opus"])
check("every model offers at least low/medium/high",
      all({"low", "medium", "high"} <= set(v) for v in efforts.values()))

# A second literal list anywhere in server/ is the drift this test exists for.
copies = []
for path in sorted(SERVER.glob("*.py")):
    text = path.read_text(encoding="utf-8")
    if 'CLAUDE_MODELS = [' in text:
        copies.append(path.name)
check(f"only one file DEFINES the catalogue (found: {copies or 'none'})",
      copies == ["nth_supervisor.py"])

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("OK — one catalogue, ordered, with per-model efforts")
