# WinLinuxBridge v1.3.0 — Durable Execution

**Release date:** 2026-10-29
**Archive:** `WinLinuxBridge-1.3.0.zip` (14.2 MB, 1099 files)
**SHA-256:** see `SHA256SUMS.txt` published with the archive
**Licence:** MIT
**Platforms:** Windows 10/11 (no admin) · Linux (no root)

---

## Highlights

v1.1 made the bridge work. v1.2 made it installable by anyone. **v1.3 makes it safe to drive
long-running hardware work from a network you do not control.**

The previous execution model was a single synchronous HTTP request. When it timed out — a
flaky Wi-Fi hop, a VPN renegotiation, a laptop sleeping — the client could not tell the
difference between *"the bridge is down"*, *"the program crashed"* and *"the board is still
flashing"*. Worse, the naive reaction (retry) could flash the same device twice.

This release replaces that with a job engine where **every execution has a durable identity
on the Windows disk**, waiting is a separate operation from executing, and cancelling is a
separate operation from both.

### What you can now rely on

| Guarantee | How it is enforced |
| --- | --- |
| A timeout never lies | Client wait expiry reports `state=UNKNOWN`, `error_code=CLIENT_WAIT_TIMEOUT`, exit `124`, and prints how to follow the job |
| A retry never re-flashes | `request_id` maps to exactly one job; the repeat returns it with `deduplicated=true` |
| A disconnect loses nothing | Job state, pid, exit code and both output files are on disk, queryable after reconnect |
| A cancel never hits the wrong process | `taskkill /T` on the job's own pid, with a reported `killed` count and post-kill liveness check |
| An agent restart leaves no ghosts | On startup the agent reclassifies jobs whose processes died with it as `LOST` |
| Big output never floods a context | Full text to disk (32 MB live cap), bounded head+tail summary plus `sha256` and a byte cursor |
| Two serial operations never interleave | Resource keys (`serial:COM139`) serialise competing jobs; the second waits in `QUEUED` |

---

## What changed

### Execution

- **Durable job engine** (`win/winbridge_jobs.py`). Eight states: `QUEUED`, `RUNNING`,
  `SUCCEEDED`, `FAILED`, `TIMEOUT`, `CANCELLED`, `LOST`, `SPAWN_ERROR`. Records live in
  `%USERPROFILE%\.winlinuxbridge\jobs\<job_id>\` as `job.json` + `stdout.log` + `stderr.log`,
  retained 7 days (`WL_JOB_KEEP_DAYS`).
- **`/job/*` endpoints**: `start`, `status` (with long polling), `logs`, `result`, `cancel`,
  `list`.
- **`/run` is now a synchronous wrapper** over the same engine. The legacy response fields
  (`rc`, `out`, `err`, `timed_out`, `duration_ms`, `shell`) are unchanged; new fields are
  additive. There is one execution path, not two.
- **Three timeouts, enforced by the right party**:

  | Timeout | Enforced by | Default | On expiry |
  | --- | --- | --- | --- |
  | Connect | Linux client | 5 s | exit `125`, `BRIDGE_UNREACHABLE` |
  | Execution | Windows agent | 120 s | state `TIMEOUT`, process tree killed |
  | Client wait | Linux client | execution + 30 s | exit `124`, job keeps running |

- **Structured execution**: `exe + argv[] + cwd + env`, so paths with spaces, quotes, CJK
  characters or UNC prefixes no longer depend on shell quoting. String / PowerShell / cmd
  forms remain supported.
- **`wl batch`** submits a multi-step plan in one round trip, each step a normal job.

### Observability

- **`/doctor` and `wl doctor --json`**: both-side versions, available shells with their
  versions, every timeout, allowed filesystem roots, job statistics, screenshot engine.
  Contains no credentials — verified by a test that rejects anything resembling a secret.
- **`wl job list`** for recent jobs, filterable by `request_id`.

### Fixed

Four defects that only appear under real use, all previously masked as `SPAWN_ERROR`:

1. A custom writer passed to `Popen` has no `fileno()`, so process creation failed silently.
2. `tempfile.mkstemp` descriptors were never closed. The script file stayed locked, so
   `powershell -File` returned exit code 1 with empty output.
3. `threading.Semaphore` has no `wait()` method. Resource serialisation raised inside the
   worker thread and every job became `SPAWN_ERROR`.
4. `taskkill` output is localised. A `"SUCCESS" in output` check is always false on non-English
   Windows, so cancellation reported `killed=0` while the process was already dead. It now
   uses the exit code plus an `OpenProcess`/`GetExitCodeProcess` liveness check.

### Security and privacy

- Default configuration carries **no server identity**: `WL_LINUX_HOST` and `WL_LINUX_USER`
  are empty, and `WL_REMOTE_ROOT` is derived as `/home/<user>/WinLinuxBridge`.
- The packaging script **refuses to build** if any shippable text file still contains an IPv4
  literal, private key block, password assignment or long token literal.
- The release archive excludes internal development notes, machine state (`agent.token`,
  `credentials.dat`, `bridge.conf`) and VCS metadata.

---

## Verification

All figures were produced on a real deployment across a real tunnel, not from unit mocks.

| Suite | Scope | Result |
| --- | --- | --- |
| `tools/verify.ps1` | static checks, every agent endpoint, job engine, cancel isolation, output limits, legacy `/run` shape, doctor hygiene | **38 / 38** |
| `tools/verify.sh` | Linux layout, permissions, tunnel listener, command round trip | **20 / 20** |
| `tools/verify_jobs.sh` | wait-timeout semantics, reconnect query, cursor streaming, dedupe, execution timeout, big output, UTF-8/quoting, resource serialisation, doctor, batch plan | **17 / 17** |
| `tools/verify_cancel.sh` | cancel scope, neighbour job survival, bridge survival | **6 / 6** |

Reproduce:

```powershell
powershell -ExecutionPolicy Bypass -File tools\verify.ps1
```

```bash
~/WinLinuxBridge/tools/verify.sh
~/WinLinuxBridge/tools/verify_jobs.sh
~/WinLinuxBridge/tools/verify_cancel.sh
```

---

## Verification performed on this build

The archive is not just compiled — it is extracted into an empty directory and executed the
way an end user would run it:

| Check | Result |
| --- | --- |
| Extract `WinLinuxBridge-1.3.0.zip` to a clean directory | 1099 files, no `git/`, no build cache, no credential file |
| `win\runtime\python.exe -c "import encodings, json, tkinter"` | CPython 3.13.14, Tk 8.6 |
| `win\WinBridgeGUI.pyw --selftest` | 11 / 11 pass (including a DPAPI encrypt-decrypt round trip) |
| `win\WinBridgeGUI.pyw --uitest` | 14 / 14 pass |
| `import winbridge_jobs`, `import winbridge_agent` | job engine and agent load, agent reports 1.3.0 |
| `tools/verify.ps1` on the source tree | 38 / 38 |
| `tools/verify.sh` on the Linux host | 20 / 20 |
| `tools/verify_jobs.sh` / `tools/verify_cancel.sh` | 17 / 17 and 6 / 6 |
| End-to-end `wl run "ipconfig"` over the reverse tunnel | Windows output returned; `wl doctor` reports agent 1.3.0 |
| Host-identity scan before packaging | clean — no IP address, account name, host name, key or token in shippable text |

---

## Upgrade notes

Upgrading from v1.2 is in-place; keys and tokens are preserved.

```powershell
powershell -ExecutionPolicy Bypass -File win\install-win.ps1 -NonInteractive -SkipKeys
```

`-SkipKeys` reuses the existing tunnel and sync keys and only refreshes programs, the Linux
helper scripts and the supervisor. Existing `wl run` invocations keep working unchanged —
only additive fields appear in the JSON response.

**New files that must reach the Windows mirror:** `win/winbridge_jobs.py`, `linux/wl_job.py`,
`tools/verify_jobs.sh`, `tools/verify_cancel.sh`, `tools/normalize-lf.py`,
`tools/package_release.py`. The periodic program refresh carries them automatically; a manual
`winbridge_sync.ps1 -Mode program` does the same.

---

## Known limitations

- GUI control requires an unlocked Windows session; a locked session yields black screenshots.
- Autostart triggers at logon, so the bridge returns after a reboot only once someone logs in.
- Sync is a periodic full rescan of small trees; large files should use `wl put` / `wl get`.
- The reverse tunnel binds loopback on the Linux host only, so other LAN machines cannot share it.
- One Windows desktop per bridge instance; multiple targets need separate installs.
- A job whose process survives an agent restart stays `RUNNING` but is marked `orphaned`; its
  output can no longer be streamed live.

---

## Installation

1. Download `WinLinuxBridge-1.3.0.zip` and verify it against `SHA256SUMS.txt`.
2. Extract anywhere on the Windows machine. Do **not** extract only parts of it — the bridge
   needs `win\runtime\` and `win\vendor\` from the same archive.
3. Double-click `win\start-gui.cmd`.
4. Enter the Linux address, account and password, confirm the host-key fingerprint, press
   **Install and start**.

No Python, no pip, no PuTTY, no administrator rights. On Linux the installer has already
created `~/WinLinuxBridge` and put `wl` on `PATH`; open a new shell and run `wl status`.

---

## Documentation

- [`README.md`](https://github.com/Fatmear/WinLinuxBridge/blob/v1.3.0/README.md) — project overview (Chinese) · [`README.en.md`](https://github.com/Fatmear/WinLinuxBridge/blob/v1.3.0/README.en.md) — same in English
- [`docs/USAGE.md`](https://github.com/Fatmear/WinLinuxBridge/blob/v1.3.0/docs/USAGE.md) — deployment, configuration, troubleshooting
- [`docs/AI-AGENT.md`](https://github.com/Fatmear/WinLinuxBridge/blob/v1.3.0/docs/AI-AGENT.md) — operating manual for automation and AI agents
- [`CHANGELOG.md`](https://github.com/Fatmear/WinLinuxBridge/blob/v1.3.0/CHANGELOG.md) — full history

## Licence and third-party components

MIT. The archive bundles CPython 3.13 (PSF), Tcl/Tk 8.6 (Tcl/Tk) and PuTTY `plink` 0.77 (MIT);
their licence texts ship in `win\vendor\`.
