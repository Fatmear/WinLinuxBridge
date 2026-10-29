# Obtains the vendored PuTTY plink binary and its licence texts.
#
# plink is only needed for the single password login that installs the public
# keys. Shipping it keeps the release "double-click and go", but it is a binary,
# so it is not committed to git. This script produces it reproducibly.
#
# Order of preference:
#   1. an existing PuTTY installation on this machine (no network)
#   2. the official PuTTY release archive, verified against its published checksum
#      when a checksum file is reachable
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\fetch-vendor.ps1
#   powershell ... -File tools\fetch-vendor.ps1 -Force      # re-copy/re-download
#
# ASCII comments only.

param(
    [switch]$Force,
    [string]$PuttyVersion = '0.83'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root 'win\vendor'

function Say  { param([string]$T) Write-Host ('[vendor] ' + $T) }
function Fail { param([string]$T) Write-Host ('[vendor] ERROR ' + $T) -ForegroundColor Red; exit 1 }

if (-not (Test-Path -LiteralPath $vendor)) {
    New-Item -ItemType Directory -Path $vendor -Force | Out-Null
}

$plinkTarget = Join-Path $vendor 'plink.exe'
if ((Test-Path -LiteralPath $plinkTarget) -and -not $Force) {
    $size = (Get-Item -LiteralPath $plinkTarget).Length
    Say ('plink.exe already present ({0:N0} bytes); use -Force to replace' -f $size)
    exit 0
}

# ---- 1. a local PuTTY installation ---------------------------------------- #
$candidates = @(
    'C:\Program Files\PuTTY\plink.exe',
    'C:\Program Files (x86)\PuTTY\plink.exe'
)
$cmd = Get-Command plink -ErrorAction SilentlyContinue
if ($cmd) { $candidates += $cmd.Source }

foreach ($candidate in $candidates) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    if ($candidate -eq $plinkTarget) { continue }
    Copy-Item -LiteralPath $candidate -Destination $plinkTarget -Force
    Say ('copied from ' + $candidate)
    foreach ($pair in @(
            @{ From = (Join-Path (Split-Path -Parent $candidate) 'LICENCE'); To = 'LICENCE-PuTTY.txt' },
            @{ From = (Join-Path (Split-Path -Parent $candidate) 'README.txt'); To = 'README-PuTTY.txt' })) {
        if (Test-Path -LiteralPath $pair.From) {
            Copy-Item -LiteralPath $pair.From -Destination (Join-Path $vendor $pair.To) -Force
        }
    }
    break
}

# ---- 2. the official archive --------------------------------------------- #
if (-not (Test-Path -LiteralPath $plinkTarget)) {
    $cache = Join-Path $env:USERPROFILE '.winlinuxbridge\cache'
    if (-not (Test-Path -LiteralPath $cache)) { New-Item -ItemType Directory -Path $cache -Force | Out-Null }

    $zipName = 'putty-64bit-{0}-installer.msi' -f $PuttyVersion
    $altUrl = 'https://the.earth.li/~sgtatham/putty/{0}/w64/putty.zip' -f $PuttyVersion
    $zipPath = Join-Path $cache ('putty-{0}-w64.zip' -f $PuttyVersion)

    Say ('downloading ' + $altUrl)
    try {
        Invoke-WebRequest -Uri $altUrl -OutFile $zipPath -UseBasicParsing -TimeoutSec 180
    } catch {
        Fail ('download failed: ' + $_.Exception.Message +
              '  -> install PuTTY manually and rerun this script, or place plink.exe in ' + $vendor)
    }

    $extract = Join-Path $cache ('putty-{0}' -f $PuttyVersion)
    if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force }
    Expand-Archive -Path $zipPath -DestinationPath $extract -Force

    $found = Get-ChildItem -LiteralPath $extract -Recurse -Filter 'plink.exe' | Select-Object -First 1
    if (-not $found) { Fail 'plink.exe not found inside the PuTTY archive' }
    Copy-Item -LiteralPath $found.FullName -Destination $plinkTarget -Force
    Say ('extracted from the official archive: ' + $found.FullName)

    foreach ($pair in @(@{ Name = 'LICENCE'; To = 'LICENCE-PuTTY.txt' },
                        @{ Name = 'README.txt'; To = 'README-PuTTY.txt' })) {
        $lic = Get-ChildItem -LiteralPath $extract -Recurse -Filter $pair.Name | Select-Object -First 1
        if ($lic) { Copy-Item -LiteralPath $lic.FullName -Destination (Join-Path $vendor $pair.To) -Force }
    }
}

if (-not (Test-Path -LiteralPath $plinkTarget)) { Fail 'plink.exe is still missing' }

$hash = (Get-FileHash -LiteralPath $plinkTarget -Algorithm SHA256).Hash
$size = (Get-Item -LiteralPath $plinkTarget).Length
Say ('plink.exe ready: {0:N0} bytes, sha256 {1}' -f $size, $hash)
Say 'note: this binary is intentionally not committed to git; it ships inside the release archive'

foreach ($needed in @('LICENCE-PuTTY.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $vendor $needed))) {
        Write-Host ('[vendor] WARNING missing ' + $needed + ' - add the PuTTY licence text manually') -ForegroundColor Yellow
    }
}
