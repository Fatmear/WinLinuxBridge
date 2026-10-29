# WinLinuxBridge - one-time Windows installer (no administrator rights needed).
#
# What it does
#   1. creates two dedicated ed25519 keys (tunnel + sync)
#   2. installs their public keys on the Linux account in ~/.ssh/authorized_keys
#      (this is the ONLY step that uses your Linux password, and the password is
#       never written to disk, never put in a file and never echoed)
#   3. uploads this delivery folder to ~/WinLinuxBridge on Linux
#   4. shares the agent token (Windows env var + 0600 file on Linux)
#   5. registers autostart for the current user (Startup folder + watchdog task)
#   6. pulls the folder back as a local mirror and starts the supervisor
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install-win.ps1
#   $env:WL_LINUX_PASSWORD='...'   # optional, local env var only, keeps it non-interactive
#   ... -LinuxHost linux-host.example -LinuxUser deploy -RotateToken
#   ... -SkipKeys -SkipUpload      # refresh files only
#
# Nothing here is tied to one particular server: host, user, port and the remote
# folder all come from arguments or from bridge.conf, and the host key is
# discovered from the server itself instead of being hard coded.
#
# ASCII comments only.

param(
    [string]$LinuxHost = $env:WL_LINUX_HOST,
    [string]$LinuxUser = $env:WL_LINUX_USER,
    [string]$RemoteRoot = '',
    [string]$SshPort = '',
    [string]$HostKey = '',
    [switch]$RotateToken,
    [switch]$SkipKeys,
    [switch]$SkipUpload,
    [switch]$SkipAssets,
    [switch]$NoStart,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'winbridge_common.ps1')

function Step { param([string]$Text) Write-Host ''; Write-Host ('== ' + $Text) -ForegroundColor Cyan }
function Ok   { param([string]$Text) Write-Host ('   OK  ' + $Text) -ForegroundColor Green }
function Warn { param([string]$Text) Write-Host ('   !!  ' + $Text) -ForegroundColor Yellow }

$conf = Get-WbConf -WinDir $PSScriptRoot
if ($LinuxHost) { $conf['WL_LINUX_HOST'] = $LinuxHost }
if ($LinuxUser) { $conf['WL_LINUX_USER'] = $LinuxUser }
if ($SshPort)   { $conf['WL_SSH_PORT']   = $SshPort }
if ($RemoteRoot) { $conf['WL_REMOTE_ROOT'] = $RemoteRoot }
elseif (-not $conf['WL_REMOTE_ROOT']) {
    $conf['WL_REMOTE_ROOT'] = ('/home/{0}/WinLinuxBridge' -f $conf['WL_LINUX_USER'])
}
$target = Get-WbTarget -Conf $conf
$sshPort = [int]$conf['WL_SSH_PORT']
$localRoot = $conf['WL_LOCAL_ROOT']
$remoteRoot = $conf['WL_REMOTE_ROOT']
$sshKey = $conf['WL_SSH_KEY']
$syncKey = $conf['WL_SYNC_KEY']
$ssh = Get-WbSshExe
$scp = Get-WbScpExe
$plink = Get-WbPlinkExe

Step 'Checking local prerequisites'
if (-not $ssh) { throw 'ssh.exe not found (Windows OpenSSH client is required)' }
if (-not $scp) { throw 'scp.exe not found (Windows OpenSSH client is required)' }
$py = Get-WbPython
if (-not $py) { throw 'no Python interpreter found (python / py -3 / python3)' }
Ok ('ssh   = ' + $ssh)
Ok ('scp   = ' + $scp)
Ok ('python= ' + $py.Exe)
if ($plink) { Ok ('plink = ' + $plink) } else { Warn 'plink.exe not found: the one-time key install will need manual steps' }

Step 'Creating dedicated keys'
$sshKeygen = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh-keygen.exe'
if (-not (Test-Path -LiteralPath $sshKeygen)) { $sshKeygen = (Get-Command ssh-keygen).Source }
foreach ($spec in @(
        @{ Path = $sshKey; Comment = 'wlbridge-tunnel' },
        @{ Path = $syncKey; Comment = 'wlbridge-sync' })) {
    if (Test-Path -LiteralPath $spec.Path) {
        Ok ('exists: ' + $spec.Path)
        continue
    }
    $dir = Split-Path -Parent $spec.Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $kargs = @('-t', 'ed25519', '-f', ('"' + $spec.Path + '"'), '-N', '""', '-C', $spec.Comment, '-q')
    Start-Process -FilePath $sshKeygen -ArgumentList $kargs -Wait -NoNewWindow | Out-Null
    if (-not (Test-Path -LiteralPath $spec.Path)) { throw ('ssh-keygen failed for ' + $spec.Path) }
    Ok ('created: ' + $spec.Path)
}
$tunnelPub = (Get-Content -LiteralPath ($sshKey + '.pub') -Raw).Trim()
$syncPub = (Get-Content -LiteralPath ($syncKey + '.pub') -Raw).Trim()

if (-not $SkipKeys) {
    Step ('Installing public keys on ' + $target)
    $pw = $env:WL_LINUX_PASSWORD
    if (-not $pw -and -not $NonInteractive) {
        $sec = Read-Host -AsSecureString ('Linux password for ' + $target + ' (one-time; not stored)')
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        $pw = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if (-not $pw) { throw 'no password supplied (set WL_LINUX_PASSWORD or run interactively)' }
    if (-not $plink) { throw 'plink.exe is required for the password step (see docs/USAGE.md)' }

    if (-not $HostKey) {
        $HostKey = Get-WbHostKeyFingerprint -Target $target -Port $sshPort -Plink $plink
        if ($HostKey) { Ok ('host key discovered: ' + $HostKey) }
        else { Warn 'host key already trusted (or not readable); continuing without pinning' }
    }

    $listen = '127.0.0.1:{0}' -f $conf['WL_TUNNEL_PORT']
    # Literal bash script with placeholders; avoids all PowerShell quoting traps.
    $template = @'
set -eu
umask 077
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
add_key() {
  line="$1"
  body="$(printf '%s' "$line" | awk '{print $3}')"
  if grep -qF -- "$body" "$HOME/.ssh/authorized_keys" 2>/dev/null; then
    echo "exists  $(printf '%s' "$line" | awk '{print $NF}')"
  else
    printf '%s\n' "$line" >> "$HOME/.ssh/authorized_keys"
    echo "added   $(printf '%s' "$line" | awk '{print $NF}')"
  fi
}
add_key 'restrict,port-forwarding,permitlisten="__LISTEN__" __TUNNEL_PUB__'
add_key 'no-pty,no-port-forwarding,no-agent-forwarding,no-x11-forwarding __SYNC_PUB__'
echo done
'@
    $script = $template.Replace('__LISTEN__', $listen).Replace('__TUNNEL_PUB__', $tunnelPub).Replace('__SYNC_PUB__', $syncPub)
    # The here-string carries CRLF from this .ps1 file; bash on Linux dies on it.
    $script = $script.Replace("`r`n", "`n").Replace("`r", "`n")
    $plinkArgs = @('-ssh', '-batch', '-pw', $pw,
                   '-hostkey', $HostKey,
                   '-no-antispoof', $target, 'bash -s')
    $tmp = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($tmp, $script, (New-Object Text.UTF8Encoding($false)))
        $res = Get-Content -LiteralPath $tmp -Raw | & $plink @plinkArgs 2>&1
        $rc = $LASTEXITCODE
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        Remove-Variable pw -ErrorAction SilentlyContinue
    }
    if ($rc -ne 0) {
        throw ('key install failed (rc=' + $rc + '): ' + (($res | Out-String).Trim()) +
               '  -> if the host key changed, rerun with -HostKey <new fingerprint>')
    }
    ($res | Out-String) -split "`r?`n" | Where-Object { $_ } | ForEach-Object { Ok $_.Trim() }

    $test = & $ssh @((Get-WbSshCommonArgs -Conf $conf) + @($target, 'echo SSHKEY-OK')) 2>&1
    if ((($test | Out-String) -notmatch 'SSHKEY-OK')) {
        throw 'key based login test failed: ' + (($test | Out-String).Trim())
    }
    Ok 'key based login verified'
}

if (-not $SkipUpload) {
    Step ('Uploading the delivery folder to ' + $remoteRoot)
    $remoteCmd = "umask 077; mkdir -p '$remoteRoot/config' '$remoteRoot/shared/from_win' '$remoteRoot/shared/to_win' '$remoteRoot/shared/shared' '$remoteRoot/logs'"
    $r = & $ssh @((Get-WbSshCommonArgs -Conf $conf) + @($target, $remoteCmd)) 2>&1
    if ($LASTEXITCODE -ne 0) { throw ('cannot prepare remote root: ' + (($r | Out-String).Trim())) }

    $root = Split-Path -Parent $PSScriptRoot
    # Never ship byte-code caches: they are host specific and useless on Linux.
    Get-ChildItem -LiteralPath $root -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    foreach ($item in @('win', 'linux', 'tools', 'docs', 'shared')) {
        $src = Join-Path $root $item
        if (-not (Test-Path -LiteralPath $src)) {
            Warn ('本地缺少 ' + $item + ' 目录，跳过上传（交付目录应包含 win/linux/tools/docs/shared）')
            continue
        }
        $scpArgs = @('-r', '-p') + (Get-WbScpCommonArgs -Conf $conf) + @($src, ($target + ':' + $remoteRoot + '/'))
        & $scp @scpArgs | Out-Null
        if ($LASTEXITCODE -ne 0) { Warn ('upload failed: ' + $item) } else { Ok ('uploaded ' + $item) }
    }
    foreach ($item in @('README.md', 'VERSION')) {
        $src = Join-Path $root $item
        if (-not (Test-Path -LiteralPath $src)) { continue }
        $scpArgs = @('-p') + (Get-WbScpCommonArgs -Conf $conf) + @($src, ($target + ':' + $remoteRoot + '/'))
        & $scp @scpArgs | Out-Null
        if ($LASTEXITCODE -ne 0) { Warn ('upload failed: ' + $item) }
    }
    $remoteInstall = "tr -d '\r' < " + $remoteRoot + '/linux/install-linux.sh > ' + $remoteRoot + '/linux/install-linux.sh.tmp' +
                     ' && mv ' + $remoteRoot + '/linux/install-linux.sh.tmp ' + $remoteRoot + '/linux/install-linux.sh' +
                     ' && chmod +x ' + $remoteRoot + '/linux/install-linux.sh' +
                     ' && bash ' + $remoteRoot + '/linux/install-linux.sh'
    $r = Invoke-WbRemote -Conf $conf -Command $remoteInstall
    Ok ('install-linux.sh: ' + $r.out)
}

Step 'Sharing the agent token'
$tokPath = Join-Path (Get-WbAppDir) 'agent.token'
$token = ''
if ((-not $RotateToken) -and (Test-Path -LiteralPath $tokPath)) {
    $token = ([IO.File]::ReadAllText($tokPath)).Trim()
}
if (-not $token) {
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $token = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
}
$null = Set-WbTokenFile -Token $token
[Environment]::SetEnvironmentVariable('WL_AGENT_TOKEN', $token, 'User')
$env:WL_AGENT_TOKEN = $token
Ok ('token file: ' + $tokPath + ' (user ACL only)')
Ok 'user environment variable WL_AGENT_TOKEN set'

$envText = "WL_AGENT_TOKEN='$token'`nWL_AGENT_PORT='$($conf['WL_TUNNEL_PORT'])'`nWL_AGENT_LOCAL_PORT='$($conf['WL_AGENT_PORT'])'`n"
# scp the exact bytes: piping text to a native command rewrites LF as CRLF, which
# makes bash source a broken bridge.env.
$envTmp = [IO.Path]::GetTempFileName()
try {
    [IO.File]::WriteAllText($envTmp, $envText.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
    $scpArgs = @('-p') + (Get-WbScpCommonArgs -Conf $conf) + @($envTmp, ($target + ':' + $remoteRoot + '/config/bridge.env'))
    & $scp @scpArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'failed to upload the Linux side env file' }
} finally {
    Remove-Item -LiteralPath $envTmp -Force -ErrorAction SilentlyContinue
}
$fixEnv = "tr -d '\r' < '$remoteRoot/config/bridge.env' > '$remoteRoot/config/bridge.env.tmp' && mv '$remoteRoot/config/bridge.env.tmp' '$remoteRoot/config/bridge.env'; chmod 600 '$remoteRoot/config/bridge.env'"
$null = Invoke-WbRemote -Conf $conf -Command $fixEnv
Ok 'Linux config/bridge.env written (0600, LF)'

Step 'Saving local settings'
$confPath = Join-Path (Get-WbAppDir) 'bridge.conf'
Save-WbConf -Conf $conf -Path $confPath
Ok $confPath
foreach ($d in @($localRoot, (Join-Path $localRoot 'shared\from_win'),
                 (Join-Path $localRoot 'shared\to_win'), (Join-Path $localRoot 'shared\shared'))) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

Step 'Registering autostart (current user only)'
$startupDir = [Environment]::GetFolderPath('Startup')
$startupCmd = Join-Path $startupDir 'WinLinuxBridge.cmd'

# Writes the Startup entry for a supervisor script that is known to exist.
function Register-WbAutostart {
    param([string]$SupervisorPath)
    # One physical line, and the path is scrubbed of any stray CR/LF first:
    # a line break inside the quotes silently breaks autostart.
    $clean = ($SupervisorPath -replace "[\r\n]", "").Trim().Trim('"')
    $line = 'start "" /min powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $clean + '"'
    $body = '@echo off' + "`r`n" + 'rem WinLinuxBridge autostart (created by install-win.ps1)' + "`r`n" + $line + "`r`n"
    [IO.File]::WriteAllText($startupCmd, $body, (New-Object Text.ASCIIEncoding))
    return $startupCmd
}

$supervisor = Join-Path $localRoot 'win\winbridge_supervisor.ps1'
if (-not (Test-Path -LiteralPath $supervisor)) {
    # Autostart must never point at a file that does not exist: that failure is
    # completely silent and survives every reboot.
    Warn ('mirror is not ready yet: ' + $supervisor)
    $supervisor = Join-Path $PSScriptRoot 'winbridge_supervisor.ps1'
    Warn ('autostart will use ' + $supervisor + ' until the mirror is complete')
}
$null = Register-WbAutostart -SupervisorPath $supervisor
Ok $startupCmd

$watchdogCmd = Join-Path $localRoot 'win\start-win.cmd'
if (-not (Test-Path -LiteralPath $watchdogCmd)) { $watchdogCmd = Join-Path $PSScriptRoot 'start-win.cmd' }
if (Register-WbWatchdog -StartCmdPath $watchdogCmd) { Ok 'scheduled task WinLinuxBridge-Watchdog (5 min)' }
else { Warn 'scheduled task not created (not fatal; Startup entry still autostarts the bridge)' }

if (-not $NoStart) {
    Step 'Stopping any running instance'
    $old = @()
    $old += @(Get-WbProcess -Name 'powershell.exe' -Match @('winbridge_supervisor.ps1'))
    $old += @(Get-WbProcess -Name 'pwsh.exe' -Match @('winbridge_supervisor.ps1'))
    $old += @(Get-WbProcess -Name 'ssh.exe' -Match @(
        ('127.0.0.1:{0}:127.0.0.1:{1}' -f $conf['WL_TUNNEL_PORT'], $conf['WL_AGENT_PORT'])))
    $old += @(Get-WbProcessByCommandLine -Text 'winbridge_agent.py')
    if ($old.Count -gt 0) {
        Stop-WbProcess $old
        Start-Sleep -Seconds 1
        Ok ('stopped {0} old process(es)' -f $old.Count)
    } else {
        Ok 'nothing was running'
    }

    Step 'Refreshing the local mirror and starting the bridge'
    $sync = Join-Path $PSScriptRoot 'winbridge_sync.ps1'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $sync -Mode program | Out-Null

    # The mirror must contain the supervisor, otherwise autostart and start-win
    # point at nothing. Fall back to the folder this installer runs from.
    $supervisor = Join-Path $localRoot 'win\winbridge_supervisor.ps1'
    $startCmd = Join-Path $localRoot 'win\start-win.cmd'
    if (-not (Test-Path -LiteralPath $supervisor)) {
        Warn ('mirror is incomplete: ' + $supervisor + ' is missing')
        Warn ('using ' + $PSScriptRoot + ' instead; copy the delivery folder to ' + $localRoot)
        $supervisor = Join-Path $PSScriptRoot 'winbridge_supervisor.ps1'
        $startCmd = Join-Path $PSScriptRoot 'start-win.cmd'
    }
    if (-not (Test-Path -LiteralPath $startCmd)) { throw ('cannot start: ' + $startCmd + ' is missing') }

    # The mirror is complete now: point autostart at the mirror copy if it was
    # registered against the installer folder earlier.
    if ($supervisor -ne (Join-Path $localRoot 'win\winbridge_supervisor.ps1') -and
        (Test-Path -LiteralPath (Join-Path $localRoot 'win\winbridge_supervisor.ps1'))) {
        $null = Register-WbAutostart -SupervisorPath (Join-Path $localRoot 'win\winbridge_supervisor.ps1')
        Ok ('autostart repointed to ' + (Join-Path $localRoot 'win\winbridge_supervisor.ps1'))
    }
    if ($watchdogCmd -ne $startCmd -and (Test-Path -LiteralPath $startCmd)) {
        if (Register-WbWatchdog -StartCmdPath $startCmd) { Ok 'watchdog repointed to the mirror' }
    }

    Write-WbLog 'install: starting supervisor' 'install'
    Start-Process -FilePath $startCmd -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 8

    $okAgent = Test-WbAgent -Conf $conf
    if ($okAgent) { Ok ('agent answers on 127.0.0.1:' + $conf['WL_AGENT_PORT']) } else { Warn 'agent not answering yet (see ~\.winlinuxbridge\logs)' }
    $r = Invoke-WbRemote -Conf $conf -Command ('{0}/linux/wl ping' -f $remoteRoot) -TimeoutSec 30
    if ($r.rc -eq 0) { Ok 'end-to-end: Linux -> tunnel -> agent works' } else { Warn ('end-to-end not ready yet: ' + $r.out) }
}

Step 'Done'
Write-Host '   On Linux:  ~/WinLinuxBridge/linux/wl ping'
Write-Host '   Full guide: docs/USAGE.md'
