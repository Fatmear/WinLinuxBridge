#!/usr/bin/env python3
"""WinBridgeGUI - the one thing a normal user has to double-click.

It configures, deploys and supervises the bridge between this Windows PC and a
Linux account, without a console and without any installed prerequisite:

  * runs on the portable interpreter in .\\runtime (tkinter included)
  * drives the proven PowerShell scripts (install-win / supervisor / sync)
  * keeps the settings in %USERPROFILE%\\.winlinuxbridge\\bridge.conf
  * keeps your Linux password out of every file on disk by default; the optional
    "remember" box encrypts it with Windows DPAPI, tied to this user account

Module rule: standard library only (tkinter, ctypes, subprocess, urllib).
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

APP_NAME = "WinLinuxBridge"
APP_VERSION = "1.3.0"

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".winlinuxbridge")
CONF_PATH = os.path.join(STATE_DIR, "bridge.conf")
CRED_PATH = os.path.join(STATE_DIR, "credentials.dat")
LOG_DIR = os.path.join(STATE_DIR, "logs")
GUI_LOG = os.path.join(LOG_DIR, "gui.log")

SCRIPT_DIR = HERE
PS_INSTALL = os.path.join(SCRIPT_DIR, "install-win.ps1")
PS_SYNC = os.path.join(SCRIPT_DIR, "winbridge_sync.ps1")
PS_SUPER = os.path.join(SCRIPT_DIR, "winbridge_supervisor.ps1")
CMD_START = os.path.join(SCRIPT_DIR, "start-win.cmd")
PLINK = os.path.join(SCRIPT_DIR, "vendor", "plink.exe")
RUNTIME_PY = os.path.join(SCRIPT_DIR, "runtime", "python.exe")

DEFAULTS = {
    # Empty on purpose: this package is meant to be handed to other people.
    # The window asks for the server the first time; only non-secret settings
    # are stored in bridge.conf.
    "WL_LINUX_HOST": "",
    "WL_LINUX_USER": "",
    "WL_SSH_PORT": "22",
    "WL_REMOTE_ROOT": "",
    "WL_LOCAL_ROOT": os.path.join(HOME, "WinLinuxBridge"),
    "WL_AGENT_PORT": "8787",
    "WL_TUNNEL_PORT": "18080",
    "WL_SSH_KEY": os.path.join(HOME, ".ssh", "wlbridge_tunnel"),
    "WL_SYNC_KEY": os.path.join(HOME, ".ssh", "wlbridge_sync"),
    "WL_SYNC_INTERVAL": "30",
    "WL_PROGRAM_REFRESH": "900",
}

# --------------------------------------------------------------------------- #
# logging and small helpers
# --------------------------------------------------------------------------- #
def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_line(text: str) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(GUI_LOG, "a", encoding="utf-8-sig") as fh:
            fh.write("%s %s\n" % (now(), text))
    except Exception:
        pass


def read_conf() -> dict:
    conf = dict(DEFAULTS)
    try:
        with open(CONF_PATH, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                    value = value[1:-1]
                conf[key] = value
    except FileNotFoundError:
        pass
    except Exception as exc:
        log_line("config read failed: %s" % exc)
    return conf


def write_conf(conf: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    lines = ["# WinLinuxBridge settings (no secrets in this file)"]
    for key, value in conf.items():
        lines.append("%s=%s" % (key, value))
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, CONF_PATH)


def agent_token() -> str:
    tok = (os.environ.get("WL_AGENT_TOKEN") or "").strip()
    if tok:
        return tok
    for path in (os.path.join(STATE_DIR, "agent.token"),
                 os.path.join(STATE_DIR, "agent.token")):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                return fh.read().strip()
        except Exception:
            continue
    return ""


# --------------------------------------------------------------------------- #
# DPAPI - encrypt the optional saved password for this Windows user only
# --------------------------------------------------------------------------- #
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(text: str) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src = _blob(text.encode("utf-8"))
    out = _DataBlob()
    ok = crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("CryptProtectData failed: %d" % ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_unprotect(raw: bytes) -> str:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src = _blob(raw)
    out = _DataBlob()
    ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("CryptUnprotectData failed: %d" % ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(out.pbData)


def save_password(password: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = base64.b64encode(dpapi_protect(password))
    with open(CRED_PATH, "wb") as fh:
        fh.write(payload)


def load_password() -> str:
    try:
        with open(CRED_PATH, "rb") as fh:
            return dpapi_unprotect(base64.b64decode(fh.read()))
    except Exception:
        return ""


def forget_password() -> None:
    try:
        os.remove(CRED_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# process / network probes
# --------------------------------------------------------------------------- #
def _creationflags() -> int:
    return 0x08000000  # CREATE_NO_WINDOW


def run_ps(script: str, args=None, timeout: int = 900, env=None) -> tuple:
    """Runs a PowerShell script file and returns (rc, combined_output)."""
    argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
    if args:
        argv += list(args)
    full_env = os.environ.copy()
    full_env["PYTHONIOENCODING"] = "utf-8"
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout, env=full_env,
            cwd=SCRIPT_DIR, creationflags=_creationflags(),
        )
        out = decode(proc.stdout) + decode(proc.stderr)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "命令超时（%ss）" % timeout
    except Exception as exc:
        return -1, "无法运行 PowerShell: %s" % exc


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp936", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def processes_with(text: str) -> list:
    """Finds processes whose command line contains `text` (see trap D-017)."""
    needle = text.replace("'", "''")
    script = (
        "@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
        "$_.CommandLine.IndexOf('" + needle + "',[StringComparison]::OrdinalIgnoreCase) -ge 0 }) | "
        "ForEach-Object { ($_.ProcessId.ToString()) + [char]9 + $_.Name }"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script],
            capture_output=True, timeout=30, creationflags=_creationflags(),
        )
        rows = []
        for line in decode(proc.stdout).splitlines():
            line = line.strip()
            if not line or "\t" not in line:
                continue
            pid, name = line.split("\t", 1)
            try:
                rows.append({"pid": int(pid), "name": name})
            except ValueError:
                continue
        return rows
    except Exception:
        return []


def port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def agent_ping(port: int, timeout: float = 4.0) -> dict:
    token = agent_token()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/ping" % port,
        headers={"Authorization": "Bearer %s" % token},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe_host_key(host: str, user: str, port: int) -> str:
    """Returns the server fingerprint, or '' when plink is not usable."""
    if not os.path.isfile(PLINK):
        return ""
    try:
        proc = subprocess.run(
            [PLINK, "-ssh", "-batch", "-P", str(port), "%s@%s" % (user, host), "exit"],
            capture_output=True, timeout=30, creationflags=_creationflags(),
        )
        text = decode(proc.stdout) + decode(proc.stderr)
        match = re.search(r"SHA256:[A-Za-z0-9+/=]+", text)
        return match.group(0) if match else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# the window
# --------------------------------------------------------------------------- #
import tkinter as tk
from tkinter import ttk, messagebox, filedialog


COLOR_OK = "#1a7f37"
COLOR_BAD = "#b42318"
COLOR_IDLE = "#8a8f98"


class WinBridgeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WinLinuxBridge - Windows 控制面板")
        self.geometry("980x700")
        self.minsize(900, 640)

        self.conf = read_conf()
        self.msgs: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.fingerprint = ""

        self.var = {
            "host": tk.StringVar(value=self.conf["WL_LINUX_HOST"]),
            "user": tk.StringVar(value=self.conf["WL_LINUX_USER"]),
            "ssh_port": tk.StringVar(value=self.conf["WL_SSH_PORT"]),
            "password": tk.StringVar(value=""),
            "remember": tk.BooleanVar(value=os.path.isfile(CRED_PATH)),
            "remote": tk.StringVar(value=self.conf["WL_REMOTE_ROOT"]),
            "local": tk.StringVar(value=self.conf["WL_LOCAL_ROOT"]),
            "autostart": tk.BooleanVar(value=self._autostart_enabled()),
        }

        self._build_ui()
        self._load_saved_password()
        self.after(150, self._drain)
        self.after(300, self.refresh_status)
        self.after(600, self._announce_layout)

    # -- layout ---------------------------------------------------------- #
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Head.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Sub.TLabel", foreground="#57606a")
        style.configure("Ok.TLabel", foreground=COLOR_OK, font=("Consolas", 9))
        style.configure("Bad.TLabel", foreground=COLOR_BAD, font=("Consolas", 9))
        style.configure("Idle.TLabel", foreground=COLOR_IDLE, font=("Consolas", 9))
        style.configure("Go.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        head = ttk.Frame(self, padding=(14, 12, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text="WinLinuxBridge", style="Head.TLabel").pack(side="left")
        ttk.Label(head, text="  v" + APP_VERSION + "  让 Linux 直接操作这台 Windows",
                  style="Sub.TLabel").pack(side="left", padx=(6, 0))

        body = ttk.Frame(self, padding=(14, 0, 14, 8))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_config(left)
        self._build_actions(right)
        self._build_log()

    def _build_config(self, parent) -> None:
        box = ttk.LabelFrame(parent, text=" 1. 服务器连接 ", padding=10)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)

        rows = [
            ("Linux 地址", "host", None),
            ("登录账号", "user", None),
            ("SSH 端口", "ssh_port", None),
        ]
        for i, (label, key, hint) in enumerate(rows):
            ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", pady=3)
            entry = ttk.Entry(box, textvariable=self.var[key])
            entry.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(box, text="登录密码").grid(row=3, column=0, sticky="w", pady=3)
        pw = ttk.Entry(box, textvariable=self.var["password"], show="*")
        pw.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=3)
        chk = ttk.Checkbutton(box, text="记住密码（DPAPI 加密，仅限本机本账号）",
                              variable=self.var["remember"], command=self._on_remember_toggle)
        chk.grid(row=4, column=1, sticky="w", padx=(8, 0))

        ttk.Label(box, text="远端目录").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.var["remote"]).grid(row=5, column=1, sticky="ew", padx=(8, 0), pady=3)
        ttk.Label(box, text="留空则自动使用 /home/<账号>/WinLinuxBridge",
                  style="Sub.TLabel").grid(row=6, column=1, sticky="w", padx=(8, 0))
        ttk.Label(box, text="本地镜像").grid(row=7, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.var["local"]).grid(row=7, column=1, sticky="ew", padx=(8, 0), pady=3)

        row = ttk.Frame(box)
        row.grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.btn_probe = ttk.Button(row, text="测试连接", command=self.on_probe)
        self.btn_probe.pack(side="left")
        ttk.Button(row, text="保存配置", command=self.on_save).pack(side="left", padx=6)
        ttk.Checkbutton(row, text="开机自动启动", variable=self.var["autostart"],
                        command=self.on_toggle_autostart).pack(side="left", padx=(10, 0))

        dep = ttk.LabelFrame(parent, text=" 2. 一键部署 ", padding=10)
        dep.pack(fill="x", pady=(10, 0))
        ttk.Label(dep, text="装好密钥、上传程序、配置令牌并注册自启。首次使用只需这一步。",
                  style="Sub.TLabel", wraplength=520, justify="left").pack(anchor="w")
        row2 = ttk.Frame(dep)
        row2.pack(anchor="w", pady=(8, 0))
        self.btn_install = ttk.Button(row2, text="安装并启动", style="Go.TButton", command=self.on_install)
        self.btn_install.pack(side="left")
        self.btn_refresh = ttk.Button(row2, text="仅更新程序与配置", command=self.on_update_only)
        self.btn_refresh.pack(side="left", padx=6)

    def _build_actions(self, parent) -> None:
        box = ttk.LabelFrame(parent, text=" 3. 运行状态 ", padding=10)
        box.pack(fill="x")
        self.status_labels = {}
        for key, text in (("agent", "Windows 代理"), ("tunnel", "反向隧道"), ("sync", "共享目录")):
            row = ttk.Frame(box)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=text, width=12).pack(side="left")
            lbl = ttk.Label(row, text="-", style="Idle.TLabel")
            lbl.pack(side="left")
            self.status_labels[key] = lbl
        self.detail = ttk.Label(box, text="", style="Sub.TLabel", wraplength=320, justify="left")
        self.detail.pack(anchor="w", pady=(6, 0))

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(10, 0))
        self.btn_start = ttk.Button(row, text="启动", command=self.on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row, text="停止", command=self.on_stop)
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(row, text="刷新", command=self.refresh_status).pack(side="left")

        tools = ttk.LabelFrame(parent, text=" 工具 ", padding=10)
        tools.pack(fill="x", pady=(10, 0))
        for text, cmd in (
            ("打开本地目录", self.on_open_local),
            ("打开日志目录", self.on_open_logs),
            ("打开使用手册", self.on_open_docs),
            ("自检（不含隧道）", self.on_verify),
        ):
            ttk.Button(tools, text=text, command=cmd).pack(fill="x", pady=2)

        tip = ttk.Label(
            parent,
            text="在 Linux 上运行：~/WinLinuxBridge/linux/wl status",
            style="Sub.TLabel", wraplength=320, justify="left",
        )
        tip.pack(anchor="w", pady=(10, 0))

    def _build_log(self) -> None:
        wrap = ttk.Frame(self, padding=(14, 0, 14, 12))
        wrap.pack(fill="both", expand=True)
        bar = ttk.Frame(wrap)
        bar.pack(fill="x")
        ttk.Label(bar, text="输出").pack(side="left")
        ttk.Button(bar, text="复制", command=self._copy_log).pack(side="right")
        ttk.Button(bar, text="清空", command=lambda: self._set_log("")).pack(side="right", padx=6)
        self.log = tk.Text(wrap, height=12, wrap="none", font=("Consolas", 9),
                           bg="#0f1115", fg="#d7dde5", insertbackground="#d7dde5")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log.tag_configure("err", foreground="#ff7b72")
        self.log.tag_configure("ok", foreground="#7ee787")
        self.log.tag_configure("cmd", foreground="#79c0ff")
        self.log.tag_configure("dim", foreground="#8b949e")

    # -- log plumbing ----------------------------------------------------- #
    def _set_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        if text:
            self.log.insert("end", text)
        self.log.configure(state="disabled")

    def emit(self, text: str, tag: str = "") -> None:
        self.msgs.put(("log", (text, tag)))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "log":
                    text, tag = payload
                    self.log.configure(state="normal")
                    self.log.insert("end", text + "\n", tag or ())
                    self.log.see("end")
                    self.log.configure(state="disabled")
                    log_line(text)
                elif kind == "state":
                    self.msgs.task_done()
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end").strip())
        self.emit("已复制到剪贴板", "dim")

    # -- helpers ---------------------------------------------------------- #
    def _autostart_enabled(self) -> bool:
        startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                               "Start Menu", "Programs", "Startup", "WinLinuxBridge.cmd")
        return os.path.isfile(startup)

    def _load_saved_password(self) -> None:
        if os.path.isfile(CRED_PATH):
            saved = load_password()
            if saved:
                self.var["password"].set(saved)
                self.emit("已读取本机保存的密码（DPAPI）", "dim")

    def _on_remember_toggle(self) -> None:
        if not self.var["remember"].get():
            forget_password()
            self.emit("已删除本机保存的密码", "dim")

    def collect_conf(self) -> dict:
        conf = dict(self.conf)
        conf["WL_LINUX_HOST"] = self.var["host"].get().strip()
        conf["WL_LINUX_USER"] = self.var["user"].get().strip()
        conf["WL_SSH_PORT"] = (self.var["ssh_port"].get().strip() or "22")
        remote = self.var["remote"].get().strip()
        if not remote and conf["WL_LINUX_USER"]:
            remote = "/home/%s/WinLinuxBridge" % conf["WL_LINUX_USER"]
        conf["WL_REMOTE_ROOT"] = remote
        conf["WL_LOCAL_ROOT"] = self.var["local"].get().strip()
        return conf

    def _validate(self, need_password: bool) -> bool:
        host = self.var["host"].get().strip()
        user = self.var["user"].get().strip()
        if not host or not user:
            messagebox.showwarning("缺少信息", "请填写 Linux 地址和登录账号。")
            return False
        if " " in host or "/" in host:
            messagebox.showwarning("地址不合法", "Linux 地址只填主机名或 IP，不要带协议或路径。")
            return False
        try:
            port = int(self.var["ssh_port"].get().strip() or "22")
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showwarning("端口不合法", "SSH 端口应为 1-65535 之间的整数。")
            return False
        if need_password and not self.var["password"].get():
            messagebox.showwarning("缺少密码", "首次部署需要 Linux 登录密码（用完之后不会留在磁盘上）。")
            return False
        return True

    def _busy(self, on: bool) -> None:
        self.busy = on
        state = "disabled" if on else "normal"
        for btn in (self.btn_install, self.btn_refresh, self.btn_probe,
                    self.btn_start, self.btn_stop):
            try:
                btn.configure(state=state)
            except tk.TclError:
                pass
        self.configure(cursor="watch" if on else "")

    def _worker(self, label: str, fn) -> None:
        if self.busy:
            return
        self._busy(True)
        self.emit("=== %s ===" % label, "cmd")

        def runner():
            try:
                fn()
            except Exception as exc:
                self.emit("错误: %s" % exc, "err")
                log_line(traceback.format_exc())
            finally:
                self.msgs.put(("log", ("", "")))
                self.after(0, lambda: self._busy(False))
                self.after(200, self.refresh_status)

        threading.Thread(target=runner, daemon=True).start()

    # -- actions ---------------------------------------------------------- #
    def on_save(self) -> None:
        if not self._validate(need_password=False):
            return
        conf = self.collect_conf()
        try:
            write_conf(conf)
            self.conf = conf
            if self.var["remember"].get() and self.var["password"].get():
                save_password(self.var["password"].get())
                self.emit("配置与密码已保存到 %s" % STATE_DIR, "ok")
            else:
                self.emit("配置已保存到 %s" % CONF_PATH, "ok")
        except Exception as exc:
            self.emit("保存失败: %s" % exc, "err")

    def on_toggle_autostart(self) -> None:
        want = self.var["autostart"].get()
        startup_dir = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                                   "Start Menu", "Programs", "Startup")
        target = os.path.join(startup_dir, "WinLinuxBridge.cmd")
        try:
            if want:
                os.makedirs(startup_dir, exist_ok=True)
                local = self.var["local"].get().strip() or DEFAULTS["WL_LOCAL_ROOT"]
                cmd = os.path.join(local, "win", "start-win.cmd")
                supervisor = os.path.join(local, "win", "winbridge_supervisor.ps1")
                clean = supervisor.replace("\r", "").replace("\n", "").strip().strip('"')
                line = ('start "" /min powershell.exe -NoLogo -NoProfile '
                        '-ExecutionPolicy Bypass -WindowStyle Hidden -File "%s"' % clean)
                body = ("@echo off\r\n"
                        "rem WinLinuxBridge autostart (created by the GUI)\r\n"
                        + line + "\r\n")
                with open(target, "w", encoding="ascii", newline="") as fh:
                    fh.write(body)
                self.emit("已开启开机自启（登录后自动运行）", "ok")
            else:
                if os.path.isfile(target):
                    os.remove(target)
                self.emit("已关闭开机自启", "dim")
        except Exception as exc:
            self.emit("切换自启失败: %s" % exc, "err")
            self.var["autostart"].set(self._autostart_enabled())

    def on_probe(self) -> None:
        if not self._validate(need_password=False):
            return

        def job():
            host = self.var["host"].get().strip()
            user = self.var["user"].get().strip()
            port = int(self.var["ssh_port"].get().strip() or "22")
            self.emit("正在连接 %s:%d ..." % (host, port))
            try:
                with socket.create_connection((host, port), timeout=8):
                    self.emit("TCP 可达：%s:%d" % (host, port), "ok")
            except OSError as exc:
                self.emit("TCP 不可达：%s（%s）" % (host, exc), "err")
                return
            fp = probe_host_key(host, user, port)
            if fp:
                self.fingerprint = fp
                self.emit("服务器主机指纹：%s" % fp)
                self.emit("首次部署会把该指纹固定下来，防止中间人替换。", "dim")
            else:
                self.emit("无法读取主机指纹（可能已在 PuTTY 缓存中，属正常）", "dim")
            keys = [
                ("隧道密钥", os.path.join(HOME, ".ssh", "wlbridge_tunnel")),
                ("同步密钥", os.path.join(HOME, ".ssh", "wlbridge_sync")),
            ]
            for name, path in keys:
                self.emit("%s：%s" % (name, "已存在" if os.path.isfile(path) else "未生成"),
                          "ok" if os.path.isfile(path) else "dim")
            self.emit("系统组件：ssh=%s scp=%s plink=%s runtime=%s" % (
                "是" if which_ssh() else "否",
                "是" if which_scp() else "否",
                "是" if os.path.isfile(PLINK) else "否",
                "是" if os.path.isfile(RUNTIME_PY) else "否"), "dim")

        self._worker("测试连接", job)

    def on_install(self) -> None:
        if not self._validate(need_password=True):
            return
        host = self.var["host"].get().strip()
        user = self.var["user"].get().strip()
        port = int(self.var["ssh_port"].get().strip() or "22")
        remote = self.var["remote"].get().strip()
        local = self.var["local"].get().strip()

        fp = self.fingerprint or probe_host_key(host, user, port)
        if fp and not messagebox.askyesno(
                "确认服务器指纹",
                "服务器 %s 的主机密钥指纹：\n\n%s\n\n确认这是你自己的服务器吗？" % (host, fp)):
            self.emit("已取消：用户未确认服务器指纹", "err")
            return

        if not os.path.isfile(PS_INSTALL):
            self.emit("找不到 install-win.ps1，交付目录不完整", "err")
            return

        args = ["-LinuxHost", host, "-LinuxUser", user, "-RemoteRoot", remote]
        if fp:
            args += ["-HostKey", fp]

        def job():
            write_conf(self.collect_conf())
            if self.var["remember"].get():
                save_password(self.var["password"].get())
            env = {"WL_LINUX_PASSWORD": self.var["password"].get()}
            self.emit("开始部署：装密钥 → 上传程序 → 配置令牌 → 注册自启", "cmd")
            self.emit("首次上传包含便携运行时，约 30 MB，请稍候。", "dim")
            rc, out = run_ps(PS_INSTALL, args, timeout=1800, env=env)
            for line in out.splitlines():
                if line.strip():
                    self.emit(line)
            if rc == 0:
                self.emit("部署完成。Linux 上可执行：%s/linux/wl status" % remote, "ok")
            else:
                self.emit("部署失败（rc=%d）。常见原因：密码错误、Linux 不可达、"
                          "或该账号没有写 ~/.ssh 的权限。" % rc, "err")
            self.emit("debug: local=%s" % local, "dim")

        self._worker("安装并启动", job)

    def on_update_only(self) -> None:
        host = self.var["host"].get().strip()
        user = self.var["user"].get().strip()
        remote = self.var["remote"].get().strip()
        if not os.path.isfile(PS_INSTALL):
            self.emit("找不到 install-win.ps1", "err")
            return

        def job():
            write_conf(self.collect_conf())
            self.emit("重新上传程序文件并刷新 Linux 侧脚本（不动密钥与令牌）", "cmd")
            rc, out = run_ps(PS_INSTALL, ["-LinuxHost", host, "-LinuxUser", user,
                                          "-RemoteRoot", remote, "-SkipKeys"], timeout=1200)
            for line in out.splitlines():
                if line.strip():
                    self.emit(line)
            self.emit("完成（rc=%d）" % rc, "ok" if rc == 0 else "err")

        self._worker("更新程序", job)

    def on_start(self) -> None:
        local = self.var["local"].get().strip() or DEFAULTS["WL_LOCAL_ROOT"]
        cmd = os.path.join(local, "win", "start-win.cmd")
        if not os.path.isfile(cmd):
            cmd = CMD_START
        if not os.path.isfile(cmd):
            self.emit("找不到 start-win.cmd，请先执行「安装并启动」", "err")
            return
        try:
            subprocess.Popen([cmd], cwd=os.path.dirname(cmd),
                             creationflags=0x08000000 | 0x00000008)
            self.emit("已请求启动后台守护（约 10 秒后就绪）", "ok")
        except Exception as exc:
            self.emit("启动失败: %s" % exc, "err")
        self.after(8000, self.refresh_status)

    def on_stop(self) -> None:
        if not messagebox.askyesno("停止", "停止后 Linux 将无法操作这台 Windows。继续吗？"):
            return

        def job():
            hit = []
            hit += processes_with("winbridge_supervisor.ps1")
            hit += processes_with("winbridge_agent.py")
            hit += processes_with("winbridge_tunnel")
            port = self.var.get("tunnel") if False else None
            conf = self.collect_conf()
            tunnel_spec = "127.0.0.1:%s:127.0.0.1:%s" % (
                conf.get("WL_TUNNEL_PORT", "18080"), conf.get("WL_AGENT_PORT", "8787"))
            hit += processes_with(tunnel_spec)
            seen = set()
            for proc in hit:
                if proc["pid"] in seen:
                    continue
                seen.add(proc["pid"])
                try:
                    subprocess.run(["taskkill", "/PID", str(proc["pid"]), "/F"],
                                   capture_output=True, timeout=15, creationflags=_creationflags())
                    self.emit("已结束 %s (pid %d)" % (proc["name"], proc["pid"]))
                except Exception as exc:
                    self.emit("结束 pid %d 失败: %s" % (proc["pid"], exc), "err")
            if not seen:
                self.emit("没有发现正在运行的实例", "dim")

        self._worker("停止", job)

    def refresh_status(self) -> None:
        conf = self.collect_conf()
        agent_port = int(conf.get("WL_AGENT_PORT") or 8787)

        def set_state(key, ok, text):
            lbl = self.status_labels[key]
            lbl.configure(text=text, style="Ok.TLabel" if ok else "Bad.TLabel")

        def job():
            info = None
            try:
                info = agent_ping(agent_port, timeout=3)
            except Exception:
                info = None
            set_state("agent", bool(info), "正常" if info else "未响应")

            conf_local = self.collect_conf()
            tunnel_spec = "127.0.0.1:%s:127.0.0.1:%s" % (
                conf_local.get("WL_TUNNEL_PORT", "18080"), agent_port)
            tunnels = processes_with(tunnel_spec)
            set_state("tunnel", bool(tunnels), "%d 条连接" % len(tunnels) if tunnels else "未建立")

            local = conf_local.get("WL_LOCAL_ROOT", DEFAULTS["WL_LOCAL_ROOT"])
            shared = os.path.join(local, "shared", "to_win")
            if os.path.isdir(shared):
                files = sum(len(f) for _, _, f in os.walk(shared))
                set_state("sync", True, "%d 个文件" % files)
            else:
                set_state("sync", False, "本地目录缺失")

            detail = ""
            if info:
                detail = "Windows: %s\\%s   %sx%s   代理 v%s" % (
                    info.get("host"), info.get("user"),
                    info.get("screen", {}).get("w"), info.get("screen", {}).get("h"),
                    info.get("version"))
            else:
                detail = "代理未响应：点「启动」，或查看日志目录。"
            self.after(0, lambda: self.detail.configure(text=detail))

        threading.Thread(target=job, daemon=True).start()

    def on_open_local(self) -> None:
        path = self.var["local"].get().strip() or DEFAULTS["WL_LOCAL_ROOT"]
        if not os.path.isdir(path):
            messagebox.showinfo("提示", "本地目录还不存在，先执行「安装并启动」。")
            return
        os.startfile(path)

    def on_open_logs(self) -> None:
        os.makedirs(LOG_DIR, exist_ok=True)
        os.startfile(LOG_DIR)

    def on_open_docs(self) -> None:
        local = self.var["local"].get().strip() or DEFAULTS["WL_LOCAL_ROOT"]
        for cand in (os.path.join(local, "docs", "USAGE.md"),
                     os.path.join(HERE, "..", "docs", "USAGE.md")):
            if os.path.isfile(cand):
                webbrowser.open(cand)
                return
        messagebox.showinfo("提示", "找不到 docs/USAGE.md")

    def on_verify(self) -> None:
        script = os.path.join(HERE, "..", "tools", "verify.ps1")
        if not os.path.isfile(script):
            self.emit("找不到 tools/verify.ps1", "err")
            return

        def job():
            self.emit("运行自检（不需要隧道）...", "cmd")
            rc, out = run_ps(script, timeout=600)
            for line in out.splitlines():
                if not line.strip():
                    continue
                tag = "ok" if line.startswith("[PASS]") else ("err" if line.startswith("[FAIL]") else "")
                self.emit(line, tag)
            self.emit("自检结束 rc=%d" % rc, "ok" if rc == 0 else "err")

        self._worker("自检", job)

    def _announce_layout(self) -> None:
        self.emit("交付目录：%s" % os.path.abspath(os.path.join(HERE, "..")), "dim")
        self.emit("设置文件：%s" % CONF_PATH, "dim")
        if not os.path.isfile(RUNTIME_PY):
            self.emit("警告：未找到随包运行时 runtime\\python.exe，将回退系统 Python。", "err")


def which_ssh() -> str:
    for cand in (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe"),):
        if os.path.isfile(cand):
            return cand
    return ""


def which_scp() -> str:
    for cand in (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "OpenSSH", "scp.exe"),):
        if os.path.isfile(cand):
            return cand
    return ""


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def selftest() -> int:
    """Headless check used by tools\\verify.ps1: no window, machine readable."""
    problems = []
    checks = []

    def check(name, fn):
        try:
            value = fn()
            checks.append((name, True, value))
        except Exception as exc:
            checks.append((name, False, "%s: %s" % (type(exc).__name__, exc)))
            problems.append(name)

    check("runtime/python", lambda: sys.version.split()[0])
    check("stdlib/tkinter", lambda: __import__("tkinter").TkVersion)
    check("stdlib/json+urllib", lambda: bool(json and urllib.request))
    check("dpapi/roundtrip", lambda: dpapi_unprotect(dpapi_protect("selftest-中文")))
    check("config/defaults", lambda: read_conf()["WL_AGENT_PORT"])
    check("paths/install-win.ps1", lambda: os.path.isfile(PS_INSTALL))
    check("paths/winbridge_agent.py", lambda: os.path.isfile(os.path.join(SCRIPT_DIR, "winbridge_agent.py")))
    check("vendor/plink.exe", lambda: os.path.isfile(PLINK))
    check("tool/ssh.exe", lambda: which_ssh() or (_ for _ in ()).throw(RuntimeError("not found")))
    check("tool/scp.exe", lambda: which_scp() or (_ for _ in ()).throw(RuntimeError("not found")))
    check("state/dir-writable", lambda: _writable(STATE_DIR))

    for name, ok, value in checks:
        print("[%s] gui/%s: %s" % ("PASS" if ok else "FAIL", name, value))
    return 1 if problems else 0


def _writable(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, ".write-probe")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("x")
    os.remove(probe)
    return path


def uitest() -> int:
    """Builds the real window once and asserts the controls exist. No mainloop."""
    problems = []

    def need(condition, text):
        if not condition:
            problems.append(text)
        print("[%s] ui/%s" % ("PASS" if condition else "FAIL", text))

    try:
        app = WinBridgeApp()
        app.update_idletasks()
        app.update()
    except Exception as exc:
        print("[FAIL] ui/create-window: %s: %s" % (type(exc).__name__, exc))
        return 1

    texts = []

    def walk(w):
        try:
            t = w.cget("text")
            if t:
                texts.append(str(t))
        except Exception:
            pass
        for child in w.winfo_children():
            walk(child)

    walk(app)
    blob = " | ".join(texts)

    need("WinLinuxBridge" in blob, "title-present")
    need("1. 服务器连接" in blob, "config-section")
    need("2. 一键部署" in blob, "deploy-section")
    need("3. 运行状态" in blob, "status-section")
    need("工具" in blob, "tools-section")
    need("安装并启动" in blob, "install-button")
    need("测试连接" in blob, "probe-button")
    need("启动" in blob and "停止" in blob, "run-buttons")
    need("记住密码" in blob, "remember-checkbox")
    need("开机自动启动" in blob, "autostart-checkbox")
    need(app.winfo_width() >= 800 and app.winfo_height() >= 600,
         "window-size %dx%d" % (app.winfo_width(), app.winfo_height()))
    need(len(app.status_labels) == 3, "status-lights=3")
    need("host" in app.var and app.var["host"].get() is not None, "host-field-present")
    need(app.var["local"].get() != "", "local-default-present")

    try:
        app.destroy()
    except Exception:
        pass
    return 1 if problems else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--uitest" in sys.argv:
        return uitest()
    if "--version" in sys.argv:
        print(APP_VERSION)
        return 0
    log_line("gui start (python %s)" % sys.version.split()[0])
    try:
        app = WinBridgeApp()
        app.mainloop()
        return 0
    except Exception:
        log_line(traceback.format_exc())
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "界面启动失败，详情见：\n%s\n\n%s" % (GUI_LOG, traceback.format_exc()[-800:]),
                "WinLinuxBridge", 0x10,
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
