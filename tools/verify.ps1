# WinLinuxBridge - self check (Windows side).
#
# Runs WITHOUT the tunnel and without the real token:
#   * static checks (python syntax, PowerShell syntax, LF-only shell scripts)
#   * starts a throw-away agent on a spare port and exercises every endpoint
#
# Usage:  powershell -ExecutionPolicy Bypass -File tools\verify.ps1
#         powershell ... -File tools\verify.ps1 -Port 18787 -KeepRunning
#
# Exit code 0 = all checks passed. ASCII comments only.

param(
    [int]$Port = 18787,
    [switch]$KeepRunning
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'win\winbridge_common.ps1')

$fail = 0
$pass = 0

function Check {
    param([string]$Name, [scriptblock]$Test)
    try {
        $r = & $Test
        $script:pass++
        Write-Host ('[PASS] {0}: {1}' -f $Name, $r) -ForegroundColor Green
    } catch {
        $script:fail++
        Write-Host ('[FAIL] {0}: {1}' -f $Name, $_.Exception.Message) -ForegroundColor Red
    }
}

function Info {
    param([string]$Text)
    Write-Host ('[INFO] ' + $Text) -ForegroundColor DarkGray
}

Write-Host '--- static checks ---'

Check 'winbridge_agent.py compiles' {
    $py = Get-WbPython
    if (-not $py) { throw 'no python interpreter' }
    $agent = Join-Path $root 'win\winbridge_agent.py'
    $args = @($py.Args) + @('-m', 'py_compile', ('"' + $agent + '"'))
    & $py.Exe @args 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ('py_compile rc=' + $LASTEXITCODE) }
    'ok'
}

Check 'job engine and client compile' {
    $py = Get-WbPython
    if (-not $py) { throw 'no python interpreter' }
    $targets = @(
        (Join-Path $root 'win\winbridge_jobs.py'),
        (Join-Path $root 'linux\wl_job.py'),
        (Join-Path $root 'win\WinBridgeGUI.pyw')
    )
    foreach ($t in $targets) {
        $args = @($py.Args) + @('-m', 'py_compile', ('"' + $t + '"'))
        & $py.Exe @args 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw ('py_compile failed: ' + (Split-Path -Leaf $t)) }
    }
    ('{0} modules' -f $targets.Count)
}

Check 'all PowerShell scripts parse' {
    $bad = @()
    foreach ($f in (Get-ChildItem -LiteralPath (Join-Path $root 'win') -Filter '*.ps1')) {
        $errors = $null
        $null = [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errors)
        if ($errors -and $errors.Count -gt 0) { $bad += ('{0}: {1}' -f $f.Name, $errors[0].Message) }
    }
    if ($bad.Count -gt 0) { throw ($bad -join '; ') }
    'ok'
}

Check 'shell scripts use LF only' {
    $bad = @()
    foreach ($rel in @('linux\wl', 'linux\install-linux.sh', 'tools\verify.sh')) {
        $p = Join-Path $root $rel
        if (-not (Test-Path -LiteralPath $p)) { continue }
        $text = [IO.File]::ReadAllText($p)
        if ($text.Contains("`r")) { $bad += $rel }
    }
    if ($bad.Count -gt 0) { throw ('CRLF found in: ' + ($bad -join ', ')) }
    'ok'
}

Check 'no token literal in shipped config' {
    $p = Join-Path $root 'win\bridge.conf'
    if (Test-Path -LiteralPath $p) {
        if ((Get-Content -LiteralPath $p -Raw) -match 'TOKEN') { throw 'bridge.conf contains a token key' }
    }
    'ok'
}

Check 'required files exist' {
    $need = @('win\winbridge_agent.py', 'win\winbridge_jobs.py', 'win\winbridge_common.ps1',
              'win\winbridge_sync.ps1', 'win\winbridge_supervisor.ps1', 'win\start-win.cmd',
              'win\install-win.ps1', 'win\WinBridgeGUI.pyw', 'win\start-gui.cmd',
              'linux\wl', 'linux\wl_job.py', 'linux\install-linux.sh',
              'tools\verify.sh', 'tools\verify_jobs.sh', 'tools\verify_cancel.sh')
    $missing = @()
    foreach ($rel in $need) { if (-not (Test-Path -LiteralPath (Join-Path $root $rel))) { $missing += $rel } }
    if ($missing.Count -gt 0) { throw ('missing: ' + ($missing -join ', ')) }
    ('{0} files' -f $need.Count)
}

Check 'bundled runtime is complete' {
    $rt = Join-Path $root 'win\runtime'
    $need = @('python.exe', 'pythonw.exe', 'python3.dll', '_tkinter.pyd', 'tcl86t.dll',
              'tk86t.dll', 'zlib1.dll', 'lib\tkinter\__init__.py',
              'tcl\tcl8.6\init.tcl', 'tcl\tk8.6\tk.tcl')
    $missing = @()
    foreach ($rel in $need) { if (-not (Test-Path -LiteralPath (Join-Path $rt $rel))) { $missing += $rel } }
    if ($missing.Count -gt 0) { throw ('incomplete: ' + ($missing -join ', ') + ' - run tools\build-runtime.ps1') }
    $size = (Get-ChildItem -LiteralPath $rt -Recurse -File | Measure-Object -Sum Length).Sum
    ('{0:N1} MB, tkinter present' -f ($size / 1MB))
}

Check 'bundled runtime can open a Tk window' {
    $exe = Join-Path $root 'win\runtime\python.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw 'runtime\python.exe missing' }
    $script = Join-Path $env:TEMP ('wl_tk_' + [guid]::NewGuid().ToString('N') + '.py')
    $code = @(
        'import os, sys',
        'root = os.path.dirname(sys.executable)',
        'os.add_dll_directory(root)',
        'os.environ.setdefault("TCL_LIBRARY", os.path.join(root, "tcl", "tcl8.6"))',
        'os.environ.setdefault("TK_LIBRARY", os.path.join(root, "tcl", "tk8.6"))',
        'import tkinter',
        'r = tkinter.Tk(); r.withdraw()',
        'print(r.tk.call("info", "patchlevel")); r.destroy()'
    ) -join "`n"
    [IO.File]::WriteAllText($script, $code, (New-Object Text.UTF8Encoding($false)))
    try {
        $out = & $exe $script 2>&1
        if ($LASTEXITCODE -ne 0) { throw (($out | Out-String).Trim()) }
        ('Tk ' + (($out | Out-String).Trim()))
    } finally {
        Remove-Item -LiteralPath $script -Force -ErrorAction SilentlyContinue
    }
}

Check 'vendor binaries and licences present' {
    $need = @('win\vendor\plink.exe', 'win\vendor\LICENCE-PuTTY.txt', 'win\vendor\LICENSE-Python.txt')
    $missing = @()
    foreach ($rel in $need) { if (-not (Test-Path -LiteralPath (Join-Path $root $rel))) { $missing += $rel } }
    if ($missing.Count -gt 0) { throw ('missing: ' + ($missing -join ', ')) }
    'plink.exe + 2 licences'
}

Check 'GUI headless selftest' {
    $exe = Join-Path $root 'win\runtime\python.exe'
    if (-not (Test-Path -LiteralPath $exe)) { $exe = (Get-WbPython).Exe }
    $gui = Join-Path $root 'win\WinBridgeGUI.pyw'
    $out = & $exe $gui --selftest 2>&1
    $text = ($out | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw ($text -replace "`r?`n", ' | ') }
    $failed = ($text -split "`r?`n" | Where-Object { $_ -like 'FAIL*' })
    if ($failed) { throw ($failed -join '; ') }
    (($text -split "`r?`n").Count.ToString() + ' checks')
}

Check 'GUI never writes secrets to bridge.conf' {
    $gui = Join-Path $root 'win\WinBridgeGUI.pyw'
    $text = [IO.File]::ReadAllText($gui)
    if ($text -match 'write_conf\([^)]*password') { throw 'password appears to be written into bridge.conf' }
    if ($text -notmatch 'CryptProtectData') { throw 'DPAPI is not used for the saved password' }
    'password only via DPAPI'
}

Check 'GUI window builds with all controls' {
    $exe = Join-Path $root 'win\runtime\python.exe'
    if (-not (Test-Path -LiteralPath $exe)) { $exe = (Get-WbPython).Exe }
    $gui = Join-Path $root 'win\WinBridgeGUI.pyw'
    $out = & $exe $gui --uitest 2>&1
    $text = ($out | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw ($text -replace "`r?`n", ' | ') }
    (($text -split "`r?`n").Count.ToString() + ' ui checks')
}

Check 'PowerShell scripts are UTF-8 with BOM' {
    # Windows PowerShell 5.1 decodes a BOM-less .ps1 as ANSI. A Chinese character
    # whose second byte falls in 0x40-0xFE can then swallow a following { or ) and
    # the whole file fails to parse. BOM is not cosmetic here.
    $bad = @()
    foreach ($f in (Get-ChildItem -Path $root -Recurse -Filter '*.ps1')) {
        $bytes = [IO.File]::ReadAllBytes($f.FullName)
        $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
        if (-not $hasBom) { $bad += $f.Name }
    }
    if ($bad.Count -gt 0) { throw ('missing UTF-8 BOM: ' + ($bad -join ', ')) }
    'ok'
}

Check 'autostart entry points at an existing single-line path' {
    $startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'WinLinuxBridge.cmd'
    if (-not (Test-Path -LiteralPath $startup)) { throw 'no Startup entry (run install-win.ps1 or use the GUI checkbox)' }
    $raw = [IO.File]::ReadAllText($startup)
    $lines = @($raw -split "`r?`n" | Where-Object { $_.Trim() })
    $hit = @($lines | Where-Object { $_ -match '-File\s+"' })
    if ($hit.Count -ne 1) { throw ('expected exactly 1 command line, found ' + $hit.Count) }
    $m = [regex]::Match($hit[0], '-File\s+"([^"]+)"')
    if (-not $m.Success) { throw 'the -File argument is not quoted on one line' }
    $target = $m.Groups[1].Value
    if (-not (Test-Path -LiteralPath $target)) { throw ('autostart points at a missing file: ' + $target) }
    ('-> ' + (Split-Path -Leaf $target))
}

Check 'mirror has the scripts autostart and the supervisor need' {
    $confPath = Join-Path $env:USERPROFILE '.winlinuxbridge\bridge.conf'
    if (-not (Test-Path -LiteralPath $confPath)) { return 'no config yet (fresh machine) - skipped' }
    $local = ''
    foreach ($line in (Get-Content -LiteralPath $confPath -Encoding UTF8)) {
        $t = $line.Trim()
        if ($t -like 'WL_LOCAL_ROOT=*') { $local = ($t -split '=', 2)[1].Trim() }
    }
    if (-not $local) { return 'no WL_LOCAL_ROOT - skipped' }
    $need = @('win\winbridge_supervisor.ps1', 'win\start-win.cmd', 'win\winbridge_agent.py')
    $missing = @()
    foreach ($rel in $need) { if (-not (Test-Path -LiteralPath (Join-Path $local $rel))) { $missing += $rel } }
    if ($missing.Count -gt 0) { throw ('mirror ' + $local + ' is incomplete: ' + ($missing -join ', ') + ' (run install-win.ps1)') }
    $local
}

Write-Host ''
Write-Host '--- runtime checks (throw-away agent) ---'

$py = Get-WbPython
if (-not $py) { Write-Host '[FAIL] no python interpreter available' -ForegroundColor Red; exit 1 }

$agent = Join-Path $root 'win\winbridge_agent.py'
$token = 'verify-' + [guid]::NewGuid().ToString('N')
$sandbox = Join-Path $env:TEMP ('wlverify-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null

$env:WL_AGENT_TOKEN = $token
$env:WL_AGENT_PORT = [string]$Port
$env:WL_BRIDGE_DIR = $sandbox

$argStr = (@($py.Args) + @('"' + $agent + '"')) -join ' '
$stdout = Join-Path $sandbox 'agent.out'
$stderr = Join-Path $sandbox 'agent.err'
$proc = $null
$proc = Start-Process -FilePath $py.Exe -ArgumentList $argStr -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr

$conf = @{ WL_AGENT_PORT = [string]$Port }

try {
    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if (Test-WbAgent -Conf $conf) { $up = $true; break }
        if ($proc.HasExited) { break }
    }
    if (-not $up) {
        $tail = ''
        if (Test-Path -LiteralPath $stderr) { $tail = (Get-Content -LiteralPath $stderr -Raw) }
        throw ('agent did not start; stderr: ' + $tail)
    }

    Check 'ping' { (Invoke-WbApi -Path '/ping' -Conf $conf).version }
    Check 'run/powershell' { (Invoke-WbApi -Path '/run' -Body @{ cmd = 'Get-Date -Format o'; shell = 'powershell' } -Conf $conf).out.Trim() }
    Check 'run/cmd' { (Invoke-WbApi -Path '/run' -Body @{ cmd = 'echo %COMPUTERNAME%'; shell = 'cmd' } -Conf $conf).out.Trim() }
    Check 'run/utf8-output' {
        $r = Invoke-WbApi -Path '/run' -Body @{ cmd = 'Write-Output "OK-中文"'; shell = 'powershell' } -Conf $conf
        if ($r.out -notmatch 'OK-中文') { throw ('encoding broken: ' + $r.out) }
        'utf-8 round trip ok'
    }
    Check 'run/exit-code' {
        $r = Invoke-WbApi -Path '/run' -Body @{ cmd = 'exit 7'; shell = 'cmd' } -Conf $conf
        if ($r.rc -ne 7) { throw ('rc=' + $r.rc) }
        'rc=7'
    }
    Check 'run/timeout' {
        $r = Invoke-WbApi -Path '/run' -Body @{ cmd = 'Start-Sleep -Seconds 5'; shell = 'powershell'; timeout = 1 } -TimeoutSec 30 -Conf $conf
        if (-not $r.timed_out) { throw 'timeout flag not set' }
        'timed_out=true'
    }
    Check 'windows/list' { (Invoke-WbApi -Path '/windows' -Conf $conf).windows.Count.ToString() + ' windows' }
    Check 'screenshot' {
        $r = Invoke-WbApi -Path '/screenshot' -Body @{ mode = 'path' } -Conf $conf
        if (-not (Test-Path -LiteralPath $r.path)) { throw ('missing ' + $r.path) }
        if ($r.warning) { return ('{0:N0} bytes (warning: {1})' -f $r.size, $r.warning) }
        '{0:N0} bytes' -f $r.size
    }
    Check 'clipboard/roundtrip' {
        $null = Invoke-WbApi -Path '/clipboard' -Body @{ op = 'set'; text = 'winbridge-verify' } -Conf $conf
        $t = (Invoke-WbApi -Path '/clipboard' -Body @{ op = 'get' } -Conf $conf).text
        if ($t -ne 'winbridge-verify') { throw ('got ' + $t) }
        'ok'
    }
    Check 'http/proxy' {
        $r = Invoke-WbApi -Path '/http' -Body @{ url = ('http://127.0.0.1:{0}/ping' -f $Port); headers = @{ Authorization = ('Bearer ' + $token) } } -Conf $conf
        if ($r.status -ne 200) { throw ('status=' + $r.status) }
        'status 200'
    }
    Check 'fs/write-read-list' {
        $w = Invoke-WbApi -Path '/fs' -Body @{ op = 'write'; path = 'shared/verify.txt'; text = 'hello-中文' } -Conf $conf
        if (-not $w.ok) { throw 'write failed' }
        $r = Invoke-WbApi -Path '/fs' -Body @{ op = 'read'; path = 'shared/verify.txt' } -Conf $conf
        if ($r.text -ne 'hello-中文') { throw ('read mismatch: ' + $r.text) }
        $l = Invoke-WbApi -Path '/fs' -Body @{ op = 'list'; path = 'shared' } -Conf $conf
        if ($l.count -lt 1) { throw 'list empty' }
        ('{0} entries' -f $l.count)
    }
    Check 'fs/rejects-outside-path' {
        $code = 0
        try {
            $null = Invoke-WbApi -Path '/fs' -Body @{ op = 'list'; path = 'C:\Windows\System32\drivers\etc' } -Conf $conf
        } catch {
            $resp = $_.Exception.Response
            if ($resp) { $code = [int]$resp.StatusCode } else { $code = -1 }
        }
        if ($code -ne 403) { throw ('expected 403, got ' + $code) }
        '403'
    }
    Check 'auth/required' {
        $noauth = @{ Uri = ('http://127.0.0.1:{0}/ping' -f $Port); UseBasicParsing = $true; TimeoutSec = 10 }
        $code = 0
        try { $null = Invoke-RestMethod @noauth } catch {
            $resp = $_.Exception.Response
            if ($resp) { $code = [int]$resp.StatusCode } else { $code = -1 }
        }
        if ($code -ne 401) { throw ('expected 401, got ' + $code) }
        '401'
    }

    # --- job engine ------------------------------------------------------- #
    Check 'job/start+status+result' {
        $start = Invoke-WbApi -Path '/job/start' -Body @{ cmd = 'Write-Output "job-中文"'; shell = 'powershell'; timeout = 30 } -Conf $conf
        $id = $start.job_id
        if (-not $id) { throw 'no job_id returned' }
        $final = $start
        for ($i = 0; $i -lt 60; $i++) {
            $final = Invoke-WbApi -Path '/job/status' -Body @{ job_id = $id; wait = 1 } -Conf $conf
            if ($final.state -in @('SUCCEEDED','FAILED','TIMEOUT','CANCELLED','LOST','SPAWN_ERROR')) { break }
        }
        if ($final.state -ne 'SUCCEEDED') { throw ('state=' + $final.state) }
        $result = Invoke-WbApi -Path '/job/result' -Body @{ job_id = $id } -Conf $conf
        if ($result.stdout.text -notmatch 'job-中文') { throw ('stdout=' + $result.stdout.text) }
        ($id + ' ' + $final.state)
    }

    Check 'job/retry-safety (request_id dedupe)' {
        $rid = 'verify-' + [guid]::NewGuid().ToString('N')
        $a = Invoke-WbApi -Path '/job/start' -Body @{ cmd = 'echo one'; shell = 'cmd'; request_id = $rid } -Conf $conf
        Start-Sleep -Milliseconds 1200
        $b = Invoke-WbApi -Path '/job/start' -Body @{ cmd = 'echo two'; shell = 'cmd'; request_id = $rid } -Conf $conf
        if ($a.job_id -ne $b.job_id) { throw ('two jobs were created: ' + $a.job_id + ' / ' + $b.job_id) }
        if (-not $b.deduplicated) { throw 'second start was not marked deduplicated' }
        'same job id reused'
    }

    Check 'job/pending-wait-is-not-a-failure' {
        $start = Invoke-WbApi -Path '/job/start' -Body @{
            cmd = 'Start-Sleep -Seconds 20'; shell = 'powershell'; timeout = 600 } -Conf $conf
        $info = Invoke-WbApi -Path '/job/status' -Body @{ job_id = $start.job_id; wait = 2 } -Conf $conf
        if ($info.state -eq 'RUNNING') { $out = 'RUNNING after a 2s wait (correct)' }
        elseif ($info.state -eq 'SUCCEEDED') { $out = 'finished early' }
        else { throw ('unexpected state ' + $info.state) }
        $null = Invoke-WbApi -Path '/job/cancel' -Body @{ job_id = $start.job_id } -Conf $conf
        $out
    }

    Check 'job/execution-timeout' {
        $start = Invoke-WbApi -Path '/job/start' -Body @{
            cmd = 'Start-Sleep -Seconds 30'; shell = 'powershell'; timeout = 2 } -Conf $conf
        $final = $start
        for ($i = 0; $i -lt 80; $i++) {
            $final = Invoke-WbApi -Path '/job/status' -Body @{ job_id = $start.job_id; wait = 1 } -Conf $conf
            if ($final.state -in @('SUCCEEDED','FAILED','TIMEOUT','CANCELLED','LOST','SPAWN_ERROR')) { break }
        }
        if ($final.state -ne 'TIMEOUT') { throw ('state=' + $final.state) }
        if ($final.error_code -ne 'TIMEOUT_KILLED') { throw ('error_code=' + $final.error_code) }
        'TIMEOUT / TIMEOUT_KILLED'
    }

    Check 'job/cancel-is-isolated' {
        $keep = Invoke-WbApi -Path '/job/start' -Body @{
            cmd = 'Start-Sleep -Seconds 60'; shell = 'powershell'; timeout = 600 } -Conf $conf
        $doomed = Invoke-WbApi -Path '/job/start' -Body @{
            cmd = 'Start-Sleep -Seconds 60'; shell = 'powershell'; timeout = 600 } -Conf $conf
        Start-Sleep -Milliseconds 1500
        $cancel = Invoke-WbApi -Path '/job/cancel' -Body @{ job_id = $doomed.job_id } -Conf $conf
        if ($cancel.state -ne 'CANCELLED') { throw ('cancelled job state=' + $cancel.state) }
        $neighbour = Invoke-WbApi -Path '/job/status' -Body @{ job_id = $keep.job_id; wait = 0 } -Conf $conf
        if ($neighbour.state -ne 'RUNNING') { throw ('neighbour state=' + $neighbour.state) }
        $null = Invoke-WbApi -Path '/job/cancel' -Body @{ job_id = $keep.job_id } -Conf $conf
        'neighbour untouched'
    }

    Check 'job/big-output-summary' {
        $start = Invoke-WbApi -Path '/job/start' -Body @{
            cmd = '1..3000 | ForEach-Object { Write-Output ("line " + $_ + " " + ("x" * 50)) }'
            shell = 'powershell'; timeout = 60 } -Conf $conf
        $final = $start
        for ($i = 0; $i -lt 80; $i++) {
            $final = Invoke-WbApi -Path '/job/status' -Body @{ job_id = $start.job_id; wait = 1 } -Conf $conf
            if ($final.state -in @('SUCCEEDED','FAILED','TIMEOUT','CANCELLED','LOST','SPAWN_ERROR')) { break }
        }
        $r = Invoke-WbApi -Path '/job/result' -Body @{ job_id = $start.job_id } -Conf $conf
        if ($r.stdout.bytes -lt 100000) { throw ('bytes=' + $r.stdout.bytes) }
        if ($r.stdout.omitted_bytes -le 0) { throw 'large output was not summarised' }
        if (($r.stdout.sha256 | Measure-Object -Character).Characters -ne 64) { throw 'no sha256 for the full file' }
        ('{0:N0} bytes kept on disk, summary only' -f $r.stdout.bytes)
    }

    Check 'job/logs-cursor' {
        $start = Invoke-WbApi -Path '/job/start' -Body @{
            cmd = '1..40 | ForEach-Object { Write-Output ("c" + $_) }'; shell = 'powershell'; timeout = 30 } -Conf $conf
        $final = $start
        for ($i = 0; $i -lt 60; $i++) {
            $final = Invoke-WbApi -Path '/job/status' -Body @{ job_id = $start.job_id; wait = 1 } -Conf $conf
            if ($final.state -in @('SUCCEEDED','FAILED','TIMEOUT','CANCELLED','LOST','SPAWN_ERROR')) { break }
        }
        $first = Invoke-WbApi -Path '/job/logs' -Body @{ job_id = $start.job_id; since = 0; max_bytes = 50 } -Conf $conf
        $second = Invoke-WbApi -Path '/job/logs' -Body @{ job_id = $start.job_id; since = $first.next; max_bytes = 50000 } -Conf $conf
        if ($first.next -le 0) { throw 'no cursor returned' }
        if ($second.from -ne $first.next) { throw ('cursor mismatch ' + $second.from + ' vs ' + $first.next) }
        ('cursor ' + $first.next + ' -> ' + $second.next + ' of ' + $second.bytes_total)
    }

    Check 'job/resource-serialisation' {
        $a = Invoke-WbApi -Path '/job/start' -Body @{
            cmd = 'Start-Sleep -Seconds 3'; shell = 'powershell'; timeout = 60; resource = 'verify:serial' } -Conf $conf
        $b = Invoke-WbApi -Path '/job/start' -Body @{
            cmd = 'Write-Output B'; shell = 'powershell'; timeout = 60; resource = 'verify:serial' } -Conf $conf
        $now = Invoke-WbApi -Path '/job/status' -Body @{ job_id = $b.job_id } -Conf $conf
        if ($now.state -notin @('QUEUED','RUNNING')) { throw ('second job state=' + $now.state) }
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            $s = Invoke-WbApi -Path '/job/status' -Body @{ job_id = $b.job_id } -Conf $conf
            if ($s.state -in @('SUCCEEDED','FAILED','TIMEOUT','CANCELLED','LOST','SPAWN_ERROR')) { break }
            Start-Sleep -Milliseconds 500
        }
        if ($s.state -ne 'SUCCEEDED') { throw ('second job ended as ' + $s.state) }
        'queued behind the first, then ran'
    }

    Check 'run/legacy-shape-kept' {
        $r = Invoke-WbApi -Path '/run' -Body @{ cmd = 'echo legacy'; shell = 'cmd' } -Conf $conf
        foreach ($field in @('rc', 'out', 'err', 'timed_out', 'duration_ms', 'shell')) {
            if (-not ($r.PSObject.Properties.Name -contains $field)) { throw ('missing legacy field ' + $field) }
        }
        if (-not $r.job_id) { throw 'no job_id on the legacy response' }
        'legacy fields + job_id'
    }

    Check 'doctor/report' {
        $d = Invoke-WbApi -Path '/doctor' -Conf $conf
        if (-not $d.runtime.agent_version) { throw 'no agent version' }
        if (-not $d.shells.powershell.available) { throw 'powershell not detected' }
        if (-not $d.timeouts.capture_cap_bytes) { throw 'no timeout report' }
        if (-not $d.paths.fs_allowed_roots) { throw 'no path report' }
        $json = $d | ConvertTo-Json -Depth 6 -Compress
        if ($json -match 'WL_AGENT_TOKEN') { throw 'doctor echoed a token variable name' }
        if ($json -match '\b[0-9a-f]{32,}\b') { throw 'doctor output looks like it contains a secret' }
        ('agent ' + $d.runtime.agent_version + ', ps ' + $d.shells.powershell.version)
    }
} catch {
    $fail++
    Write-Host ('[FAIL] runtime setup: ' + $_.Exception.Message) -ForegroundColor Red
} finally {
    if (-not $KeepRunning) {
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
        # The Windows Store 'python.exe' is a launcher; the real interpreter is
        # python3.13.exe. Kill whoever owns our throw-away port for certain.
        try {
            Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
        } catch { }
        Start-Sleep -Milliseconds 400
        Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Info ('agent kept running on port {0}, bridge dir {1}' -f $Port, $sandbox)
    }
}

Write-Host ''
if ($fail -gt 0) {
    Write-Host ('{0} passed, {1} FAILED' -f $pass, $fail) -ForegroundColor Red
    exit 1
}
Write-Host ('{0} checks passed' -f $pass) -ForegroundColor Green
exit 0
