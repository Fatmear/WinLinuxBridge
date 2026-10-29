#!/usr/bin/env python3
r"""WinBridge agent: a loopback-only control endpoint that lets the Linux side
run commands, operate GUI applications, and reach local Windows services.

Security model
--------------
* Binds 127.0.0.1 ONLY. It is reachable from Linux exclusively through an
  SSH reverse tunnel opened by this Windows host (`ssh -R`).
* Every request must carry `Authorization: Bearer <token>` where the token is
  read from the WL_AGENT_TOKEN environment variable or
  %USERPROFILE%\.winlinuxbridge\agent.token .
* No secrets are written to logs or to the repository.

Environment
-----------
WL_AGENT_PORT     listen port on 127.0.0.1 (default 8787)
WL_AGENT_TOKEN    shared secret (required unless agent.token exists)
WL_BRIDGE_DIR     root for relative /fs paths (default %USERPROFILE%\WinLinuxBridge)
WL_SHOT_DIR       screenshot output dir (default %USERPROFILE%\.winlinuxbridge\shots)
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import hmac
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

# The job engine is a sibling module: one execution path for /run and /job/*.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winbridge_jobs as jobs  # noqa: E402

VERSION = "1.3.0"
HOST = "127.0.0.1"
PORT = int(os.environ.get("WL_AGENT_PORT") or 8787)

APP_DIR = os.path.join(os.path.expanduser("~"), ".winlinuxbridge")
LOG_DIR = os.path.join(APP_DIR, "logs")
# Root for relative paths used by /fs (the Windows mirror of the delivery folder).
BRIDGE_DIR = os.environ.get("WL_BRIDGE_DIR") or os.path.join(
    os.path.expanduser("~"), "WinLinuxBridge",
)
# Runtime output stays in the user profile, NOT under %LOCALAPPDATA%: the
# Microsoft Store python is an MSIX package and writes there get redirected
# into a package-private virtual store, invisible to the rest of Windows.
SHOT_DIR = os.environ.get("WL_SHOT_DIR") or os.path.join(APP_DIR, "shots")
MAX_OUTPUT = 1_000_000
MAX_FILE = 64 * 1024 * 1024

log = logging.getLogger("winbridge")

# --------------------------------------------------------------------------- #
# Win32 declarations. 64-bit handles MUST be declared as c_void_p, otherwise
# ctypes truncates them to int32 and the call fails inside the kernel.
# --------------------------------------------------------------------------- #
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.CloseClipboard.restype = wt.BOOL
user32.EmptyClipboard.restype = wt.BOOL
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wt.UINT]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.VkKeyScanW.restype = ctypes.c_short
user32.VkKeyScanW.argtypes = [wt.WCHAR]

gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.BitBlt.restype = wt.BOOL
gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT, wt.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = wt.BOOL
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def load_token() -> str:
    tok = (os.environ.get("WL_AGENT_TOKEN") or "").strip()
    if tok:
        return tok
    path = os.path.join(APP_DIR, "agent.token")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    return ""


def decode_bytes(raw: bytes) -> str:
    """Decode subprocess output without ever raising."""
    for enc in ("utf-8", "cp936", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def trim(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated by winbridge agent]"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# command execution: one engine, two entry points
#
# /run    synchronous compatibility wrapper (returns the old shape plus new fields)
# /job/*  durable jobs: start, query, stream, cancel, survive reconnects
# --------------------------------------------------------------------------- #
# A synchronous /run waits at most this long before answering "still running".
SYNC_WAIT_CEILING = float(os.environ.get("WL_SYNC_WAIT") or 900)
SYNC_WAIT_MARGIN = 30.0


def run_command(payload: dict) -> dict:
    """Legacy synchronous execution on top of the job engine.

    It never blocks longer than the job's own timeout plus a margin, so the
    answer is always truthful: either the job finished, or it is still running
    and the caller gets the job_id to query later.
    """
    info = JOBS.start(payload)
    job_id = str(info.get("job_id") or "")
    try:
        job_timeout = float(info.get("timeout") or 120)
    except (TypeError, ValueError):
        job_timeout = 120.0
    started = time.time()
    deadline = started + min(job_timeout + SYNC_WAIT_MARGIN, SYNC_WAIT_CEILING)

    while True:
        info = JOBS.status(job_id, wait=1.0)
        if info.get("state") in jobs.TERMINAL_STATES:
            break
        if time.time() >= deadline:
            break

    result = JOBS.read_result(job_id)
    state = result.get("state", "UNKNOWN")
    stdout = result.get("stdout", {}) or {}
    stderr = result.get("stderr", {}) or {}
    still_running = state not in jobs.TERMINAL_STATES

    err_text = stderr.get("text", "")
    if state == jobs.TIMEOUT:
        err_text += "\n[winbridge] execution timeout; the process tree was killed"
    elif still_running:
        err_text += ("\n[winbridge] still running after %.0fs - state is UNKNOWN, "
                     "not a failure. Query it with: wl job status %s" % (time.time() - started, job_id))
    elif state == jobs.SPAWN_ERROR:
        err_text += "\n[winbridge] the program could not be started"

    return {
        # legacy fields, unchanged meaning for small outputs
        "rc": result.get("exit_code") if result.get("exit_code") is not None else -1,
        "out": stdout.get("text", ""),
        "err": err_text,
        "timed_out": state == jobs.TIMEOUT,
        "duration_ms": int((time.time() - started) * 1000),
        "shell": result.get("shell", ""),
        # new fields: transport vs process vs program outcome are separate
        "job_id": job_id,
        "request_id": result.get("request_id", ""),
        "state": state,
        "error_code": result.get("error_code", ""),
        "transport_ok": True,
        "running": still_running,
        "exit_code": result.get("exit_code"),
        "detail": result.get("detail", ""),
        "deduplicated": bool(info.get("deduplicated")),
        "stdout_bytes": stdout.get("bytes", 0),
        "stderr_bytes": stderr.get("bytes", 0),
        "stdout_truncated": bool(stdout.get("truncated")),
        "stderr_truncated": bool(stderr.get("truncated")),
        "stdout_sha256": stdout.get("sha256", ""),
        "stderr_sha256": stderr.get("sha256", ""),
        "stdout_path": stdout.get("path", ""),
        "stderr_path": stderr.get("path", ""),
    }


# --------------------------------------------------------------------------- #
# job endpoints
# --------------------------------------------------------------------------- #
def job_start(payload: dict) -> dict:
    return JOBS.start(payload)


def job_status(payload: dict) -> dict:
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("'job_id' is required")
    return JOBS.status(job_id, wait=float(payload.get("wait") or 0.0))


def job_logs(payload: dict) -> dict:
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("'job_id' is required")
    return JOBS.read_log(
        job_id,
        which=str(payload.get("stream") or "stdout"),
        since=int(payload.get("since") or 0),
        max_bytes=int(payload.get("max_bytes") or 65536),
    )


def job_result(payload: dict) -> dict:
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("'job_id' is required")
    return JOBS.read_result(
        job_id,
        head=int(payload.get("head") or jobs.SUMMARY_HEAD),
        tail=int(payload.get("tail") or jobs.SUMMARY_TAIL),
    )


def job_cancel(payload: dict) -> dict:
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("'job_id' is required")
    return JOBS.cancel(job_id, note=str(payload.get("note") or ""))


def job_list(payload: dict) -> dict:
    return JOBS.list_jobs(
        limit=int(payload.get("limit") or 50),
        request_id=str(payload.get("request_id") or ""),
    )


# --------------------------------------------------------------------------- #
# /doctor - one shot environment report, no secrets
# --------------------------------------------------------------------------- #
_DOCTOR_CACHE = {"at": 0.0, "value": {}}


def _shell_inventory() -> dict:
    shells = {}
    for name in ("powershell", "pwsh"):
        exe = shutil.which(name)
        shells[name] = {"path": exe or "", "available": bool(exe), "version": ""}
    comspec = os.environ.get("COMSPEC") or shutil.which("cmd")
    shells["cmd"] = {"path": comspec or "", "available": bool(comspec), "version": ""}
    # Only query versions for shells that exist; this runs at most once a minute.
    if time.time() - _DOCTOR_CACHE["at"] > 60:
        for name in ("powershell", "pwsh"):
            if not shells[name]["available"]:
                continue
            try:
                proc = subprocess.run(
                    [shells[name]["path"], "-NoLogo", "-NoProfile", "-Command",
                     "$PSVersionTable.PSVersion.ToString()"],
                    capture_output=True, timeout=30,
                    creationflags=0x08000000,
                )
                shells[name]["version"] = decode_bytes(proc.stdout).strip()
            except Exception:
                pass
        _DOCTOR_CACHE["at"] = time.time()
        _DOCTOR_CACHE["value"] = shells
        return shells
    cached = _DOCTOR_CACHE["value"]
    for name, info in cached.items():
        if name in shells and info.get("available"):
            shells[name]["version"] = info.get("version", "")
    return shells


def doctor(payload: dict) -> dict:
    runtime = {
        "agent_version": VERSION,
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "oses": os.name,
        "platform": sys.platform,
        "arch": platform.machine(),
        "host": os.environ.get("COMPUTERNAME", ""),
        "user": os.environ.get("USERNAME", ""),
        "session": os.environ.get("SESSIONNAME", ""),
        "admin": _is_admin(),
        "now": now_iso(),
    }
    timeouts = {
        "run_default_s": jobs.DEFAULT_TIMEOUT,
        "sync_wait_ceiling_s": SYNC_WAIT_CEILING,
        "sync_wait_margin_s": SYNC_WAIT_MARGIN,
        "max_job_timeout_s": jobs.MAX_TIMEOUT,
        "capture_cap_bytes": jobs.MAX_CAPTURE_BYTES,
    }
    paths = {
        "state_root": APP_DIR,
        "bridge_root_for_fs": BRIDGE_DIR,
        "fs_allowed_roots": [BRIDGE_DIR, os.path.expanduser("~")],
        "screenshots": SHOT_DIR,
        "jobs": JOBS.jobs_dir,
        "logs": LOG_DIR,
        "home": os.path.expanduser("~"),
    }
    channels = {
        "loopback_only": True,
        "bind": "%s:%d" % (HOST, PORT),
        "file_channels": ["/fs", "/file"],
        "gui_channels": ["/screenshot", "/input", "/window", "/windows", "/clipboard"],
    }
    return {
        "ok": True,
        "runtime": runtime,
        "shells": _shell_inventory(),
        "timeouts": timeouts,
        "paths": paths,
        "channels": channels,
        "jobs": JOBS.stats(),
        "screenshot_engine": "png-stdlib" if not _has_pillow() else "png-pillow",
    }


def _has_pillow() -> bool:
    try:
        import PIL  # noqa: F401

        return True
    except Exception:
        return False


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# GDI screen capture (no third-party dependency)
# --------------------------------------------------------------------------- #
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


def gdi_capture_png(path: str, region=None) -> tuple:
    """Screen capture via GDI, encoded as PNG (the portable runtime has no Pillow)."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    if region:
        x, y, w, h = [int(v) for v in region]
    else:
        x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        y = user32.GetSystemMetrics(77)
        w = user32.GetSystemMetrics(78)
        h = user32.GetSystemMetrics(79)
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h  # top-down rows
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0  # BI_RGB
    stride = w * 4
    buf = ctypes.create_string_buffer(stride * h)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)

    if (w, h) == (0, 0):
        return 0, 0
    _write_png(path, w, h, buf.raw)
    return w, h


def _write_png(path: str, w: int, h: int, bgra: bytes) -> None:
    """Encodes a top-down BGRA buffer as PNG using only the standard library."""
    import struct
    import zlib

    stride = w * 4
    rows = []
    for y in range(h):
        row = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(w * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        rows.append(b"\x00" + bytes(rgb))
    payload = zlib.compress(b"".join(rows), 6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", payload))
        fh.write(chunk(b"IEND", b""))


def screenshot(payload: dict) -> dict:
    os.makedirs(SHOT_DIR, exist_ok=True)
    region = payload.get("region")
    all_screens = bool(payload.get("all_screens", True))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    mode = str(payload.get("mode") or "path")
    path = os.path.join(SHOT_DIR, "win_%s.png" % stamp)
    try:
        from PIL import ImageGrab  # optional fast path when Pillow is installed

        img = ImageGrab.grab(
            bbox=tuple(int(v) for v in region) if region else None,
            all_screens=all_screens,
        )
        img.save(path, "PNG")
    except Exception:
        w, h = gdi_capture_png(path, region)
        if (w, h) == (0, 0):
            raise RuntimeError("screen capture returned empty image")
    size = os.path.getsize(path)
    result = {"path": path, "size": size, "ts": now_iso()}
    if mode == "b64":
        with open(path, "rb") as fh:
            result["b64"] = base64.b64encode(fh.read()).decode("ascii")
    if size < 40 * 1024:
        result["warning"] = "image is very small; the desktop may be locked or black"
    return result


# --------------------------------------------------------------------------- #
# SendInput based mouse / keyboard
# --------------------------------------------------------------------------- #
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


VK_MAP = {
    "CTRL": 0x11, "CONTROL": 0x11, "SHIFT": 0x10, "ALT": 0x12, "WIN": 0x5B,
    "ENTER": 0x0D, "RETURN": 0x0D, "TAB": 0x09, "ESC": 0x1B, "ESCAPE": 0x1B,
    "SPACE": 0x20, "BACKSPACE": 0x08, "DELETE": 0x2E, "DEL": 0x2E,
    "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PAGEDOWN": 0x22,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "INSERT": 0x2D, "PRINTSCREEN": 0x2C,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise RuntimeError("SendInput sent %d/%d events (err=%d)" % (sent, len(inputs), ctypes.get_last_error()))


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, None)
    return inp


def _key_vk(vk: int, up: bool) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    return inp


def _key_unicode(ch: str, up: bool) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, None)
    return inp


def _virtual_screen() -> tuple:
    u = ctypes.windll.user32
    return (u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79))


def _abs(x: int, y: int) -> tuple:
    vx, vy, vw, vh = _virtual_screen()
    nx = int((x - vx) * 65535 / max(vw - 1, 1))
    ny = int((y - vy) * 65535 / max(vh - 1, 1))
    return nx, ny


def input_action(payload: dict) -> dict:
    action = str(payload.get("action") or "").lower()
    if action == "move":
        x, y = int(payload["x"]), int(payload["y"])
        ctypes.windll.user32.SetCursorPos(x, y)
        nx, ny = _abs(x, y)
        _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny))
        return {"ok": True, "cursor": cursor_pos()}
    if action in ("click", "double_click", "right_click", "middle_click"):
        if payload.get("x") is not None:
            ctypes.windll.user32.SetCursorPos(int(payload["x"]), int(payload["y"]))
        down, up = {
            "click": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "double_click": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right_click": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle_click": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[action]
        for _ in range(2 if action == "double_click" else 1):
            _send(_mouse(down))
            time.sleep(0.03)
            _send(_mouse(up))
            time.sleep(0.08)
        return {"ok": True, "cursor": cursor_pos()}
    if action == "wheel":
        delta = int(payload.get("delta") or 120)
        _send(_mouse(MOUSEEVENTF_WHEEL, 0, 0, delta))
        return {"ok": True}
    if action == "key":
        combo = str(payload.get("key") or payload.get("combo") or "")
        return press_combo(combo, repeat=int(payload.get("repeat") or 1))
    if action == "type":
        text = str(payload.get("text") or "")
        return type_text(text)
    raise ValueError("unknown action: %s" % action)


def press_combo(combo: str, repeat: int = 1) -> dict:
    parts = [p.strip() for p in re.split(r"[+]", combo) if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    vks = []
    for part in parts:
        upper = part.upper()
        if upper in VK_MAP:
            vks.append(VK_MAP[upper])
        elif len(part) == 1:
            vks.append(ctypes.windll.user32.VkKeyScanW(part) & 0xFF)
        else:
            raise ValueError("unknown key name: %s" % part)
    out = {"ok": True, "combo": combo}
    for _ in range(max(repeat, 1)):
        for vk in vks:
            _send(_key_vk(vk, False))
            time.sleep(0.01)
        for vk in reversed(vks):
            _send(_key_vk(vk, True))
            time.sleep(0.01)
    return out


def type_text(text: str) -> dict:
    for ch in text:
        code = ord(ch)
        if code > 0xFFFF:  # surrogate pair
            hi = 0xD800 + ((code - 0x10000) >> 10)
            lo = 0xDC00 + ((code - 0x10000) & 0x3FF)
            _send(_key_unicode(chr(hi), False), _key_unicode(chr(lo), False))
            _send(_key_unicode(chr(hi), True), _key_unicode(chr(lo), True))
        elif ch == "\n":
            _send(_key_vk(0x0D, False), _key_vk(0x0D, True))
        else:
            _send(_key_unicode(ch, False), _key_unicode(ch, True))
        time.sleep(0.004)
    return {"ok": True, "chars": len(text)}


def cursor_pos() -> dict:
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return {"x": pt.x, "y": pt.y}


# --------------------------------------------------------------------------- #
# windows inventory / control
# --------------------------------------------------------------------------- #
def list_windows() -> list:
    user32 = ctypes.windll.user32
    result = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        result.append({
            "hwnd": int(hwnd),
            "title": buf.value,
            "pid": int(pid.value),
            "rect": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
            "minimized": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(_cb, 0)
    return result


def window_action(payload: dict) -> dict:
    user32 = ctypes.windll.user32
    action = str(payload.get("action") or "focus").lower()
    hwnd = payload.get("hwnd")
    title = payload.get("title")
    if hwnd:
        hwnd = int(hwnd)
    elif title:
        wanted = str(title).lower()
        match = [w for w in list_windows() if wanted in w["title"].lower()]
        if not match:
            raise LookupError("no window title contains: %s" % title)
        hwnd = match[0]["hwnd"]
    else:
        raise ValueError("hwnd or title is required")

    if action == "focus":
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return {"ok": True, "hwnd": hwnd}
    if action == "minimize":
        user32.ShowWindow(hwnd, 6)
        return {"ok": True, "hwnd": hwnd}
    if action == "maximize":
        user32.ShowWindow(hwnd, 3)
        return {"ok": True, "hwnd": hwnd}
    if action == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return {"ok": True, "hwnd": hwnd}
    raise ValueError("unknown window action: %s" % action)


# --------------------------------------------------------------------------- #
# clipboard
# --------------------------------------------------------------------------- #
def clipboard(payload: dict) -> dict:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    op = str(payload.get("op") or "get").lower()
    CF_UNICODETEXT = 13
    if op == "get":
        if not user32.OpenClipboard(0):
            raise RuntimeError("OpenClipboard failed")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return {"text": ""}
            ptr = kernel32.GlobalLock(handle)
            try:
                return {"text": ctypes.c_wchar_p(ptr).value or ""}
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    if op == "set":
        text = str(payload.get("text") or "")
        if not user32.OpenClipboard(0):
            raise RuntimeError("OpenClipboard failed")
        try:
            user32.EmptyClipboard()
            size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
            handle = kernel32.GlobalAlloc(0x0042, size)  # GMEM_MOVEABLE|GMEM_ZEROINIT
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, handle)
        finally:
            user32.CloseClipboard()
        return {"ok": True, "length": len(text)}
    raise ValueError("unknown clipboard op: %s" % op)


# --------------------------------------------------------------------------- #
# HTTP proxy to local services
# --------------------------------------------------------------------------- #
def http_call(payload: dict) -> dict:
    url = str(payload.get("url") or "")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    method = str(payload.get("method") or "GET").upper()
    headers = {str(k): str(v) for k, v in (payload.get("headers") or {}).items()}
    body = payload.get("body")
    if body is not None and not isinstance(body, str):
        body = json.dumps(body)
        headers.setdefault("Content-Type", "application/json")
    data = body.encode("utf-8") if isinstance(body, str) else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    timeout = float(payload.get("timeout") or 30)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_FILE)
            status = resp.status
            rheaders = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_FILE)
        status = exc.code
        rheaders = dict(exc.headers.items())
    text = None
    try:
        text = raw.decode("utf-8")
    except Exception:
        pass
    out = {"status": status, "headers": rheaders, "size": len(raw), "text": text}
    if text is None:
        out["b64"] = base64.b64encode(raw).decode("ascii")
    return out


# --------------------------------------------------------------------------- #
# file read (for quick pulls that must not wait for sync)
# --------------------------------------------------------------------------- #
def read_file(payload: dict) -> dict:
    path = os.path.abspath(str(payload.get("path") or ""))
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    size = os.path.getsize(path)
    if size > MAX_FILE:
        raise ValueError("file too large: %d bytes (limit %d)" % (size, MAX_FILE))
    with open(path, "rb") as fh:
        raw = fh.read()
    return {
        "path": path,
        "size": size,
        "mtime": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds"),
        "b64": base64.b64encode(raw).decode("ascii"),
    }


# --------------------------------------------------------------------------- #
# shared-folder operations, confined to the bridge dir and the user profile
# --------------------------------------------------------------------------- #
def _confined(path: str) -> str:
    raw = str(path or "")
    if not raw:
        raise ValueError("'path' is required")
    candidate = raw if os.path.isabs(raw) else os.path.join(BRIDGE_DIR, raw)
    resolved = os.path.abspath(candidate)
    allowed = [os.path.normcase(BRIDGE_DIR), os.path.normcase(os.path.expanduser("~"))]
    norm = os.path.normcase(resolved)
    for root in allowed:
        if norm == root or norm.startswith(root + os.sep):
            return resolved
    raise PermissionError("path is outside the allowed roots: %s" % resolved)


def fs_op(payload: dict) -> dict:
    op = str(payload.get("op") or "list").lower()
    target = _confined(payload.get("path") or ".")

    if op == "list":
        if not os.path.isdir(target):
            raise FileNotFoundError(target)
        entries = []
        with os.scandir(target) as it:
            for e in it:
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append({
                    "name": e.name,
                    "type": "dir" if e.is_dir() else "file",
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                })
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return {"path": target, "count": len(entries), "entries": entries}

    if op == "stat":
        if not os.path.exists(target):
            raise FileNotFoundError(target)
        st = os.stat(target)
        return {
            "path": target,
            "type": "dir" if os.path.isdir(target) else "file",
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        }

    if op == "read":
        if not os.path.isfile(target):
            raise FileNotFoundError(target)
        size = os.path.getsize(target)
        if size > MAX_FILE:
            raise ValueError("file too large: %d bytes (limit %d)" % (size, MAX_FILE))
        with open(target, "rb") as fh:
            raw = fh.read()
        out = {"path": target, "size": size}
        try:
            out["text"] = raw.decode("utf-8")
        except Exception:
            out["b64"] = base64.b64encode(raw).decode("ascii")
        return out

    if op == "write":
        os.makedirs(os.path.dirname(target) or BRIDGE_DIR, exist_ok=True)
        if payload.get("b64") is not None:
            raw = base64.b64decode(payload["b64"])
        else:
            raw = str(payload.get("text") or "").encode("utf-8")
        if len(raw) > MAX_FILE:
            raise ValueError("payload too large: %d bytes (limit %d)" % (len(raw), MAX_FILE))
        with open(target, "wb") as fh:
            fh.write(raw)
        return {"path": target, "size": len(raw), "ok": True}

    if op == "mkdir":
        os.makedirs(target, exist_ok=True)
        return {"path": target, "ok": True}

    if op == "rm":
        if not os.path.exists(target):
            raise FileNotFoundError(target)
        if os.path.isdir(target):
            if payload.get("recursive"):
                shutil.rmtree(target)
            else:
                os.rmdir(target)
        else:
            os.remove(target)
        return {"path": target, "ok": True}

    raise ValueError("unknown fs op: %s" % op)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "WinBridge/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route BaseHTTPRequestHandler logs to ours
        log.debug("%s %s", self.address_string(), fmt % args)

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), TOKEN)

    def _payload(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("invalid JSON body: %s" % exc)
        if not isinstance(obj, dict):
            raise ValueError("body must be a JSON object")
        return obj

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not self._authorized():
            self._json(401, {"error": "unauthorized", "hint": "Authorization: Bearer <WL_AGENT_TOKEN>"})
            return
        routes = {
            "/ping": lambda p: {
                "ok": True,
                "version": VERSION,
                "host": os.environ.get("COMPUTERNAME"),
                "user": os.environ.get("USERNAME"),
                "ts": now_iso(),
                "python": sys.version.split()[0],
                "bridge_dir": BRIDGE_DIR,
                "screen": dict(zip(("x", "y", "w", "h"), _virtual_screen())),
                "cursor": cursor_pos(),
                "session_id": os.environ.get("SESSIONNAME"),
            },
            "/run": run_command,
            "/job/start": job_start,
            "/job/status": job_status,
            "/job/logs": job_logs,
            "/job/result": job_result,
            "/job/cancel": job_cancel,
            "/job/list": job_list,
            "/doctor": doctor,
            "/screenshot": screenshot,
            "/input": input_action,
            "/windows": lambda p: {"windows": list_windows()},
            "/window": window_action,
            "/clipboard": clipboard,
            "/http": http_call,
            "/file": read_file,
            "/fs": fs_op,
        }
        handler = routes.get(path)
        if handler is None:
            self._json(404, {"error": "no such endpoint", "path": path, "endpoints": sorted(routes)})
            return
        if method == "POST" and path in ("/ping", "/windows"):
            method = "GET"
        try:
            payload = self._payload() if method == "POST" else {}
            result = handler(payload)
            self._json(200, result if isinstance(result, dict) else {"result": result})
        except PermissionError as exc:
            self._json(403, {"error": "permission denied", "detail": str(exc)})
        except FileNotFoundError as exc:
            self._json(404, {"error": "not found", "detail": str(exc)})
        except (ValueError, KeyError, LookupError, TypeError) as exc:
            self._json(400, {"error": type(exc).__name__, "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.error("handler error on %s: %s\n%s", path, exc, traceback.format_exc())
            self._json(500, {"error": type(exc).__name__, "detail": str(exc)})


def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(
        os.path.join(LOG_DIR, "agent.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8-sig"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    log.addHandler(sh)


def main() -> int:
    global TOKEN, JOBS
    setup_logging()
    TOKEN = load_token()
    if not TOKEN:
        log.error(
            "WL_AGENT_TOKEN is not set and %s\\agent.token does not exist; refusing to start.",
            APP_DIR,
        )
        return 2
    for path in (BRIDGE_DIR, SHOT_DIR, LOG_DIR):
        os.makedirs(path, exist_ok=True)

    # Jobs outlive this process on disk; reclassify the ones that died with the
    # previous agent run so nobody waits on a phantom RUNNING state.
    jobs.set_agent_version(VERSION)
    JOBS = jobs.JobManager(APP_DIR)
    JOBS.recover()
    JOBS.prune(max_age_days=int(os.environ.get("WL_JOB_KEEP_DAYS") or 7))

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        log.error("cannot bind %s:%d (%s) - another instance may be running", HOST, PORT, exc)
        return 3
    log.info("winbridge agent %s listening on http://%s:%d", VERSION, HOST, PORT)
    log.info("bridge dir: %s", BRIDGE_DIR)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("stopping (keyboard interrupt)")
    finally:
        server.server_close()
    return 0


TOKEN = ""
JOBS = None  # type: ignore[assignment]

if __name__ == "__main__":
    sys.exit(main())
