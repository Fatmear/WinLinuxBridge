# One-way / merged folder synchronisation between the Linux delivery folder and
# the Windows mirror, using only scp.exe (part of Windows OpenSSH - no admin,
# no extra binary). ASCII comments only.
#
# Layout (identical on both sides, so every path in the docs matches):
#   <root>/win        Windows programs        Linux -> Windows (read only)
#   <root>/linux      Linux CLI               Linux -> Windows (read only)
#   <root>/tools      verification scripts    Linux -> Windows (read only)
#   <root>/docs       documentation           Linux -> Windows (read only)
#   <root>/shared/from_win    Windows writes, Linux reads
#   <root>/shared/to_win      Linux writes,   Windows reads
#   <root>/shared/shared      both write, newest-wins merge
#
# Usage:
#   powershell -File winbridge_sync.ps1 -Mode program
#   powershell -File winbridge_sync.ps1 -Mode fromwin|towin|shared|lanes|all

param(
    [ValidateSet('program', 'fromwin', 'towin', 'shared', 'lanes', 'all')]
    [string]$Mode = 'lanes'
)

. (Join-Path $PSScriptRoot 'winbridge_common.ps1')

$conf = Get-WbConf -WinDir $PSScriptRoot
$local = $conf['WL_LOCAL_ROOT']
$remote = $conf['WL_REMOTE_ROOT']
$target = Get-WbTarget -Conf $conf
$scp = Get-WbScpExe
$scpArgs0 = Get-WbScpCommonArgs -Conf $conf

if (-not $scp) {
    Write-WbLog 'sync: scp.exe not found, sync disabled' 'sync'
    exit 2
}
if (-not (Test-Path -LiteralPath $conf['WL_SYNC_KEY'])) {
    Write-WbLog ('sync: sync key missing: ' + $conf['WL_SYNC_KEY']) 'sync'
    exit 2
}

function New-WbDir {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

# scp -r copies the source directory INTO an existing destination directory.
function Invoke-Scp {
    param([string]$Source, [string]$Dest, [string]$Label)
    $scpArgs = @('-r', '-p') + $scpArgs0 + @($Source, $Dest)
    $out = & $scp @scpArgs 2>&1
    $rc = $LASTEXITCODE
    if ($rc -ne 0) {
        Write-WbLog ('sync/{0}: rc={1} {2}' -f $Label, $rc, (($out | Out-String).Trim())) 'sync'
    }
    return $rc
}

# Copies one file with scp (no -r), used by the manifest refresh below.
# scp does NOT create local directories: if the parent is missing it fails with
# 'open local "...": No such file or directory' and the mirror silently stays
# incomplete. Always create the parent first.
function Invoke-ScpFile {
    param([string]$Source, [string]$Dest, [string]$Label)
    $parent = Split-Path -Parent $Dest
    if ($parent) { New-WbDir $parent }
    $scpArgs = @('-p') + $scpArgs0 + @($Source, $Dest)
    $out = & $scp @scpArgs 2>&1
    $rc = $LASTEXITCODE
    if ($rc -ne 0) { Write-WbLog ('sync/{0}: rc={1} {2}' -f $Label, $rc, (($out | Out-String).Trim())) 'sync' }
    return $rc
}

function Merge-TreeNewer {
    param([string]$Source, [string]$Target)
    $copied = 0
    $skipped = 0
    if (-not (Test-Path -LiteralPath $Source)) { return @{ copied = 0; skipped = 0 } }
    New-WbDir $Target
    Get-ChildItem -LiteralPath $Source -Recurse -File -Force | ForEach-Object {
        $rel = $_.FullName.Substring($Source.Length).TrimStart('\', '/')
        $dst = Join-Path $Target $rel
        $take = $true
        if (Test-Path -LiteralPath $dst) {
            $dstTime = (Get-Item -LiteralPath $dst -Force).LastWriteTimeUtc
            if ($dstTime -ge $_.LastWriteTimeUtc) { $take = $false }
        }
        if ($take) {
            $d = Split-Path -Parent $dst
            New-WbDir $d
            Copy-Item -LiteralPath $_.FullName -Destination $dst -Force
            $copied++
        } else {
            $skipped++
        }
    }
    return @{ copied = $copied; skipped = $skipped }
}

function Sync-Program {
    # Manifest refresh: only the small text files the bridge actually runs.
    # runtime\ (30 MB) and vendor\ are delivered once by install-win.ps1 and
    # never re-pulled, so the periodic refresh stays a few hundred KB.
    New-WbDir $local
    $rc = 0

    # A brand new local root (changed in the GUI, or a fresh machine) has no
    # win/linux/tools/docs yet. Pull those trees once so the mirror is complete
    # and the supervisor script actually exists locally.
    foreach ($item in @('win', 'linux', 'tools', 'docs')) {
        $dir = Join-Path $local $item
        if (Test-Path -LiteralPath $dir) { continue }
        Write-WbLog ('sync/program: bootstrapping missing tree ' + $item) 'sync'
        $r = Invoke-Scp -Source ($target + ':' + $remote + '/' + $item) -Dest $local -Label ('bootstrap/' + $item)
        if ($r -ne 0) { $rc = $r }
    }

    $files = @(
        'VERSION', 'README.md',
        'win\WinBridgeGUI.pyw', 'win\start-gui.cmd', 'win\start-win.cmd',
        'win\winbridge_agent.py', 'win\winbridge_jobs.py', 'win\winbridge_common.ps1',
        'win\winbridge_supervisor.ps1', 'win\winbridge_sync.ps1',
        'win\install-win.ps1', 'win\bridge.conf.example',
        'tools\verify.ps1', 'tools\verify.sh', 'tools\build-runtime.ps1',
        'docs\USAGE.md', 'docs\AI-AGENT.md',
        'linux\wl', 'linux\wl_job.py', 'linux\install-linux.sh'
    )
    foreach ($rel in $files) {
        $remotePath = ($remote + '/' + ($rel -replace '\\', '/'))
        $r = Invoke-ScpFile -Source ($target + ':' + $remotePath) -Dest (Join-Path $local $rel) -Label ('program/' + $rel)
        if ($r -ne 0) { $rc = $r }
    }
    Write-WbLog ('sync/program: {0} files, rc={1}' -f $files.Count, $rc) 'sync'
    return $rc
}

function Sync-FromWin {
    New-WbDir (Join-Path $local 'shared')
    $src = Join-Path $local 'shared\from_win'
    if (-not (Test-Path -LiteralPath $src)) { New-WbDir $src }
    $rc = Invoke-Scp -Source $src -Dest ($target + ':' + $remote + '/shared/') -Label 'fromwin'
    Write-WbLog ('sync/fromwin: rc=' + $rc) 'sync'
    return $rc
}

function Sync-ToWin {
    New-WbDir (Join-Path $local 'shared')
    $rc = Invoke-Scp -Source ($target + ':' + $remote + '/shared/to_win') -Dest (Join-Path $local 'shared') -Label 'towin'
    Write-WbLog ('sync/towin: rc=' + $rc) 'sync'
    return $rc
}

# shared/: download, merge newest-wins into the mirror, then push the merged
# tree back. Deletions are not propagated (documented behaviour).
function Sync-Shared {
    $stage = Join-Path (Get-WbAppDir) 'incoming'
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    New-WbDir $stage
    $rc = Invoke-Scp -Source ($target + ':' + $remote + '/shared/shared') -Dest $stage -Label 'shared-pull'
    $remoteTree = Join-Path $stage 'shared'
    $localTree = Join-Path $local 'shared\shared'
    $merged = @{ copied = 0; skipped = 0 }
    if (Test-Path -LiteralPath $remoteTree) {
        $merged = Merge-TreeNewer -Source $remoteTree -Target $localTree
    }
    New-WbDir $localTree
    $rc2 = Invoke-Scp -Source $localTree -Dest ($target + ':' + $remote + '/shared/') -Label 'shared-push'
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Write-WbLog ('sync/shared: pulled={0} kept={1} pull_rc={2} push_rc={3}' -f $merged.copied, $merged.skipped, $rc, $rc2) 'sync'
    return ($rc + $rc2)
}

switch ($Mode) {
    'program' { $null = Sync-Program }
    'fromwin' { $null = Sync-FromWin }
    'towin'   { $null = Sync-ToWin }
    'shared'  { $null = Sync-Shared }
    'lanes'   { $null = Sync-FromWin; $null = Sync-ToWin; $null = Sync-Shared }
    'all'     { $null = Sync-Program; $null = Sync-FromWin; $null = Sync-ToWin; $null = Sync-Shared }
}
exit 0
