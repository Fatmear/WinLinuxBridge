# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.3.0] - 2026-10-29

Durable execution. This release rebuilds the execution layer around persisted jobs, so that a
client timeout, a tunnel drop or an agent restart can no longer be confused with a failed test.

### Added

- **Durable job engine** (`win/winbridge_jobs.py`) with eight states — `QUEUED`, `RUNNING`,
  `SUCCEEDED`, `FAILED`, `TIMEOUT`, `CANCELLED`, `LOST`, `SPAWN_ERROR` — persisted per job
  as `job.json` plus `stdout.log` / `stderr.log` under `%USERPROFILE%\.winlinuxbridge\jobs\`.
- **`/job/*` endpoints** — `start`, `status`, `logs`, `result`, `cancel`, `list`, with long
  polling on `status`.
- **`/doctor` endpoint** reporting agent version, Python, available shells and versions,
  every timeout, allowed filesystem roots, job statistics and screenshot engine — never
  credentials.
- **Linux job client** (`linux/wl_job.py`) exposed as `wl job ...`, `wl batch ...`, and the
  rebuilt `wl run`.
- **Retry safety** via `request_id`: an identical request returns the original job and is
  flagged `deduplicated`.
- **Scoped cancellation**: `taskkill /T` on the job's own pid only, with a cleanup report
  (`killed`, `alive`).
- **Byte-cursor log streaming** (`wl job logs --since N`) plus bounded result summaries
  carrying `bytes`, `omitted_bytes`, `truncated`, `sha256` and the full-output path.
- **Resource keys** (`--resource serial:COM139`) serialising competing jobs, and `wl batch`
  for multi-step plans submitted in one round trip.
- **Structured execution**: `exe + argv[] + cwd + env`, keeping the string/PowerShell/cmd
  forms for compatibility.
- **`tools/verify_jobs.sh`** (17 checks) and **`tools/verify_cancel.sh`** (6 checks).
- **`tools/package_release.py`** — reproducible zero-install archive with a host-identity
  scan before packaging.
- **`tools/normalize-lf.py`** — enforces LF on every script that runs on Linux.
- Bilingual documentation (`README.md` in Chinese, `README.en.md` in English) and
  `CHANGELOG.md`.

### Changed

- **`/run` is now a synchronous wrapper over the job engine.** The legacy response fields
  (`rc`, `out`, `err`, `timed_out`, `duration_ms`, `shell`) are preserved; new fields
  (`state`, `error_code`, `job_id`, `transport_ok`, `running`, byte counts, hashes) are added
  alongside. There is exactly one execution path.
- **Three timeouts are enforced separately** — connect (client), execution (agent), client
  wait (default: execution + 30 s). A client wait expiry reports *state unknown / still
  running* and exits `124`; it never kills the job and never claims failure.
- **Agent version 1.3.0**; the GUI reports the same version.
- The periodic program refresh now uses a manifest of individual files instead of whole
  directory copies, so the portable runtime is never re-pulled.
- Default configuration no longer carries any server identity: `WL_LINUX_HOST` /
  `WL_LINUX_USER` are empty and `WL_REMOTE_ROOT` is derived as
  `/home/<user>/WinLinuxBridge`.

### Fixed

- Custom writers passed to `Popen` (`no fileno()`) caused silent `SPAWN_ERROR`.
- `tempfile.mkstemp` descriptors were never closed, locking the script file so the child
  could not read it — `powershell -File` returned 1 with empty output.
- `threading.Semaphore` has no `wait()`; resource serialisation failed for every job.
- `taskkill` output is localised, so `"SUCCESS" in output` was always false on non-English
  Windows (`killed` reported 0 while the process was already gone).
- Restart left phantom `RUNNING` jobs; the agent now reclassifies orphaned jobs as `LOST`.
- Large capture files grew unbounded; the 32 MB cap is now enforced while the job runs.
- The packager excluded every `*.zip`, which silently dropped the runtime's own standard
  library archive (`win/runtime/python313.zip`) and made the shipped interpreter abort with
  `ModuleNotFoundError: No module named 'encodings'`. It now excludes only the release
  archive itself, and the stdlib archive is a required entry checked at build time.

### Security

- Packaging refuses to build if any literal IPv4 address, private key block, password
  assignment or long token literal survives in shippable text files.

---

## [1.2.0] - 2026-10-29

Zero-install delivery and a graphical front end.

### Added

- **`win/start-gui.cmd` + `win/WinBridgeGUI.pyw`** — tkinter control panel covering
  configuration, one-click deployment, live status, logs, autostart toggle and self-tests
  (`--selftest`, `--uitest`).
- **Portable CPython runtime** (`win/runtime/`) with working Tcl/Tk, produced by
  `tools/build-runtime.ps1` from the official embeddable package plus the Tcl/Tk files the
  embeddable build omits.
- **Vendored `plink.exe`** (`win/vendor/`) so the one-time password step needs no PuTTY
  installation, with third-party licence texts.
- Optional password storage encrypted with Windows DPAPI, bound to the current account.
- `install-win.ps1 -NonInteractive`, `-SkipKeys`, `-SkipUpload`; SSH port as a first-class
  setting; host key discovered automatically instead of hard coded.

### Changed

- Runtime state moved from `%LOCALAPPDATA%` to `%USERPROFILE%\.winlinuxbridge` because the
  Microsoft Store Python is an MSIX package that redirects writes under AppData into a
  private virtual store invisible to the rest of Windows.
- PNG screenshots are encoded with the standard library (GDI + zlib) instead of falling back
  to 16 MB BMPs when Pillow is absent.
- Autostart is registered only for paths that exist, on a single physical line.

### Fixed

- `scp` single-file downloads do not create local directories, leaving the mirror incomplete
  and autostart pointing at a missing script.
- A `tr -d "\r"` quoted in single quotes deletes every `r` character rather than carriage
  returns, corrupting uploaded shell scripts.
- `scp`'s `-p` (preserve timestamps) was confused with `-P` (port), so the port was treated
  as a source file.
- PowerShell 5.1 decodes BOM-less `.ps1` files as ANSI; Chinese characters could swallow a
  `{` and break parsing. All scripts now ship with a UTF-8 BOM.

---

## [1.1.0] - 2026-10-29

First end-to-end release.

### Added

- Reverse SSH tunnel opened by Windows (`ssh -R 127.0.0.1:18080:127.0.0.1:8787`) and a
  loopback-only control agent with bearer-token authentication.
- Agent endpoints: `/ping`, `/run`, `/screenshot`, `/input`, `/windows`, `/window`,
  `/clipboard`, `/http`, `/file`, `/fs`.
- Supervisor loop keeping the agent and tunnel alive, with restart-on-failure and a five
  minute watchdog task.
- Three-lane shared folder (`shared/from_win`, `shared/to_win`, `shared/shared`) synced with
  `scp` and newest-wins merge.
- Linux CLI `wl` covering execution, GUI control, clipboard, HTTP proxying and file transfer.
- One delivery folder on Linux (`~/WinLinuxBridge`) with an automatically maintained Windows
  mirror, plus login autostart.
- `tools/verify.ps1` and `tools/verify.sh`.

---

[1.3.0]: https://github.com/Fatmear/WinLinuxBridge/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Fatmear/WinLinuxBridge/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Fatmear/WinLinuxBridge/releases/tag/v1.1.0
