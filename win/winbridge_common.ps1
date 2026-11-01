# Shared helpers for the Windows side of WinLinuxBridge. ASCII comments only.
# Dot-source this file from the other scripts:
#   . (Join-Path $PSScriptRoot 'winbridge_common.ps1')
#
# Secrets policy: this file never reads or writes secrets other than the agent
# token, which lives outside the synced tree:
#   %LOCALAPPDATA%\WinLinuxBridge\agent.token          (ACL: current user only)
#   %LOCALAPPDATA%\WinLinuxBridge\bridge.conf          (non-secret settings)

$ErrorActionPreference = 'Continue'

function Get-WbAppDir {
    # Deliberately NOT %LOCALAPPDATA%: the Microsoft Store python is an MSIX
    # package and its writes under AppData end up in a private virtual store, so
    # the agent could not see files written here (and vice versa).
    $base = $env:USERPROFILE
    if (-not $base) { $base = $env:HOME }
    return (Join-Path $base '.winlinuxbridge')
}

function Get-WbDefaultConf {
    $homeDir = $env:USERPROFILE
    $appDir = Get-WbAppDir
    return [ordered]@{
        WL_LINUX_HOST      = ''
        WL_LINUX_USER      = ''
        WL_SSH_PORT        = '22'
        WL_REMOTE_ROOT     = ''
        WL_LOCAL_ROOT      = (Join-Path $homeDir 'WinLinuxBridge')
        WL_AGENT_PORT      = '8787'
        WL_TUNNEL_PORT     = '18080'
        WL_SSH_KEY         = (Join-Path $homeDir '.ssh\wlbridge_tunnel')
        WL_SYNC_KEY        = (Join-Path $homeDir '.ssh\wlbridge_sync')
        WL_SYNC_INTERVAL   = '30'
        WL_PROGRAM_REFRESH = '900'
    }
}

# Reads bridge.conf. Priority: %USERPROFILE%\.winlinuxbridge\bridge.conf,
# then <win dir>\bridge.conf, then built-in defaults (which carry no server
# identity). Never put secrets in this file.
function Get-WbConf {
    param([string]$WinDir = $PSScriptRoot)

    $conf = Get-WbDefaultConf
    $candidates = @(
        (Join-Path (Get-WbAppDir) 'bridge.conf'),
        (Join-Path $WinDir 'bridge.conf')
    )
    foreach ($path in $candidates) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        foreach ($line in (Get-Content -LiteralPath $path -Encoding UTF8)) {
            $t = $line.Trim()
            if (-not $t) { continue }
            if ($t.StartsWith('#')) { continue }
            $i = $t.IndexOf('=')
            if ($i -lt 1) { continue }
            $k = $t.Substring(0, $i).Trim()
            $v = $t.Substring($i + 1).Trim()
            if ($v.Length -ge 2 -and $v.StartsWith('"') -and $v.EndsWith('"')) {
                $v = $v.Substring(1, $v.Length - 2)
            }
            $conf[$k] = $v
        }
        break
    }
    return $conf
}

function Save-WbConf {
    param($Conf, [string]$Path)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $lines = @('# WinLinuxBridge settings (no secrets in this file)')
    foreach ($k in $Conf.Keys) { $lines += ('{0}={1}' -f $k, $Conf[$k]) }
    [IO.File]::WriteAllLines($Path, $lines, (New-Object Text.UTF8Encoding($false)))
}

function Get-WbToken {
    $tok = $env:WL_AGENT_TOKEN
    if ($tok) { return $tok.Trim() }
    $path = Join-Path (Get-WbAppDir) 'agent.token'
    if (Test-Path -LiteralPath $path) {
        return ([IO.File]::ReadAllText($path)).Trim()
    }
    return ''
}

function Set-WbTokenFile {
    param([string]$Token)
    $appDir = Get-WbAppDir
    if (-not (Test-Path -LiteralPath $appDir)) { New-Item -ItemType Directory -Path $appDir -Force | Out-Null }
    $path = Join-Path $appDir 'agent.token'
    [IO.File]::WriteAllText($path, $Token, (New-Object Text.UTF8Encoding($false)))
    # Restrict to the current user only.
    $null = Invoke-WbProcess -FilePath 'icacls.exe' -Arguments @($path, '/inheritance:r', '/grant:r', ("{0}:(F)" -f $env:USERNAME))
    return $path
}

function Write-WbLog {
    param([string]$Message, [string]$Source = 'supervisor')
    $dir = Join-Path (Get-WbAppDir) 'logs'
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $path = Join-Path $dir ($Source + '.log')
    $line = ('{0} {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Message)
    try {
        Add-Content -LiteralPath $path -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch {
        # Logging must never break the bridge.
    }
    if (-not $env:WL_QUIET) { Write-Host $line }
}

# Registers (or refreshes) the 5 minute watchdog task. Kept here rather than in
# install-win.ps1 so the same helper is available to every entry point.
function Register-WbWatchdog {
    param([string]$StartCmdPath)
    $launcher = Join-Path (Split-Path -Parent $StartCmdPath) 'start-win.vbs'
    try {
        if (-not (Test-Path -LiteralPath $launcher)) { throw ('launcher missing: ' + $launcher) }
        $action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\wscript.exe') `
            -Argument ('//B //Nologo "' + $launcher + '"')
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
            -RepetitionInterval ([TimeSpan]::FromMinutes(5))
        $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
            -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $null = Register-ScheduledTask -TaskName 'WinLinuxBridge-Watchdog' -Action $action `
            -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop
        return $true
    } catch {
        Write-WbLog ('watchdog: registration failed: ' + $_.Exception.Message) 'install'
        return $false
    }
}

function Get-WbPython {
    # The shipped portable runtime wins: the end user must not have to install
    # Python, and the Store build virtualises writes under AppData (see D-015).
    $local = Join-Path $PSScriptRoot 'runtime\python.exe'
    if (Test-Path -LiteralPath $local) { return @{ Exe = $local; Args = @(); Bundled = $true } }
    $cands = @(
        @{ Exe = 'python';  Args = @() },
        @{ Exe = 'py';      Args = @('-3') },
        @{ Exe = 'python3'; Args = @() }
    )
    foreach ($c in $cands) {
        $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $result = Invoke-WbProcess -FilePath $c.Exe -Arguments @($c.Args + @('-c', 'import sys'))
            if ($result.rc -eq 0) { return @{ Exe = $c.Exe; Args = $c.Args; Bundled = $false } }
        } catch { }
    }
    return $null
}

function Get-WbSshExe {
    $p = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh.exe'
    if (Test-Path -LiteralPath $p) { return $p }
    $cmd = Get-Command ssh -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-WbScpExe {
    $p = Join-Path $env:SystemRoot 'System32\OpenSSH\scp.exe'
    if (Test-Path -LiteralPath $p) { return $p }
    $cmd = Get-Command scp -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-WbPlinkExe {
    # Vendor copy first: the bridge must work on a machine with no PuTTY installed.
    $local = Join-Path $PSScriptRoot 'vendor\plink.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    foreach ($p in @('D:\PuTTY\plink.EXE', 'C:\Program Files\PuTTY\plink.exe', 'C:\Program Files (x86)\PuTTY\plink.exe')) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    $cmd = Get-Command plink -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# Parses the host key fingerprint out of a plink "not cached" message.
function Get-WbHostKeyFingerprint {
    param([string]$Target, [int]$Port = 22, [string]$Plink)
    if (-not $Plink) { $Plink = Get-WbPlinkExe }
    if (-not $Plink) { return '' }
    $out = (Invoke-WbProcess -FilePath $Plink -Arguments @('-ssh', '-batch', '-P', [string]$Port, $Target, 'exit')).out
    $m = [regex]::Match($out, 'SHA256:[A-Za-z0-9+/=]+')
    if ($m.Success) { return $m.Value }
    return ''
}

# True when plink can already connect without asking about the host key.
function Test-WbHostKeyCached {
    param([string]$Target, [int]$Port = 22, [string]$Plink)
    if (-not $Plink) { $Plink = Get-WbPlinkExe }
    if (-not $Plink) { return $false }
    $out = (Invoke-WbProcess -FilePath $Plink -Arguments @('-ssh', '-batch', '-P', [string]$Port, $Target, 'exit')).out
    return ($out -notmatch 'not cached' -and $out -notmatch 'Cannot confirm a host key')
}

# Common ssh options for the sync key (exec allowed, no pty, no forwarding).
function Get-WbSshCommonArgs {
    param($Conf)
    $port = $Conf['WL_SSH_PORT']
    if (-not $port) { $port = '22' }
    return @(
        '-p', [string]$port,
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'ConnectTimeout=10',
        '-o', 'IdentitiesOnly=yes',
        '-o', ('UserKnownHostsFile=' + (Join-Path $env:USERPROFILE '.ssh\known_hosts')),
        '-i', $Conf['WL_SYNC_KEY']
    )
}

# scp spells the port option -P and rejects ssh-only options.
function Get-WbScpCommonArgs {
    param($Conf)
    $port = $Conf['WL_SSH_PORT']
    if (-not $port) { $port = '22' }
    return @(
        '-P', [string]$port,
        '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'ConnectTimeout=10',
        '-o', 'IdentitiesOnly=yes',
        '-o', ('UserKnownHostsFile=' + (Join-Path $env:USERPROFILE '.ssh\known_hosts')),
        '-i', $Conf['WL_SYNC_KEY']
    )
}

function Get-WbTarget {
    param($Conf)
    return ('{0}@{1}' -f $Conf['WL_LINUX_USER'], $Conf['WL_LINUX_HOST'])
}

function ConvertTo-WbProcessArguments {
    param([string[]]$Arguments)
    return (($Arguments | ForEach-Object {
        '"' + [regex]::Replace([regex]::Replace($_, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
    }) -join ' ')
}

function Invoke-WbProcess {
    param([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSec = 60, [string]$InputText)
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = ConvertTo-WbProcessArguments -Arguments $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = $PSBoundParameters.ContainsKey('InputText')
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($info.RedirectStandardInput) {
            $process.StandardInput.Write($InputText)
            $process.StandardInput.Close()
        }
        if (-not $process.WaitForExit($TimeoutSec * 1000)) {
            $process.Kill()
            $process.WaitForExit()
            throw ('process timed out after {0}s: {1}' -f $TimeoutSec, $FilePath)
        }
        return @{ rc = $process.ExitCode; out = ($stdout.Result + $stderr.Result).Trim() }
    } finally {
        $process.Dispose()
    }
}

function Start-WbBackgroundProcess {
    param([string]$FilePath, [string[]]$Arguments, [string]$StdoutPath, [string]$StderrPath)
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = ConvertTo-WbProcessArguments -Arguments $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = [bool]$StdoutPath
    $info.RedirectStandardError = [bool]$StderrPath
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        if ($StdoutPath) {
            $stream = [IO.FileStream]::new($StdoutPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite, 1, $true)
        }
        if ($StderrPath) {
            $errorStream = [IO.FileStream]::new($StderrPath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite, 1, $true)
        }
        $null = $process.Start()
        if ($StdoutPath) {
            $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($stream)
        }
        if ($StderrPath) {
            $stderrTask = $process.StandardError.BaseStream.CopyToAsync($errorStream)
        }
        return @{ Process = $process; StdoutTask = $stdoutTask; StderrTask = $stderrTask;
                  StdoutStream = $stream; StderrStream = $errorStream }
    } catch {
        if ($stream) { $stream.Dispose() }
        if ($errorStream) { $errorStream.Dispose() }
        $process.Dispose()
        throw
    }
}

function Invoke-WbRemote {
    param($Conf, [string]$Command, [int]$TimeoutSec = 60)
    $ssh = Get-WbSshExe
    if (-not $ssh) { throw 'ssh.exe not found' }
    $sshArgs = (Get-WbSshCommonArgs -Conf $Conf) + @((Get-WbTarget -Conf $Conf), $Command)
    return Invoke-WbProcess -FilePath $ssh -Arguments $sshArgs -TimeoutSec $TimeoutSec
}

function Invoke-WbApi {
    param([string]$Path, $Body = $null, [int]$TimeoutSec = 30, $Conf)
    if (-not $Conf) { $Conf = Get-WbConf }
    $port = [int]$Conf['WL_AGENT_PORT']
    $p = @{
        Uri             = ('http://127.0.0.1:{0}{1}' -f $port, $Path)
        Headers         = @{ Authorization = ('Bearer {0}' -f (Get-WbToken)) }
        TimeoutSec      = $TimeoutSec
        UseBasicParsing = $true
        ErrorAction     = 'Stop'
        Method          = 'GET'
    }
    if ($null -ne $Body) {
        $p.Method = 'POST'
        $p.ContentType = 'application/json; charset=utf-8'
        $p.Body = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json $Body -Depth 8 -Compress))
    }
    return Invoke-RestMethod @p
}

function Test-WbAgent {
    param($Conf)
    try {
        $null = Invoke-WbApi -Path '/ping' -TimeoutSec 3 -Conf $Conf
        return $true
    } catch {
        return $false
    }
}

# Finds processes whose command line contains every token in -Match.
function Get-WbProcess {
    param([string]$Name, [string[]]$Match = @())
    $list = @()
    try {
        $list = @(Get-CimInstance Win32_Process -Filter ("Name='{0}'" -f $Name) -ErrorAction Stop)
    } catch {
        return @()
    }
    if ($Match.Count -eq 0) { return $list }
    $hits = @()
    foreach ($p in $list) {
        $cmdline = [string]$p.CommandLine
        if (-not $cmdline) { continue }
        $ok = $true
        foreach ($m in $Match) { if ($cmdline.IndexOf($m, [StringComparison]::OrdinalIgnoreCase) -lt 0) { $ok = $false; break } }
        if ($ok) { $hits += $p }
    }
    return $hits
}

function Stop-WbProcess {
    param($Processes)
    foreach ($p in $Processes) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
    }
}

# Matches by command line, ignoring the executable name: the Microsoft Store
# python runs as python3.13.exe, so a Name='python.exe' filter misses our agent.
function Get-WbProcessByCommandLine {
    param([string]$Text)
    $hits = @()
    try {
        $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        return @()
    }
    foreach ($p in $all) {
        $cl = [string]$p.CommandLine
        if ($cl -and $cl.IndexOf($Text, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $hits += $p }
    }
    return $hits
}
