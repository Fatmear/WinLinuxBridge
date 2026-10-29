# WinLinuxBridge supervisor (Windows side).
#
# Responsibilities, in a single always-on loop (no admin, no service):
#   1. keep the control agent alive on 127.0.0.1:<WL_AGENT_PORT>
#   2. keep the reverse SSH tunnel alive so Linux can reach that agent
#   3. refresh the local mirror of the Linux delivery folder and run the
#      shared-folder lanes
#
# Start it with start-win.cmd (hidden window) or from the Startup folder.
# ASCII comments only.

param(
    [switch]$Once,
    [int]$TickSeconds = 10
)

. (Join-Path $PSScriptRoot 'winbridge_common.ps1')

$conf = Get-WbConf -WinDir $PSScriptRoot
$agentPort = [int]$conf['WL_AGENT_PORT']
$tunnelPort = [int]$conf['WL_TUNNEL_PORT']
$target = Get-WbTarget -Conf $conf
$localRoot = $conf['WL_LOCAL_ROOT']
$remoteRoot = $conf['WL_REMOTE_ROOT']
$syncKey = $conf['WL_SYNC_KEY']
$sshKey = $conf['WL_SSH_KEY']
$knownHosts = Join-Path $env:USERPROFILE '.ssh\known_hosts'

$appDir = Get-WbAppDir
foreach ($d in @($appDir, (Join-Path $appDir 'logs'), $localRoot,
                 (Join-Path $localRoot 'shared\from_win'),
                 (Join-Path $localRoot 'shared\to_win'),
                 (Join-Path $localRoot 'shared\shared'))) {
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# Single instance guard.
$mutex = New-Object Threading.Mutex($false, 'Local\WinLinuxBridgeSupervisor')
try {
    $owned = $mutex.WaitOne(0)
} catch {
    $owned = $false
}
if (-not $owned) {
    Write-WbLog 'another supervisor instance is already running; exiting'
    exit 0
}

$py = Get-WbPython
$ssh = Get-WbSshExe

Write-WbLog ('supervisor start: agent=127.0.0.1:{0} tunnel=127.0.0.1:{1} target={2}' -f $agentPort, $tunnelPort, $target)

# Clean slate: an agent left over from an earlier run may hold the port with an
# old token, which looks like "agent down" forever. Matched by command line so a
# differently named python build is still found.
$stale = @(Get-WbProcessByCommandLine -Text 'winbridge_agent.py')
if ($stale.Count -gt 0) {
    Stop-WbProcess $stale
    Write-WbLog ('agent: stopped {0} stale instance(s)' -f $stale.Count)
    Start-Sleep -Milliseconds 700
}
$lastAgentStart = Get-Date

function Start-Agent {
    if (-not $py) {
        Write-WbLog 'agent: no usable python interpreter found (tried python / py -3 / python3)'
        return
    }
    $agentPy = Join-Path $PSScriptRoot 'winbridge_agent.py'
    if (-not (Test-Path -LiteralPath $agentPy)) {
        Write-WbLog ('agent: missing ' + $agentPy)
        return
    }
    $argStr = (@($py.Args) + @('"' + $agentPy + '"')) -join ' '
    $env:WL_AGENT_PORT = [string]$agentPort
    $env:WL_BRIDGE_DIR = $localRoot
    try {
        Start-Process -FilePath $py.Exe -ArgumentList $argStr -WindowStyle Hidden `
            -RedirectStandardError (Join-Path $appDir 'logs\agent-stderr.log') `
            -RedirectStandardOutput (Join-Path $appDir 'logs\agent-stdout.log') | Out-Null
        Write-WbLog 'agent: started'
        $script:lastAgentStart = Get-Date
    } catch {
        Write-WbLog ('agent: start failed: ' + $_.Exception.Message)
    }
}

function Get-TunnelProcesses {
    return @(Get-WbProcess -Name 'ssh.exe' -Match @(
        ('127.0.0.1:{0}:127.0.0.1:{1}' -f $tunnelPort, $agentPort), $conf['WL_LINUX_HOST']
    ))
}

function Start-Tunnel {
    if (-not $ssh) { Write-WbLog 'tunnel: ssh.exe not found'; return }
    if (-not (Test-Path -LiteralPath $sshKey)) {
        Write-WbLog ('tunnel: key missing: ' + $sshKey + ' - run install-win.ps1')
        return
    }
    $forward = '127.0.0.1:{0}:127.0.0.1:{1}' -f $tunnelPort, $agentPort
    $tunnelArgs = @(
        '-N', '-T',
        '-i', ('"' + $sshKey + '"'),
        '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'ServerAliveInterval=20',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'ConnectTimeout=10',
        '-o', 'IdentitiesOnly=yes',
        '-o', ('UserKnownHostsFile=' + $knownHosts),
        '-R', $forward,
        $target
    )
    try {
        Start-Process -FilePath $ssh -ArgumentList $tunnelArgs -WindowStyle Hidden `
            -RedirectStandardError (Join-Path $appDir 'logs\tunnel.log') | Out-Null
        Write-WbLog ('tunnel: started (-R ' + $forward + ')')
    } catch {
        Write-WbLog ('tunnel: start failed: ' + $_.Exception.Message)
    }
}

function Test-TunnelRemote {
    # Asks the Linux side to call the agent through the forwarded port.
    if (-not (Test-Path -LiteralPath $syncKey)) { return $true }  # cannot verify, do not kill
    $cmd = ('{0}/linux/wl ping >/dev/null 2>&1 && echo OK' -f $remoteRoot)
    $r = Invoke-WbRemote -Conf $conf -Command $cmd -TimeoutSec 25
    return ($r.out -match 'OK')
}

$tick = 0
$failStreak = 0
$syncEvery = [Math]::Max([int]$conf['WL_SYNC_INTERVAL'] / $TickSeconds, 1)
$programEvery = [Math]::Max([int]$conf['WL_PROGRAM_REFRESH'] / $TickSeconds, 1)
$firstProgram = $true

while ($true) {
    try {
        if (-not (Test-WbAgent -Conf $conf)) {
            # Only one path leads to a restart: either nothing is running, or a
            # process holds the port but never answers (stale token) for 30s.
            $procs = @(Get-WbProcessByCommandLine -Text 'winbridge_agent.py')
            $ageSeconds = ((Get-Date) - $lastAgentStart).TotalSeconds
            if ($procs.Count -eq 0) {
                Start-Agent
            } elseif ($ageSeconds -gt 30) {
                Write-WbLog ('agent: unresponsive after {0:N0}s, restarting' -f $ageSeconds)
                Stop-WbProcess $procs
                Start-Sleep -Milliseconds 500
                Start-Agent
            }
        }

        $tunnels = Get-TunnelProcesses
        if ($tunnels.Count -eq 0) {
            Start-Tunnel
        } elseif (($tick % 6) -eq 0) {
            if (Test-TunnelRemote) {
                $failStreak = 0
            } else {
                $failStreak++
                Write-WbLog ('tunnel: remote health check failed ({0}/2)' -f $failStreak)
                if ($failStreak -ge 2) {
                    Write-WbLog 'tunnel: restarting'
                    Stop-WbProcess $tunnels
                    $failStreak = 0
                    Start-Tunnel
                }
            }
        }

        if ($firstProgram -or (($tick % $programEvery) -eq 0 -and $tick -gt 0)) {
            $firstProgram = $false
            & (Join-Path $PSScriptRoot 'winbridge_sync.ps1') -Mode program 6>$null
        }
        if (($tick % $syncEvery) -eq 0) {
            & (Join-Path $PSScriptRoot 'winbridge_sync.ps1') -Mode lanes 6>$null
        }
    } catch {
        Write-WbLog ('supervisor loop error: ' + $_.Exception.Message)
    }

    if ($Once) { break }
    $tick++
    Start-Sleep -Seconds $TickSeconds
}

$mutex.ReleaseMutex()
Write-WbLog 'supervisor exit'
