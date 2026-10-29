r"""Builds the zero-install release archive.

Runs on the bundled portable interpreter (or any CPython 3.9+). Uses zipfile so
entries always use forward slashes, which Compress-Archive on Windows does not
guarantee.

    python tools/package_release.py                # version from VERSION
    python tools/package_release.py --version 1.3.0 --out release

Refuses to build when a shippable text file still contains host identity
(an IPv4 literal, key material) or a secret-shaped value.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Never shipped: machine state, internal notes, build output, VCS metadata.
EXCLUDE_DIRS = {
    "project-context", "git", ".git", ".mypy_cache", "__pycache__",
    "release", ".vscode", "node_modules", ".idea",
}
EXCLUDE_FILES = {
    "agent.token", "credentials.dat", "bridge.conf", "bridge.env",
    "AGENTS.md", "SHA256SUMS.txt", "RELEASE_BODY.md",
}
# Release archives must not nest; the runtime's own stdlib archive must survive.
EXCLUDE_NAME_PATTERNS = ("WinLinuxBridge-*.zip",)
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".tok")

# Paths relative to the archive root that must be present.
REQUIRED = [
    "VERSION", "README.md", "README.en.md", "LICENSE", "CHANGELOG.md",
    "win/start-gui.cmd", "win/WinBridgeGUI.pyw", "win/winbridge_agent.py",
    "win/winbridge_jobs.py", "win/winbridge_common.ps1",
    "win/winbridge_supervisor.ps1", "win/winbridge_sync.ps1",
    "win/install-win.ps1", "win/start-win.cmd",
    "win/runtime/python.exe", "win/runtime/pythonw.exe",
    "win/runtime/python313.zip",
    "win/runtime/_tkinter.pyd", "win/runtime/tcl/tcl8.6/init.tcl",
    "win/runtime/tcl/tk8.6/tk.tcl", "win/vendor/plink.exe",
    "win/vendor/LICENSE-Python.txt", "win/vendor/LICENCE-PuTTY.txt",
    "linux/wl", "linux/wl_job.py", "linux/install-linux.sh",
    "tools/verify.ps1", "tools/verify.sh", "tools/verify_jobs.sh",
    "tools/verify_cancel.sh", "tools/build-runtime.ps1",
    "tools/normalize-lf.py", "tools/package_release.py",
    "docs/USAGE.md", "docs/AI-AGENT.md",
]

# Text files that must never carry identity or secrets.
SCAN_EXTENSIONS = (".md", ".ps1", ".py", ".sh", ".cmd", ".conf", ".example",
                   ".txt", ".json", ".yml", ".yaml", "")

ALLOWED_IPV4 = {"127.0.0.1", "0.0.0.0", "255.255.255.0", "192.0.2.1"}

PATTERNS = [
    ("ipv4", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")),
    ("private-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("password-value", re.compile(r"(?i)pass(?:word|wd)?\s*[=:]\s*['\"]?[^\s'\"<$]{4,}")),
    ("token-value", re.compile(r"(?i)token\s*[=:]\s*['\"]?[0-9a-f]{16,}")),
    ("secret-value", re.compile(r"(?i)(secret|api[_-]?key)\s*[=:]\s*\S{8,}")),
]


def read_version() -> str:
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as fh:
        return fh.read().strip()


def is_text_candidate(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in SCAN_EXTENSIONS


def iter_files(base: str):
    """Yields (absolute path, archive-relative posix path)."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            if name in EXCLUDE_FILES:
                continue
            if name.endswith(EXCLUDE_SUFFIXES):
                continue
            if any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_NAME_PATTERNS):
                continue
            absolute = os.path.join(dirpath, name)
            relative = os.path.relpath(absolute, base).replace(os.sep, "/")
            yield absolute, relative


def scan(base: str) -> list:
    problems = []
    for absolute, relative in iter_files(base):
        if not is_text_candidate(relative):
            continue
        try:
            with open(absolute, "r", encoding="utf-8-sig", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(0).strip()
                if label == "ipv4" and value in ALLOWED_IPV4:
                    continue
                problems.append("%s: [%s] %s" % (relative, label, value[:80]))
    return problems


def normalise_lf(base: str) -> None:
    """Shell/Python clients must be LF; Windows checkouts may carry CRLF."""
    for relative in ("linux/wl", "linux/wl_job.py", "linux/install-linux.sh",
                     "tools/verify.sh", "tools/verify_jobs.sh",
                     "tools/verify_cancel.sh"):
        path = os.path.join(base, relative.replace("/", os.sep))
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8", newline="") as fh:
            raw = fh.read()
        fixed = raw.replace("\r\n", "\n").replace("\r", "\n")
        if fixed != raw:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(fixed)


def main(argv) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="")
    parser.add_argument("--out", default=os.path.join(ROOT, "release"))
    parser.add_argument("--skip-scan", action="store_true")
    args = parser.parse_args(argv)

    version = args.version or read_version()
    print("[release] version %s" % version)

    missing = [p for p in REQUIRED if not os.path.exists(
        os.path.join(ROOT, p.replace("/", os.sep)))]
    if missing:
        print("[release] ERROR missing required files:", file=sys.stderr)
        for item in missing:
            print("   " + item, file=sys.stderr)
        print("   hint: run tools\\build-runtime.ps1 to produce win\\runtime",
              file=sys.stderr)
        return 2

    if not args.skip_scan:
        print("[release] scanning for host identity and credentials")
        problems = scan(ROOT)
        if problems:
            for item in problems[:25]:
                print("   " + item, file=sys.stderr)
            print("[release] ERROR refusing to package: %d occurrence(s)"
                  % len(problems), file=sys.stderr)
            return 3
        print("[release] identity scan clean")

    os.makedirs(args.out, exist_ok=True)
    zip_name = "WinLinuxBridge-%s.zip" % version
    zip_path = os.path.join(args.out, zip_name)
    if os.path.exists(zip_path):
        os.remove(zip_path)

    staging = tempfile.mkdtemp(prefix="wlb-release-")
    stage_root = os.path.join(staging, "WinLinuxBridge")
    try:
        for absolute, relative in iter_files(ROOT):
            target = os.path.join(stage_root, relative.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(absolute, target)
        normalise_lf(stage_root)

        count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for dirpath, dirnames, filenames in os.walk(stage_root):
                dirnames.sort()
                for name in sorted(filenames):
                    absolute = os.path.join(dirpath, name)
                    relative = os.path.relpath(absolute, staging).replace(os.sep, "/")
                    info = zipfile.ZipInfo(relative)
                    info.external_attr = 0o644 << 16
                    if relative.startswith("WinLinuxBridge/linux/") or \
                            relative.startswith("WinLinuxBridge/tools/") and relative.endswith(".sh"):
                        info.external_attr = 0o755 << 16
                    with open(absolute, "rb") as fh:
                        zf.writestr(info, fh.read(), compress_type=zipfile.ZIP_DEFLATED)
                    count += 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print("[release] archive %s (%d entries, %.1f MB)" % (zip_name, count, size_mb))

    digest = hashlib.sha256()
    with open(zip_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    checksum = digest.hexdigest()
    with open(os.path.join(args.out, "SHA256SUMS.txt"), "w",
              encoding="ascii", newline="\n") as fh:
        fh.write("%s  %s\n" % (checksum, zip_name))
    print("[release] sha256 %s" % checksum)

    assets = os.path.join(ROOT, "docs", "RELEASE_NOTES_v%s.md" % version)
    if os.path.isfile(assets):
        print("[release] release notes present: docs/RELEASE_NOTES_v%s.md" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
