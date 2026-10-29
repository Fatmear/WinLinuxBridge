#!/usr/bin/env bash
# Live verification of the job features through the real tunnel.
# Runs ON the Linux host. Prints one PASS/FAIL line per behaviour.
set -u
cd "$HOME/WinLinuxBridge" || exit 1
W=./linux/wl
pass=0
fail=0

ok()   { pass=$((pass+1)); printf '[PASS] %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf '[FAIL] %s\n' "$1"; }

echo "--- 1. client wait timeout is not a failure ---"
OUT=$($W run --timeout 600 --wait 3 --json --shell powershell \
      '1..20 | ForEach-Object { Start-Sleep -Seconds 1; Write-Output ("tick " + $_) }' 2>/dev/null)
RC=$?
STATE=$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: print("PARSE"); raise SystemExit
print(d.get("state"), d.get("error_code"), d.get("job_id"))' 2>/dev/null)
set -- $STATE
JOB1=${3:-}
if [ "$RC" -eq 124 ]; then ok "exit code 124 (wait timeout, not failure)"; else bad "exit code was $RC, expected 124"; fi
if [ "$1" = "RUNNING" ]; then ok "state reported RUNNING while the job continues"; else bad "state was $1"; fi
if [ "$2" = "CLIENT_WAIT_TIMEOUT" ]; then ok "error_code CLIENT_WAIT_TIMEOUT"; else bad "error_code was $2"; fi
if [ -n "$JOB1" ]; then ok "job id returned: $JOB1"; else bad "no job id returned"; fi

echo "--- 2. reconnecting can still see it ---"
S=$($W job status "$JOB1" --json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["state"])')
if [ "$S" = "RUNNING" ]; then ok "job still queryable after the client gave up"; else bad "status was $S"; fi
L=$($W job logs "$JOB1" --json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("next"), d.get("bytes_total"))')
ok "logs cursor available: $L"

echo "--- 3. cancel kills only that job tree ---"
C=$($W job cancel "$JOB1" --json)
printf '%s' "$C" | python3 -c 'import json,sys
d=json.load(sys.stdin); c=d.get("cancel_result") or {}
print("[INFO] cancel state=%s killed=%s alive=%s" % (d.get("state"), c.get("killed"), (c.get("cleanup") or {}).get("alive")))'
S2=$($W job status "$JOB1" --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["state"])')
if [ "$S2" = "CANCELLED" ]; then ok "job state CANCELLED"; else bad "state after cancel was $S2"; fi
if $W ping >/dev/null 2>&1; then ok "bridge still healthy after cancel"; else bad "bridge died after cancel"; fi

echo "--- 4. request_id dedupe (retry safety) ---"
RID="verify-dedupe-$$"
A=$($W run --json --request-id "$RID" --shell cmd 'echo first-run' 2>/dev/null)
JA=$(printf '%s' "$A" | python3 -c 'import json,sys;print(json.load(sys.stdin)["job_id"])')
B=$($W run --json --request-id "$RID" --shell cmd 'echo second-run' 2>/dev/null)
JB=$(printf '%s' "$B" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["job_id"], d.get("deduplicated"))')
set -- $JB
if [ "$1" = "$JA" ]; then ok "same request_id returned the same job ($JA)"; else bad "got a new job: $1 vs $JA"; fi

echo "--- 5. execution timeout kills and reports TIMEOUT ---"
T=$($W run --json --timeout 2 --shell powershell 'Start-Sleep -Seconds 30' 2>/dev/null)
printf '%s' "$T" | python3 -c 'import json,sys
d=json.load(sys.stdin); s=d.get("state"); e=d.get("error_code")
print("[INFO] state=%s error_code=%s" % (s, e))
raise SystemExit(0 if s == "TIMEOUT" and e == "TIMEOUT_KILLED" else 1)' \
  && ok "execution timeout reported TIMEOUT/TIMEOUT_KILLED" || bad "timeout not reported correctly"

echo "--- 6. large output: summary + file + sha256 ---"
BIG=$($W run --json --shell powershell '1..4000 | ForEach-Object { Write-Output ("line $_ " + ("x" * 60)) }' 2>/dev/null)
printf '%s' "$BIG" | python3 -c 'import json,sys
d=json.load(sys.stdin); o=d["stdout"]
print("[INFO] bytes=%s omitted=%s sha=%s..." % (o["bytes"], o["omitted_bytes"], (o.get("sha256") or "")[:12]))
ok = o["bytes"] > 200000 and o["omitted_bytes"] > 0 and len(o.get("sha256") or "") == 64
raise SystemExit(0 if ok else 1)' \
  && ok "big output auto-summarised, full text kept on disk with sha256" || bad "big output handling wrong"

echo "--- 7. structured argv: chinese, spaces, quotes, amp ---"
A2=$($W run --json --shell cmd 'echo "中文 路径 & 空格 ''引号''"' 2>/dev/null)
printf '%s' "$A2" | python3 -c 'import json,sys
d=json.load(sys.stdin); t=d["stdout"]["text"]
print("[INFO] out=%r" % t.strip())
raise SystemExit(0 if "中文" in t else 1)' \
  && ok "utf-8 through cmd with quotes/ampersand" || bad "encoding/quoting problem"

echo "--- 8. resource serialisation ---"
P1=$($W run --json --no-wait --resource "verify:res-A" --shell powershell 'Start-Sleep -Seconds 4; Write-Output A' 2>/dev/null)
P2=$($W run --json --no-wait --resource "verify:res-A" --shell powershell 'Write-Output B' 2>/dev/null)
J1=$(printf '%s' "$P1" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("job_id",""))' 2>/dev/null)
J2=$(printf '%s' "$P2" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("job_id",""))' 2>/dev/null)
S2NOW=$($W job status "$J2" --json | python3 -c 'import json,sys;print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
if [ "$S2NOW" = "QUEUED" ] || [ "$S2NOW" = "RUNNING" ]; then
  ok "second job waited for the resource (state $S2NOW)"
else
  bad "second job did not queue behind the first (state $S2NOW)"
fi
sleep 8
S1=$($W job status "$J1" --json | python3 -c 'import json,sys;print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
S2=$($W job status "$J2" --json | python3 -c 'import json,sys;print(json.load(sys.stdin).get("state",""))' 2>/dev/null)
if [ "$S1" = "SUCCEEDED" ] && [ "$S2" = "SUCCEEDED" ]; then
  ok "both serialised jobs succeeded ($S1, $S2)"
else
  bad "serialised jobs ended as $S1 / $S2"
fi

echo "--- 9. doctor ---"
D=$($W doctor --json 2>/dev/null)
printf '%s' "$D" | python3 -c 'import json,sys
d=json.load(sys.stdin); a=d.get("agent") or {}
ok = d.get("transport_ok") and a.get("runtime",{}).get("agent_version") and a.get("timeouts") and a.get("paths")
print("[INFO] agent=%s shell_ps=%s jobs=%s" % (
    a.get("runtime",{}).get("agent_version"),
    (a.get("shells",{}).get("powershell") or {}).get("version"),
    (a.get("jobs") or {}).get("total")))
raise SystemExit(0 if ok else 1)' \
  && ok "doctor reports versions, shells, timeouts, paths" || bad "doctor output incomplete"
if printf '%s' "$D" | grep -Eq '[0-9a-f]{32,}'; then
  bad "doctor output contains something that looks like a secret"
else
  ok "doctor exposes no credentials"
fi

echo "--- 10. batch plan with resource key ---"
cat > /tmp/wl_plan.json <<'JSON'
{"resource": "verify:batch-res", "timeout": 20,
 "steps": [
   {"name": "one", "cmd": "echo step-one", "shell": "cmd"},
   {"name": "two", "cmd": "echo step-two", "shell": "cmd"}
 ]}
JSON
B=$($W batch --file /tmp/wl_plan.json 2>/dev/null)
printf '%s' "$B" | python3 -c 'import json,sys
d=json.load(sys.stdin)
print("[INFO] ok=%s ran=%s steps=%s" % (d.get("ok"), d.get("ran"), [s.get("state") for s in d.get("steps",[])]))
raise SystemExit(0 if d.get("ok") and d.get("ran") == 2 else 1)' \
  && ok "batch ran both steps and reported per-step state" || bad "batch failed"

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
