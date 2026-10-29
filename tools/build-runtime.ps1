# WinLinuxBridge - build the self-contained Python runtime shipped in win\runtime.
#
# The end user must not need Python, pip or PuTTY installed. This script produces
# a portable interpreter that already has working tkinter, using two public
# sources and nothing else:
#
#   * python.org   - the "embeddable" package (interpreter + stdlib + ctypes)
#   * a full CPython install - its tcl/tk runtime and the _tkinter extension
#     module, which the embeddable package deliberately omits
#
# The second source defaults to whatever CPython 3.13 is already on this machine
# (the Microsoft Store build works). Any official install works too; pass
# -TkinterFrom "C:\Python313" when it is somewhere unusual.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1
#   powershell ... -File tools\build-runtime.ps1 -ProductVersion 3.13.14
#   powershell ... -File tools\build-runtime.ps1 -TkinterFrom 'C:\Python313'
#
# ASCII comments only.

param(
    [string]$ProductVersion = '3.13.14',
    [string]$TkinterFrom = '',
    [string]$OutDir = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root 'win\runtime' }
$cache = Join-Path $env:USERPROFILE '.winlinuxbridge\cache'
if (-not (Test-Path -LiteralPath $cache)) { New-Item -ItemType Directory -Path $cache -Force | Out-Null }

function Say  { param([string]$T) Write-Host ('[build] ' + $T) }
function Fail { param([string]$T) Write-Host ('[build] ERROR ' + $T) -ForegroundColor Red; exit 1 }

$majorMinor = ($ProductVersion -split '\.')[0..1] -join ''
Say ("target runtime: CPython $ProductVersion into $OutDir")

if ((Test-Path -LiteralPath (Join-Path $OutDir 'python.exe')) -and -not $Force) {
    Say 'runtime already present (use -Force to rebuild)'
    exit 0
}

# ---- 1. the full install that donates tcl/tk ------------------------------- #
if (-not $TkinterFrom) {
    $probe = @(
        @{ Exe = 'python';  Args = @() },
        @{ Exe = 'python3'; Args = @() },
        @{ Exe = 'py';      Args = @('-3') }
    )
    foreach ($p in $probe) {
        if (-not (Get-Command $p.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $p.Exe @($p.Args + @('-c', 'import sys,os,tkinter,os.path;print(sys.base_prefix);print(os.path.dirname(os.path.dirname(tkinter.__file__)))')) 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $TkinterFrom = ($out | Select-Object -First 1).Trim()
                $tkinterDir = ($out | Select-Object -Last 1).Trim()
                Say ("tcl/tk source   : $TkinterFrom")
                Say ("tkinter package : $tkinterDir")
                break
            }
        } catch { }
    }
}
if (-not $TkinterFrom) {
    Fail 'no CPython with tkinter found. Install Python 3.13 (python.org or the Microsoft Store) and rerun, or pass -TkinterFrom <prefix>.'
}
if (-not $tkinterDir) { $tkinterDir = Join-Path $TkinterFrom 'Lib' }

foreach ($rel in @('DLLs\_tkinter.pyd', 'DLLs\tcl86t.dll', 'DLLs\tk86t.dll', 'DLLs\zlib1.dll')) {
    $p = Join-Path $TkinterFrom $rel
    if (-not (Test-Path -LiteralPath $p)) { Fail ("missing dependency: $p") }
}
$tclLib = Join-Path $TkinterFrom 'tcl\tcl8.6'
$tkLib = Join-Path $TkinterFrom 'tcl\tk8.6'
if (-not (Test-Path -LiteralPath $tclLib)) { Fail ("missing Tcl script library: $tclLib") }
if (-not (Test-Path -LiteralPath $tkLib)) { Fail ("missing Tk script library: $tkLib") }

# ---- 2. the embeddable interpreter ---------------------------------------- #
$zipName = ('python-{0}-embed-amd64.zip' -f $ProductVersion)
$zipPath = Join-Path $cache $zipName
if (-not (Test-Path -LiteralPath $zipPath)) {
    $url = ('https://www.python.org/ftp/python/{0}/{1}' -f $ProductVersion, $zipName)
    Say ("downloading $url")
    try {
        Invoke-WebRequest -Uri $url -OutFile $zipPath -TimeoutSec 300 -UseBasicParsing
    } catch {
        Fail ('download failed: ' + $_.Exception.Message + '  (check the version, or drop the zip into ' + $cache + ')')
    }
}
Say ("embeddable zip  : $zipPath ({0:N1} MB)" -f ((Get-Item $zipPath).Length / 1MB))

if (Test-Path -LiteralPath $OutDir) { Remove-Item -LiteralPath $OutDir -Recurse -Force }
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
Expand-Archive -Path $zipPath -DestinationPath $OutDir -Force
Say 'extracted the embeddable package'

# ---- 3. graft tkinter onto it --------------------------------------------- #
foreach ($f in @('_tkinter.pyd', 'tcl86t.dll', 'tk86t.dll', 'zlib1.dll')) {
    Copy-Item -LiteralPath (Join-Path $TkinterFrom ('DLLs\' + $f)) -Destination $OutDir -Force
}
New-Item -ItemType Directory -Path (Join-Path $OutDir 'tcl') -Force | Out-Null
Copy-Item -LiteralPath $tclLib -Destination (Join-Path $OutDir 'tcl\tcl8.6') -Recurse -Force
Copy-Item -LiteralPath $tkLib -Destination (Join-Path $OutDir 'tcl\tk8.6') -Recurse -Force
New-Item -ItemType Directory -Path (Join-Path $OutDir 'lib') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $tkinterDir 'tkinter') -Destination (Join-Path $OutDir 'lib\tkinter') -Recurse -Force
Say 'grafted tkinter (extension module, tcl/tk libraries, python package)'

# Keep the stdlib inside its zip: faster start and a much smaller folder.
$pth = Get-ChildItem -LiteralPath $OutDir -Filter '*._pth' | Select-Object -First 1
if (-not $pth) { Fail 'no ._pth file in the embeddable package' }
$zipInner = (Get-ChildItem -LiteralPath $OutDir -Filter 'python*.zip' | Select-Object -First 1).Name
$pthLines = @($zipInner, '.', 'lib', '# import site is intentionally left off: no pip, no surprises')
[IO.File]::WriteAllLines($pth.FullName, $pthLines, (New-Object Text.ASCIIEncoding))
Say ("wrote {0} -> {1}" -f $pth.Name, ($pthLines -join ' '))

# ---- 4. prove it works ---------------------------------------------------- #
$exe = Join-Path $OutDir 'python.exe'
$test = @(
    'import sys, os',
    'root = os.path.dirname(sys.executable)',
    'os.add_dll_directory(root)',
    'os.environ.setdefault("TCL_LIBRARY", os.path.join(root, "tcl", "tcl8.6"))',
    'os.environ.setdefault("TK_LIBRARY", os.path.join(root, "tcl", "tk8.6"))',
    'import tkinter',
    'r = tkinter.Tk(); r.withdraw()',
    'info = r.tk.call("info", "patchlevel")',
    'r.destroy()',
    'import ctypes, json, urllib.request, http.server',
    'print("TK=" + str(tkinter.TkVersion) + " PATCH=" + info + " PY=" + sys.version.split()[0])'
) -join "`n"
$testFile = Join-Path $env:TEMP ('wl_rt_test_' + [guid]::NewGuid().ToString('N') + '.py')
[IO.File]::WriteAllText($testFile, $test, (New-Object Text.UTF8Encoding($false)))
try {
    $result = & $exe $testFile 2>&1
    if ($LASTEXITCODE -ne 0) { Fail ('runtime self test failed: ' + (($result | Out-String).Trim())) }
    Say ('self test       : ' + (($result | Out-String).Trim()))
} finally {
    Remove-Item -LiteralPath $testFile -Force -ErrorAction SilentlyContinue
}

Copy-Item -LiteralPath (Join-Path $TkinterFrom 'LICENSE.txt') -Destination (Join-Path $OutDir 'LICENSE-Python.txt') -Force

$size = (Get-ChildItem -LiteralPath $OutDir -Recurse -File | Measure-Object -Sum Length).Sum
Say ('runtime ready   : {0:N1} MB in {1}' -f ($size / 1MB), $OutDir)
