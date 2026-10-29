<div align="center">

# WinLinuxBridge

**Operate a Windows desktop from a Linux shell — over a tunnel Windows opens itself.**

Steer Windows applications, GUIs, files and local services from Linux automation with no
inbound port, no port forwarding, no admin rights on either side, and no third-party relay.

[中文](README.zh-CN.md) | **English**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20%7C%20Linux-lightgrey.svg)](#requirements)
[![Python](https://img.shields.io/badge/python-3.13%20bundled-informational.svg)](#zero-install)
[![Version](https://img.shields.io/badge/version-1.3.0-success.svg)](CHANGELOG.md)

</div>

---

## Why this exists

Linux servers are the natural place to run automation and AI agents. The Windows machines
that own the hardware — serial ports, flashing tools, vendor GUIs, internal web endpoints —
are usually unreachable from that Linux host: no inbound port, no administrative rights, and
a public relay is either forbidden or fragile.

WinLinuxBridge inverts the direction. **Windows dials out to Linux** over SSH and opens a
reverse tunnel. Linux then talks to `127.0.0.1` and reaches a control agent running on
Windows. Nothing listens on the network at either end, and nothing has to be installed
system-wide.

```
   Linux host                                         Windows desktop
   ──────────                                         ───────────────
   ~/WinLinuxBridge/  ────── one delivery folder ──────  %USERPROFILE%\WinLinuxBridge\
   ├── win/            sources, GUI, agent
   ├── linux/          wl CLI
   ├── tools/          verification suites
   ├── docs/           manuals
   └── shared/         three sync lanes

   wl run / wl job ──► 127.0.0.1:18080 ══ reverse SSH tunnel ══► 127.0.0.1:8787 agent
                     (opened BY Windows)                         │
                                          subprocess · Win32 · urllib
                                                                 ▼
                                                    programs · GUI · local HTTP
```

## Features

**Execution that survives real networks**

- Every command becomes a durable **job** on Windows disk: state, pid, timestamps, exit code
  and output files stay queryable across client timeouts, tunnel drops and agent restarts.
- **Three timeouts, deliberately separate** — connect, execution, client wait. A client that
  stops waiting reports *state unknown / still running*, never a fake failure.
- **Retry safety** via `request_id`: repeating an identical request returns the original job
  instead of flashing the board twice.
- **Cancel is scoped**: only the targeted process tree is killed, and the cleanup is reported.

**Output that respects context windows**

- Full output is streamed to disk (32 MB cap, enforced live) and returned as a bounded
  summary with byte counts and SHA-256.
- Byte-cursor streaming (`--since`) for incremental reads.
- Extra fields from vendor tools are preserved verbatim — the bridge never rewrites results.

**Operating Windows**

- Run programs with structured `exe + argv[] + cwd + env` (no shell quoting layers) or
  through PowerShell / cmd.
- Screenshots (PNG via GDI + stdlib zlib, no Pillow), mouse, keyboard, clipboard,
  window listing/focus/close, and reverse-proxied HTTP to Windows-local services.
- File read/write confined to the user profile, with an explicit allow-list.

**Zero install, zero privilege**

- Ships a portable CPython with working Tk, plus `plink` for the one-time password step.
- No administrator rights on Windows; only the login user's `$HOME` on Linux.
- One delivery folder; a double-click launcher plus a GUI for first-time setup.

## Requirements

| Side | Needs |
| --- | --- |
| Windows 10/11 | Built-in OpenSSH client (`ssh.exe`, `scp.exe`). No admin, no Python install. |
| Linux | `bash`, `curl` or `python3`, an SSH account. No root, no system packages. |
| Network | Windows can reach the Linux host on SSH. Linux needs **no** inbound path to Windows. |

## Quick start

**1. Windows — double-click `win\start-gui.cmd`**

Fill in the Linux address, account and password, then press **Install and start**
(安装并启动). The first run shows the server host-key fingerprint for confirmation, then
generates keys, installs the public keys, uploads the package, configures the token,
registers autostart and launches the bridge.

The password is not stored by default. The optional "remember" box encrypts it with Windows
DPAPI, bound to the current Windows account and machine.

Command-line equivalent:

```powershell
$env:WL_LINUX_PASSWORD = '<typed locally, never written to disk>'
powershell -ExecutionPolicy Bypass -File win\install-win.ps1 -NonInteractive
```

**2. Linux — use it**

```bash
wl status                 # tunnel, agent and sync lanes at a glance
wl run "ipconfig"         # run a command on Windows
wl doctor                 # versions, shells, timeouts, paths, job stats
```

## CLI reference

| Command | Purpose |
| --- | --- |
| `wl status` / `wl ping` / `wl doctor` | health, identity, full environment report |
| `wl run [opts] -- <cmd>` | run and wait; durable job underneath |
| `wl job start\|status\|logs\|result\|cancel\|list` | full job control |
| `wl batch --file plan.json` | multi-step plan with resource keys |
| `wl ps` / `wl focus` / `wl shot` | window inventory, focus, screenshot |
| `wl click` / `wl move` / `wl type` / `wl key` | mouse and keyboard |
| `wl copy` / `wl paste` | clipboard |
| `wl http <url>` | call a Windows-local HTTP service |
| `wl ls` / `wl get` / `wl put` / `wl rm` | file transfer inside the profile |
| `wl start <path\|url>` / `wl lanes` | open on Windows, inspect sync lanes |

`wl run` options: `--shell`, `--timeout`, `--wait`, `--no-wait`, `--json`,
`--output text|json|summary`, `--request-id`, `--resource`, `--cwd`, `--env`, `--exe`.

## The job model

```
        start                run                  finish
client ──► /job/start ──► QUEUED ──► RUNNING ──► SUCCEEDED | FAILED | TIMEOUT
                            │             │                | CANCELLED | LOST
                            │             └── cancel ──────┘
                            └── resource key held by another job
```

**States** — `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `TIMEOUT`, `CANCELLED`, `LOST`,
`SPAWN_ERROR`.

**Error codes** — `PROCESS_EXIT_NONZERO`, `TIMEOUT_KILLED`, `CANCELLED_BY_REQUEST`,
`SPAWN_FAILED`, `JOB_LOST`, `CLIENT_WAIT_TIMEOUT`, `RESOURCE_WAIT_TIMEOUT`,
`BRIDGE_UNREACHABLE`.

**Exit codes**

| Code | Meaning |
| --- | --- |
| `0` | job succeeded |
| `1` | program ran and exited non-zero — a real test failure |
| `124` | client wait expired; **state unknown, not a failure** |
| `125` | bridge unreachable |
| `126` | program could not be started |
| `130` | job cancelled |

Always judge in this order: `transport_ok` → `state` → `error_code` → `exit_code`.
**A successful bridge call says nothing about whether the program's own test passed.**

Jobs live in `%USERPROFILE%\.winlinuxbridge\jobs\<job_id>\`
(`job.json`, `stdout.log`, `stderr.log`), retained 7 days by default
(`WL_JOB_KEEP_DAYS`). On restart the agent reclassifies jobs whose processes died with it as
`LOST`, so no client waits forever on a phantom `RUNNING`.

## Timeouts

| Timeout | Enforced by | Default | Outcome |
| --- | --- | --- | --- |
| Connect | Linux client (`WL_CONNECT_TIMEOUT`) | 5 s | exit 125, `BRIDGE_UNREACHABLE` |
| Execution | Windows agent (`--timeout`) | 120 s | state `TIMEOUT`, tree killed, `TIMEOUT_KILLED` |
| Client wait | Linux client (`--wait`) | execution + `WL_WAIT_MARGIN` (30 s) | exit 124, still running |

## Shared folder

One delivery folder, mirrored on both sides with identical layout. Three lanes keep
conflicts impossible:

| Lane | Writer | Reader | Sync |
| --- | --- | --- | --- |
| `shared/from_win` | Windows | Linux | pushed every 30 s |
| `shared/to_win` | Linux | Windows | pulled every 30 s |
| `shared/shared` | both | both | pull → newest-wins merge → push |

Deletions are not propagated; use `wl rm` for that. Need it now rather than in 30 s?
`wl put` / `wl get` transfer immediately over the same tunnel.

## Security model

- The agent binds `127.0.0.1` only and is reachable from Linux **solely** through the reverse
  tunnel that Windows opens.
- Every request carries `Authorization: Bearer <token>`. The token is generated during
  install and stored in a user-scoped environment variable plus an ACL-restricted file on
  Windows and a `0600` file on Linux.
- Two dedicated keys. The tunnel key is `restrict,port-forwarding,permitlisten="127.0.0.1:18080"`
  — leaking it opens one loopback port, not a shell. The sync key is `no-pty,no-port-forwarding`.
- The Linux password is used once, to append public keys. It is read from a local session
  environment variable or an interactive prompt, and is not written to any file or log.
- File APIs are confined to an allow-list rooted at the bridge folder and `%USERPROFILE%`;
  anything else returns `403`.
- `wl doctor` reports versions, shells, limits and paths — never credentials.

## Zero install

The release archive is self-contained:

| Component | Version | Licence | Purpose |
| --- | --- | --- | --- |
| CPython (embeddable) | 3.13 | PSF | portable interpreter |
| Tcl/Tk | 8.6 | Tcl/Tk | the setup GUI |
| PuTTY `plink` | 0.77 | MIT | one-time password login for key install |

To rebuild the runtime from public sources:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1
```

The script downloads the official embeddable package and grafts Tcl/Tk plus `_tkinter.pyd`
(which the embeddable build deliberately omits), then verifies that a real `Tk()` window can
be created.

## Building from source (this repository)

This repository holds **source only** — `win/runtime/` and `win/vendor/*.exe` are produced by
the build scripts and shipped inside the release archive instead of being committed.

```powershell
powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1   # portable Python + Tk
powershell -ExecutionPolicy Bypass -File tools\package-release.ps1  # build release zip
```

## Verification

Every claim above is checked by scripts that run on the real tunnel.

| Suite | Covers | Count |
| --- | --- | --- |
| `tools/verify.ps1` | static checks, agent endpoints, job engine, cancel isolation, output limits | 38 |
| `tools/verify.sh` | Linux layout, permissions, tunnel, round trip | 20 |
| `tools/verify_jobs.sh` | timeout semantics, dedupe, cursors, batch, doctor | 17 |
| `tools/verify_cancel.sh` | cancel scope and bridge survival | 6 |

```powershell
powershell -ExecutionPolicy Bypass -File tools\verify.ps1
```

```bash
~/WinLinuxBridge/tools/verify.sh
~/WinLinuxBridge/tools/verify_jobs.sh
~/WinLinuxBridge/tools/verify_cancel.sh
```

## Known limitations

- GUI control requires an unlocked Windows session; a locked session yields black screenshots.
- Autostart triggers at logon, so after a reboot the bridge returns once someone logs in.
- Sync is a periodic full rescan of small trees; large files should use `wl put` / `wl get`.
- The tunnel binds loopback only, so other LAN hosts cannot borrow it (by design).
- One Windows desktop per bridge instance; multiple targets use separate installs.

## Documentation

- [`docs/USAGE.md`](docs/USAGE.md) — deployment, configuration, troubleshooting
- [`docs/AI-AGENT.md`](docs/AI-AGENT.md) — operating manual written for automation and AI agents
- [`CHANGELOG.md`](CHANGELOG.md) — release history

## Contributing

Issues and pull requests are welcome. Please run the four verification suites before opening
a PR, keep shell scripts LF-only (`python tools/normalize-lf.py`), and never commit
credentials, host names or account names.

## License

[MIT](LICENSE). Bundled third-party components keep their own licences, shipped in
`win/vendor/`.
