# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed

- Bridge clients reject error/non-object envelopes and missing job identities instead
  of reporting successful submission; JSON output remains JSON on transport errors.
- Client wait budgets include submission time and bound status polls using a
  monotonic clock. Batch disconnects retain the submitted job id for recovery.
- `wl job result` propagates task failure/cancellation/startup errors; unfinished
  results and batches return 124, and batch transport failures return 125.
- Shell-free execution preserves structured argument boundaries; legacy Windows
  command-line strings are passed directly to CreateProcess instead of POSIX shlex.
- Outputs between the head size and head-plus-tail size are no longer omitted.
  Capture caps are reapplied after further writes and before final metadata is saved.
- Results distinguish summary omission from actual disk-capture loss through
  `capture_truncated`, preserve this information on restart, and warn on stderr.
- Capture-limit truncation reuses the child's own write handle and seeks back to the
  new end, so a concurrent reader never sees NUL padding or a file several times
  larger than the cap.
- `wl job status --wait` reports an expired budget as 124 with an explicit
  "state is unknown" message while a plain query stays 0; `wl job logs` exposes a
  `reset` flag and warns when the cursor falls past the file end.
- Server-side execution, cancel and wait deadlines use a monotonic clock, so an NTP
  or manual wall-clock change can neither cut a healthy job short nor extend a
  timeout.

### Verification

- `python tools/test_bridge_jobs.py -v`: 26 offline regressions plus real Windows
  process checks for literal arguments, exit codes and persisted capture-limit flags.
- The clock regressions were mutation-checked against the previous implementation and
  fail there, so they guard the fix rather than the code shape.
- Scope: bridge execution/client only; no CSI capture tools, firmware or GUI changes.
  Physical serial disconnection and the live Linux tunnel still require field checks.

## [1.4.0] - 2026-11-01

Event-driven execution, observable deployment and windowless background operation.

### Added

- Installation progress with named phases and percentages in the GUI; installer,
  update and verification output is streamed into the existing log panel.
- Interruptible filesystem-event log following for supervisor, tunnel
  and agent logs, without repeatedly rereading the whole log files.
- Optional bounded `/job/start` submit-and-wait: short tasks return their result in
  the start response; longer tasks retain their durable job identity for follow-up.
- Millisecond phase timings for request parsing, command preparation, queueing,
  process spawn/run, completion detection, metadata writes and result construction.
- `win/start-gui.vbs` and `win/start-win.vbs` as default windowless launchers;
  scheduled watchdog and login startup use windowless entries instead of `.cmd`.
- Six regression checks covering no-console process creation, argument/output/exit
  preservation, live background logs, windowless entries, structured watchdog registration,
  post-mirror startup refresh and expected HTTP disconnects (44 checks total).

### Changed

- Job status is memory-first and waits on a condition variable. Process completion
  blocks on the operating system wait and notifies an event instead of fixed 150 ms
  process polling; synchronous `/run` uses the same terminal-state wait.
- Noncritical heartbeat metadata is merged by a background event-driven flusher
  (default 2 seconds, configurable 1–5 seconds) without per-heartbeat `fsync`.
  Critical identity/state/pid/exit/cancellation/result changes remain atomic and durable.
- The heartbeat interval defaults to 1 second. Safety timeout/output-cap checks still
  run periodically; folder synchronization and GUI health refresh are not fully event-driven.
- Linux synchronous execution waits up to 8 seconds in the start call by default;
  subsequent status requests use 15-second long polling rather than 2-second sleep polling.
- Background SSH/SCP, key generation, host-key probing, token ACL updates, installer
  refresh, agent and tunnel launches explicitly prevent console creation. Captured
  stdout/stderr and background log files remain available.
- Packaging executes a real task with the portable runtime before replacing an archive.
- Release output is ignored and no longer tracked in Git; distributable archives,
  checksums and release notes are attached to GitHub Releases.

### Fixed

- Installation could show Done while every action button remained disabled:
  a long-lived supervisor inherited the installer output pipe, so waiting for
  EOF never completed. GUI command execution now waits for the installer itself,
  stops its blocking reader with `CancelSynchronousIo` and drains trailing output
  without stopping the supervisor or tunnel.
- Supervisor startup explicitly separates all standard handles from the installer
  and closes inherited handles while retaining no-console creation. Its durable
  logs still reach the GUI; later console writes cannot break after installer exit.
- Worker completion, status updates and button unlocking are delivered through
  the UI queue. Configuration is captured on the UI thread, and repeated status
  refreshes are coalesced instead of reading or mutating Tk controls from workers.
- Log notifications wait on filesystem changes or an explicit shutdown event;
  shutdown wakes the watcher and releases its native handles.
- Real GUI lifecycle checks click the install button repeatedly, keep a fixture
  child alive after completion, verify live/trailing output and UI responsiveness,
  and cover nonzero exit, timeout, exception handling and thread cleanup.
- GUI installation progress reaches 100% as soon as the installer emits the
  exact `== Done` completion marker, rather than remaining at 94% until process
  exit and output-pipe closure. GUI regression checks cover the progress bar,
  percentage label, completion phase and unrelated `done` log text.
- Missing `JobManager` flush/cache/timing helpers and persistence constants crashed
  agent startup despite successful syntax checks.
- Cancellation racing with the process-exit event could be reported as `FAILED`
  instead of `CANCELLED`; cancellation intent is persisted before terminating the job.
- Delayed heartbeat snapshots could overwrite final metadata; snapshot writes and
  critical transitions are serialized by the same lock.
- Installation reported success after agent startup or Linux end-to-end checks failed.
  These failures now produce a nonzero installer result and an actionable error.
- Background operations could briefly show a black console because direct native
  invocation and `.cmd` startup bypassed the GUI's hidden-process flags.
- GUI combined `CREATE_NO_WINDOW` with `DETACHED_PROCESS`, which overrides the former;
  it now uses a separate process group while retaining no-console creation.
- Autostart paths containing non-ASCII characters were not representable in ASCII VBS;
  generated startup entries now use UTF-16.
- Packaging scans Pythonw/VBS entry points too, and uses Python AST values to avoid
  mistaking password type annotations or boolean arguments for hardcoded credentials.
- **Same-version rebuild on 2026-11-01:** normal TCP resets and broken pipes no longer
  print misleading request-processing tracebacks; unexpected exceptions remain visible.
- Watchdog registration now uses structured ScheduledTasks actions with the current
  interactive user and limited privilege, rather than nested `schtasks /TR` quoting.
  Registration failures retain their exact diagnostic instead of only a generic warning.
- Installer startup/watchdog migration is retried unconditionally after mirror refresh,
  with VBS-aware fallback before refresh, even when the mirror path is unchanged.
- Watchdog regression mocks explicitly load the task module first and refuse to run
  if the real registration function is still selected, preventing live-task side effects.

### Verification

- Windows `tools/verify.ps1 -Port 19887`: 44/44 checks passed, including the new
  no-console and filesystem-event live-log checks. GUI checks: 11 selftests and 62 UI checks.
- No universal speedup multiplier is claimed: workload, shell, storage and tunnel
  latency differ. Structured `--exe` + argument lists remain the preferred shell-free path.
- Live Linux installation, reconnect and login/watchdog execution were not revalidated
  on a remote deployment for this release; upgrading must refresh existing startup records.

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

[1.4.0]: https://github.com/Fatmear/WinLinuxBridge/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/Fatmear/WinLinuxBridge/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Fatmear/WinLinuxBridge/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Fatmear/WinLinuxBridge/releases/tag/v1.1.0
