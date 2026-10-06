"""No decorator may drift onto the wrong function.

An orphaned `@staticmethod` silently rebinds itself to whatever `def` comes
next. That is not a syntax error and not an import error — the module loads
fine and the wrong method quietly loses its `self`, so it fails only when that
one route is exercised, with a confusing "missing 1 required positional
argument".

This exact shape shipped once here: a block of methods was inserted between a
`@staticmethod` and its function, so the decorator landed on an unrelated HTTP
handler. Every test passed except the single route that called it.

The reliable signature is a `@staticmethod`/`@classmethod` whose function still
takes `self` — a method cannot be both. Checked via AST, so the embedded CSS
(`@media`, `@keyframes`) and the MCP tool decorators are not mistaken for it.

Usage: python tests/test-no-orphan-decorators.py
"""
import ast
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + f": {name}")
    if not cond:
        failures.append(name)


def drifted_decorators(path: Path):
    """(line, reason) for every function whose decorator cannot be its own."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError as e:
        return [(e.lineno or 0, f"syntax error: {e.msg}")]
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {d.id for d in node.decorator_list if isinstance(d, ast.Name)}
        args = [a.arg for a in node.args.args]
        first = args[0] if args else None
        if names & {"staticmethod"} and first == "self":
            bad.append((node.lineno,
                        f"{node.name}() is @staticmethod but takes self"))
        if names & {"classmethod"} and first == "self":
            bad.append((node.lineno,
                        f"{node.name}() is @classmethod but takes self"))
    return bad


scanned = 0
for path in sorted(SERVER.glob("*.py")):
    scanned += 1
    problems = drifted_decorators(path)
    check(f"{path.name}: no decorator has drifted onto another function",
          not problems)
    for line, why in problems:
        print(f"    {path.name}:{line}  {why}")

check("actually scanned the server modules", scanned > 5)

print()
if failures:
    print(f"FAILED — {len(failures)} failure(s)")
    sys.exit(1)
print("OK — 0 failure(s)")
