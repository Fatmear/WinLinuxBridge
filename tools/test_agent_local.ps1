# Local smoke test for the WinBridge agent (Windows side only, no tunnel).
# Usage:  $env:WL_AGENT_TOKEN='...'; .\tools\test_agent_local.ps1
param(
    [string]$Token = $env:WL_AGENT_TOKEN,
    [int]$Port = 8787
)
$ErrorActionPreference = 'Stop'
$base = "http://127.0.0.1:$Port"
$headers = @{ Authorization = "Bearer $Token" }

function Invoke-Agent {
    param([string]$Path, $Body)
    $args = @{ Uri = "$base$Path"; Method = 'GET'; Headers = $headers; TimeoutSec = 30; UseBasicParsing = $true }
    if ($null -ne $Body) {
        $args.Method = 'POST'
        $args.ContentType = 'application/json; charset=utf-8'
        $args.Body = ([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json $Body -Depth 8 -Compress)))
    }
    Invoke-RestMethod @args
}

$fail = 0
function Check {
    param([string]$Name, [scriptblock]$Test)
    try {
        $r = & $Test
        Write-Host ("[PASS] {0}: {1}" -f $Name, $r) -ForegroundColor Green
    } catch {
        Write-Host ("[FAIL] {0}: {1}" -f $Name, $_.Exception.Message) -ForegroundColor Red
        $script:fail++
    }
}

Check 'ping' { (Invoke-Agent '/ping').version }
Check 'run/powershell' { (Invoke-Agent '/run' @{ cmd = 'Get-Date -Format o'; shell = 'powershell' }).out.Trim() }
Check 'run/cmd' { (Invoke-Agent '/run' @{ cmd = 'echo %COMPUTERNAME%'; shell = 'cmd' }).out.Trim() }
Check 'run/stdout-multibyte' {
    $r = Invoke-Agent '/run' @{ cmd = 'Write-Output "中文OK"'; shell = 'powershell' }
    if ($r.out -notmatch '中文OK') { throw "encoding broken: $($r.out)" }
    $r.out.Trim()
}
Check 'run/exitcode' {
    $r = Invoke-Agent '/run' @{ cmd = 'exit 7'; shell = 'cmd' }
    if ($r.rc -ne 7) { throw "rc=$($r.rc)" }
    "rc=$($r.rc)"
}
Check 'windows' { (Invoke-Agent '/windows').windows.Count.ToString() + ' visible windows' }
Check 'screenshot' {
    $r = Invoke-Agent '/screenshot' @{ mode = 'path' }
    if (-not (Test-Path $r.path)) { throw "missing $($r.path)" }
    "{0} ({1:N0} bytes)" -f $r.path, $r.size
}
Check 'clipboard' {
    Invoke-Agent '/clipboard' @{ op = 'set'; text = 'winbridge-clip-test' } | Out-Null
    (Invoke-Agent '/clipboard' @{ op = 'get' }).text
}
Check 'http-proxy' {
    # ask the agent to fetch its own /ping on behalf of a Windows-local service
    $r = Invoke-Agent '/http' @{ url = "$base/ping"; headers = @{ Authorization = "Bearer $Token" } }
    if ($r.status -ne 200) { throw "status=$($r.status)" }
    'status 200'
}

if ($fail -gt 0) {
    Write-Host ("`n{0} check(s) FAILED" -f $fail) -ForegroundColor Red
    exit 1
}
Write-Host "`nall checks passed" -ForegroundColor Green
