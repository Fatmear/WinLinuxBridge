#!/usr/bin/env bash
# WinLinuxBridge - self check (Linux side). Read-only, no sudo.
#
# Usage: ~/WinLinuxBridge/tools/verify.sh
# Exit code 0 = all checks passed. ASCII comments only.

set -uo pipefail

ROOT="${WL_ROOT:-$HOME/WinLinuxBridge}"
ENV_FILE="$ROOT/config/bridge.env"
pass=0
fail=0

ok()   { pass=$((pass + 1)); printf '[PASS] %s: %s\n' "$1" "$2"; }
bad()  { fail=$((fail + 1)); printf '[FAIL] %s: %s\n' "$1" "$2"; }
info() { printf '[INFO] %s\n' "$1"; }

check() { # check NAME COMMAND...
    local name="$1"; shift
    local out
    if out=$("$@" 2>&1); then ok "$name" "${out:-ok}"; else bad "$name" "$out"; fi
}

echo "--- layout ---"
for d in win linux tools docs config shared/from_win shared/to_win shared/shared; do
    if [ -d "$ROOT/$d" ]; then ok "dir $d" "present"; else bad "dir $d" "missing"; fi
done

for f in linux/wl linux/install-linux.sh win/winbridge_agent.py; do
    if [ -f "$ROOT/$f" ]; then ok "file $f" "$(wc -c < "$ROOT/$f") bytes"; else bad "file $f" "missing"; fi
done

echo "--- tools ---"
command -v python3 >/dev/null 2>&1 && ok "python3" "$(python3 --version 2>&1)" || bad "python3" "missing"
command -v curl >/dev/null 2>&1 && ok "curl" "$(curl --version 2>/dev/null | head -1)" || bad "curl" "missing"

echo "--- permissions ---"
if [ -f "$ENV_FILE" ]; then
    mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo '?')
    if [ "$mode" = "600" ]; then ok "bridge.env mode" "600"; else bad "bridge.env mode" "$mode (expected 600)"; fi
    if grep -q "^WL_AGENT_TOKEN='.\+'" "$ENV_FILE"; then
        ok "agent token" "present (not shown)"
    else
        bad "agent token" "empty - run install-win.ps1 on Windows"
    fi
else
    bad "bridge.env" "missing"
fi

echo "--- syntax ---"
if bash -n "$ROOT/linux/wl" 2>/dev/null; then ok "wl syntax" "ok"; else bad "wl syntax" "$(bash -n "$ROOT/linux/wl" 2>&1)"; fi
if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import ast,sys; ast.parse(open(sys.argv[1],encoding="utf-8").read())' "$ROOT/win/winbridge_agent.py" 2>/dev/null; then
        ok "agent syntax" "ok"
    else
        bad "agent syntax" "python parse failed"
    fi
fi

echo "--- tunnel ---"
port="${WL_AGENT_PORT:-18080}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    port="${WL_AGENT_PORT:-$port}"
fi
if command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q "127.0.0.1:${port} "; then
    ok "tunnel listen" "127.0.0.1:${port}"
    if "$ROOT/linux/wl" ping >/dev/null 2>&1; then
        ok "wl ping" "agent answered"
        if out=$("$ROOT/linux/wl" run --shell cmd 'echo WL-ROUNDTRIP' 2>&1) && echo "$out" | grep -q 'WL-ROUNDTRIP'; then
            ok "wl run round trip" "ok"
        else
            bad "wl run round trip" "$out"
        fi
    else
        bad "wl ping" "no answer through the tunnel"
    fi
else
    info "tunnel not listening on 127.0.0.1:${port} (Windows side down) - skipping live checks"
fi

echo
if [ "$fail" -gt 0 ]; then
    printf '%d passed, %d FAILED\n' "$pass" "$fail"
    exit 1
fi
printf '%d checks passed\n' "$pass"
exit 0
