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
    & icacls.exe $path /inheritance:r /grant:r ("{0}:(F)" -f $env:USERNAME) | Out-Null
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
    if (-not (Test-Path -LiteralPath $StartCmdPath)) { return $false }
    $null = & schtasks.exe /Create /F /SC MINUTE /MO 5 /TN 'WinLinuxBridge-Watchdog' /TR ('"' + $StartCmdPath + '"') 2>&1
    return ($LASTEXITCODE -eq 0)
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
            & $c.Exe @($c.Args + @('-c', 'import sys')) 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return @{ Exe = $c.Exe; Args = $c.Args; Bundled = $false } }
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
    $out = & $Plink -ssh -batch -P $Port $Target 'exit' 2>&1 | Out-String
    $m = [regex]::Match($out, 'SHA256:[A-Za-z0-9+/=]+')
    if ($m.Success) { return $m.Value }
    return ''
}

# True when plink can already connect without asking about the host key.
function Test-WbHostKeyCached {
    param([string]$Target, [int]$Port = 22, [string]$Plink)
    if (-not $Plink) { $Plink = Get-WbPlinkExe }
    if (-not $Plink) { return $false }
    $out = & $Plink -ssh -batch -P $Port $Target 'exit' 2>&1 | Out-String
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

function Invoke-WbRemote {
    param($Conf, [string]$Command, [int]$TimeoutSec = 60)
    $ssh = Get-WbSshExe
    if (-not $ssh) { throw 'ssh.exe not found' }
    $sshArgs = (Get-WbSshCommonArgs -Conf $Conf) + @((Get-WbTarget -Conf $Conf), $Command)
    $out = & $ssh @sshArgs 2>&1
    return @{ rc = $LASTEXITCODE; out = ($out | Out-String).Trim() }
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
