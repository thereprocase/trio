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

# timeout(1) is GNU coreutils and is NOT on a stock macOS, where this suite is
# developed. Without this shim every test "failed" with `timeout: command not
# found` — a fully red suite that says nothing at all about the code, which is
# worse than no suite, because it trains you to ignore it. Homebrew's coreutils
# installs the same binary as `gtimeout`. With neither, run the test directly
# and lose only the per-test time limit.
if command -v timeout >/dev/null 2>&1; then
    run_test() { timeout "$TIMEOUT" "$@"; }
elif command -v gtimeout >/dev/null 2>&1; then
    run_test() { gtimeout "$TIMEOUT" "$@"; }
else
    run_test() { "$@"; }
    printf '  \033[90mnote\033[0m  no timeout(1) on PATH — running without a per-test limit\n'
fi

SOAK="test-agent-restart-loop.py test-heartbeat-theory.py
      test-timeout-battery.py test-timeout-ceiling.py
      test-timeout-unfakeable.py test-restart-arch.py"

is_soak() {
    for s in $SOAK; do [ "$s" = "$1" ] && return 0; done
    return 1
}

# Run the files concurrently. The suite is wait-bound, not CPU-bound: the
# slowest files spend 15-25 s each in deliberate poll/timeout sleeps, and the
# whole set adds up to about six minutes serially but under a minute at
# JOBS=8. Every test already isolates its own HOME, runtime and temp state,
# so they do not interfere. Output keeps a stable order: each worker writes
# its result to a temp file and the report prints them after all finish, so
# a failure is as easy to find as before.
#     JOBS=1 bash tests/run-all.sh      # serial, if a test ever needs it
JOBS="${JOBS:-8}"
results="$(mktemp -d)"
trap 'rm -rf "$results"' EXIT

run_one() {  # run_one FILE INTERPRETER -> $results/FILE.status and .out
    local f="$1" interp="$2" out rc
    out="$(run_test "$interp" "$f" 2>&1)"; rc=$?
    printf '%s\n' "$out" > "$results/$f.out"
    if [ $rc -eq 0 ]; then echo pass
    elif printf '%s' "$out" | grep -q "No module named 'mcp'"; then echo skip-mcp
    elif [ $rc -eq 124 ]; then echo timeout
    else echo fail
    fi > "$results/$f.status"
}

pass=0; fail=0; skip=0; failed_names=""
running=0
for f in test-*.py; do
    [ -e "$f" ] || continue
    if is_soak "$f"; then
        printf '  \033[90mSOAK\033[0m  %s (long-running; run by hand)\n' "$f"
        skip=$((skip+1)); continue
    fi
    run_one "$f" "$PY" &
    running=$((running+1))
    if [ "$running" -ge "$JOBS" ]; then wait -n; running=$((running-1)); fi
done

# The Node DOM tests. These exercise the browser client against a fake DOM and
# were previously run only by hand, so nothing noticed when a change to the
# client's location broke all four of them at once. A test nobody runs is not
# a test. Node is stdlib-only here (no npm), so this adds no dependency.
if command -v node >/dev/null 2>&1; then
    for f in test-*.js; do
        [ -e "$f" ] || continue
        run_one "$f" node &
        running=$((running+1))
        if [ "$running" -ge "$JOBS" ]; then wait -n; running=$((running-1)); fi
    done
else
    for f in test-*.js; do
        [ -e "$f" ] || continue
        printf '  \033[90mSKIP\033[0m  %s (needs node)\n' "$f"; skip=$((skip+1))
    done
fi
wait

for f in test-*.py test-*.js; do
    [ -e "$results/$f.status" ] || continue
    case "$(cat "$results/$f.status")" in
        pass) printf '  \033[32mPASS\033[0m  %s\n' "$f"; pass=$((pass+1));;
        skip-mcp) printf '  \033[90mSKIP\033[0m  %s (needs the mcp SDK; set PY to the nth venv)\n' "$f"; skip=$((skip+1));;
        timeout) printf '  \033[31mFAIL\033[0m  %s (timed out after %ss)\n' "$f" "$TIMEOUT"; fail=$((fail+1)); failed_names="$failed_names $f";;
        *) printf '  \033[31mFAIL\033[0m  %s\n' "$f"; tail -20 "$results/$f.out" | sed 's/^/        /'; fail=$((fail+1)); failed_names="$failed_names $f";;
    esac
done

echo ""
echo "  $pass passed, $fail failed, $skip skipped"
[ -n "$failed_names" ] && echo "  failed:$failed_names"
exit $((fail > 0 ? 1 : 0))
