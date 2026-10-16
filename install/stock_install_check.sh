#!/usr/bin/env bash
# The ordinary-user cycle on a stock hermes, end to end, in a throwaway $HOME — and the guarantee
# that installing HermesCloak never touches the runtime environment hermes shares between agents.
#
#   HERMES=/path/to/hermes  bash install/stock_install_check.sh  [SOURCE]
#
# SOURCE defaults to this checkout (file://…); pass `aviv27-dev/HermesCloak` to test the published
# default branch. Steps, each one checked (exit 1 on the first failure):
#   1. stock hermes in an empty $HOME  → record the shared runtime: environment ids + generations
#   2. hermes plugins install SOURCE --enable
#   3. shared runtime UNCHANGED: same environment ids, no new generation, no hermescloak inside
#   4. hermes hermescloak verify       → protected
#   5. hermes hermescloak test         → PROTECTED, and no scratch home left behind
#   6. hermes plugins remove hermescloak → nothing named hermescloak left under ~/.hermes
# Uses synthetic data only, opens no window, sends nothing off the machine (the test's "cloud" is a
# local fake). Prints elapsed time and the peak size of the throwaway home.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="${1:-file://$REPO}"
HERMES="${HERMES:-$(command -v hermes || true)}"
[ -x "$HERMES" ] || { echo "set HERMES=/path/to/hermes (the hermes launcher)"; exit 2; }
U="$(mktemp -d -t hermescloak-stock-XXXX)"
trap 'rm -rf "$U"' EXIT
T0=$(date +%s); PEAK=0
h() { HOME="$U" "$HERMES" "$@" 2>&1 | grep -v "install out of sync"; return "${PIPESTATUS[0]}"; }
size() { local s; s=$(du -sm "$U" 2>/dev/null | cut -f1); [ "${s:-0}" -gt "$PEAK" ] && PEAK=$s; }
fail() { echo "  FAIL: $*"; exit 1; }
envs() { ls -d "$U"/.hermes/installs/*/environments/*/ 2>/dev/null | sort; }
cloak_in_runtime() { local n=0 d; for d in "$U"/.hermes/installs/*/environments/*/venv/lib/python*/site-packages; do
  [ -d "$d" ] && n=$((n + $(ls "$d" | grep -ci hermescloak))); done; echo "$n"; }

echo "[1] stock hermes, empty home: $U"
h plugins list --plain >/dev/null || fail "hermes does not start"
h pm install >/dev/null || fail "hermes pm install (building the stock runtime)"   # an existing deployment already has one
BEFORE="$(envs)"; size
[ -n "$BEFORE" ] || fail "no stock runtime environment was built"
echo "    shared runtime: $(echo "$BEFORE" | grep -c . ) environment generation(s)"

echo "[2] hermes plugins install $SOURCE --enable"
h plugins install "$SOURCE" --enable | grep -E "Installed|enabled|Blocked|declares" || fail "install"
[ -f "$U/.hermes/plugins/hermescloak/plugin.yaml" ] || fail "plugin directory missing"; size

echo "[3] shared runtime untouched"
AFTER="$(envs)"
[ "$BEFORE" = "$AFTER" ] || fail "environment generations changed: before=[$BEFORE] after=[$AFTER]"
[ "$(cloak_in_runtime)" = 0 ] || fail "hermescloak found inside the shared runtime"
echo "    same environment id(s), no new generation, no hermescloak inside ✓"

echo "[4] hermes hermescloak verify"
h hermescloak verify | grep -E "\[(MISSING|WARN)\]|protected" ; [ "${PIPESTATUS[0]}" = 0 ] || fail "verify"

echo "[5] hermes hermescloak test"
h hermescloak test | tail -1 ; [ "${PIPESTATUS[0]}" = 0 ] || fail "test"; size
[ -z "$(ls -A "$U/.hermes/profiles" 2>/dev/null)" ] || fail "scratch profile left behind"
[ "$BEFORE" = "$(envs)" ] || fail "the test changed the shared runtime"

echo "[6] hermes plugins remove hermescloak"
h plugins remove hermescloak >/dev/null || fail "remove"
LEFT="$(find "$U/.hermes" -iname '*hermescloak*' 2>/dev/null)"
[ -z "$LEFT" ] || fail "left behind: $LEFT"
echo "    nothing named hermescloak left under ~/.hermes ✓"

echo "STOCK INSTALL CHECK PASSED ✓  ($(( $(date +%s) - T0 ))s, peak throwaway home ${PEAK} MB incl. the stock runtime)"
