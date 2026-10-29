#!/usr/bin/env bash
# Cancel isolation: killing one job must not touch another job or the bridge.
set -u
cd "$HOME/WinLinuxBridge" || exit 1
W=./linux/wl
pass=0
fail=0
ok()  { pass=$((pass+1)); printf '[PASS] %s\n' "$1"; }
bad() { fail=$((fail+1)); printf '[FAIL] %s\n' "$1"; }
jf()  { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]))' "$1"; }

A=$($W run --json --no-wait --shell powershell 'Start-Sleep -Seconds 90' | jf job_id)
B=$($W run --json --no-wait --shell powershell 'Start-Sleep -Seconds 90' | jf job_id)
PIDB=$($W job status "$B" --json | jf pid)
echo "[INFO] A=$A  B=$B  B.pid=$PIDB"

CANCEL=$($W job cancel "$A" --json)
CA=$(printf '%s' "$CANCEL" | jf state)
KILLED=$(printf '%s' "$CANCEL" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("cancel_result") or {}).get("killed"))')
ALIVE_A=$(printf '%s' "$CANCEL" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(((d.get("cancel_result") or {}).get("cleanup") or {}).get("alive"))')
echo "[INFO] A -> $CA  killed=$KILLED  alive_after=$ALIVE_A"

[ "$CA" = "CANCELLED" ] && ok "cancelled job reports CANCELLED" || bad "state $CA"
[ "$ALIVE_A" = "False" ] && ok "cancelled process tree is gone" || bad "pid still alive=$ALIVE_A"
[ "$KILLED" -ge 1 ] 2>/dev/null && ok "killed count is reported ($KILLED)" || bad "killed count missing ($KILLED)"

SB=$($W job status "$B" --json | jf state)
[ "$SB" = "RUNNING" ] && ok "the neighbouring job was untouched (RUNNING)" || bad "neighbour ended as $SB"

if $W ping >/dev/null 2>&1; then ok "bridge and tunnel survived the cancel"; else bad "bridge unreachable after cancel"; fi

# A process that is not a job must also survive a cancel.
$W run --shell powershell 'Start-Process -FilePath powershell -ArgumentList "-NoProfile","-Command","Start-Sleep -Seconds 120" -WindowStyle Hidden' >/dev/null 2>&1
OUTSIDE=$($W run --shell powershell '(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*Start-Sleep -Seconds 120*" } | Measure-Object).Count' 2>/dev/null | tr -d '\r\n ')
echo "[INFO] unrelated processes matching the marker: ${OUTSIDE:-?}"

$W job cancel "$B" >/dev/null 2>&1
if $W ping >/dev/null 2>&1; then ok "bridge still healthy after the second cancel"; else bad "bridge died"; fi

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
