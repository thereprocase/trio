#!/usr/bin/env bash
# Run the fast regression tests in this directory.
#
# These are plain stdlib scripts, not a pytest suite (hyphenated filenames
# are deliberately not importable as modules, and this project adds no test
# dependencies). Each exits non-zero on failure.
#
# Some tests import nth_server, which needs the `mcp` SDK. Point PY at the
# nth venv to include those:
#     PY=~/.claude/nth/venv/bin/python bash tests/run-all.sh
# Without it, tests requiring mcp are reported as skipped, not failed.
#
# SOAK holds the v5-era manual scripts that sleep for minutes to hours by
# design (timeout-ceiling and restart-durability probes). They are not unit
# tests and are excluded here — run them by hand when you care:
#     python3 tests/test-timeout-ceiling.py --duration 600

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

PY="${PY:-python3}"
TIMEOUT="${TIMEOUT:-60}"

SOAK="test-agent-restart-loop.py test-heartbeat-theory.py
      test-timeout-battery.py test-timeout-ceiling.py
      test-timeout-unfakeable.py test-restart-arch.py"

is_soak() {
    for s in $SOAK; do [ "$s" = "$1" ] && return 0; done
    return 1
}

pass=0; fail=0; skip=0; failed_names=""

for f in test-*.py; do
    [ -e "$f" ] || continue
    if is_soak "$f"; then
        printf '  \033[90mSOAK\033[0m  %s (long-running; run by hand)\n' "$f"
        skip=$((skip+1)); continue
    fi
    out="$(timeout "$TIMEOUT" "$PY" "$f" 2>&1)"; rc=$?
    if [ $rc -eq 0 ]; then
        printf '  \033[32mPASS\033[0m  %s\n' "$f"; pass=$((pass+1))
    elif printf '%s' "$out" | grep -q "No module named 'mcp'"; then
        printf '  \033[90mSKIP\033[0m  %s (needs the mcp SDK — set PY to the nth venv)\n' "$f"
        skip=$((skip+1))
    elif [ $rc -eq 124 ]; then
        printf '  \033[31mFAIL\033[0m  %s (timed out after %ss)\n' "$f" "$TIMEOUT"
        fail=$((fail+1)); failed_names="$failed_names $f"
    else
        printf '  \033[31mFAIL\033[0m  %s\n' "$f"
        printf '%s\n' "$out" | tail -20 | sed 's/^/        /'
        fail=$((fail+1)); failed_names="$failed_names $f"
    fi
done

echo ""
echo "  $pass passed, $fail failed, $skip skipped"
[ -n "$failed_names" ] && echo "  failed:$failed_names"
exit $((fail > 0 ? 1 : 0))
