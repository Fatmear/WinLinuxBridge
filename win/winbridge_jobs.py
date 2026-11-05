r"""Persistent job manager for the WinBridge agent.

Why this exists
---------------
The old /run endpoint was synchronous: the Linux client waited on one HTTP
request, so a client timeout left the Windows process running with no way to
find it again, no way to deduplicate a retry, and no way to cancel just that
process. This module gives every execution a durable identity.

Guarantees
----------
* Every execution has a job id that survives: agent restart, client timeout,
  tunnel drop. The record and its output files live on disk under
  %USERPROFILE%\.winlinuxbridge\jobs\<job_id>\.
* A request_id maps to exactly one job. Repeating the same request_id returns
  the original job instead of running anything again (this is what makes
  retry-safe flashing / rebooting / serial tests possible).
* Waiting is separate from executing. A client that stops waiting only stops
  waiting; the job keeps its state and can be queried later.
* Cancelling a job kills that process tree only (taskkill /T on the pid the
  job spawned). It never touches other user programs, the agent or the tunnel.
* Output is captured to files at full length (capped on disk) and returned as a
  small summary (head + tail + byte counts + sha256 + path) plus cursor-based
  access, so a 32 KB serial dump does not have to enter an AI context.

States
------
QUEUED    waiting for its resource key (another job holds it)
RUNNING   process is alive
SUCCEEDED exit code 0
FAILED    process started but exited non-zero
TIMEOUT   hit its own execution timeout; process tree was killed
CANCELLED cancelled on request; process tree was killed
LOST      the agent restarted and the recorded pid is gone (outcome unknown)
SPAWN_ERROR  the process could not be started at all

Error codes (machine readable, distinct from the state)
------------------------------------------------------
TRANSPORT_*      produced by the client, never by this module
SPAWN_FAILED     process creation failed (bad exe, bad cwd, permissions)
PROCESS_EXIT_NONZERO  ran fine, exited non-zero  <- normal test failure
TIMEOUT_KILLED   execution timeout reached, tree killed
CANCELLED_BY_REQUEST
JOB_LOST         agent restarted, child pid vanished
USAGE            bad request payload

A job that "succeeded" at the bridge level says nothing about whether the
program's own test passed. That is exit_code / error_code territory, and the
caller must look at those, not at transport_ok.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("winbridge.jobs")

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
LOST = "LOST"
SPAWN_ERROR = "SPAWN_ERROR"

TERMINAL_STATES = (SUCCEEDED, FAILED, TIMEOUT, CANCELLED, LOST, SPAWN_ERROR)

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# Disk capture is generous; the HTTP summary is small on purpose.
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 7 * 24 * 3600.0
SUMMARY_HEAD = 2000
SUMMARY_TAIL = 2000

def _interval(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
        return min(5.0, max(1.0, value))
    except ValueError:
        return default


META_FLUSH_S = _interval("WL_META_FLUSH_S", 2.0)
HEARTBEAT_S = _interval("WL_JOB_HEARTBEAT_S", 1.0)
DURABLE_FIELDS = frozenset({
    "state", "pid", "exit_code", "error_code", "detail", "started_at",
    "finished_at", "cancel_requested", "timeout_hit", "deduplicated",
    "orphaned", "note", "cancel_note", "request_id", "timings",
})

POWERSHELL_PREAMBLE = (
    "$ErrorActionPreference='Continue'\n"
    "$ProgressPreference='SilentlyContinue'\n"
    "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\n"
    "try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\n"
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def now_iso(ms: bool = False) -> str:
    if ms:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_ms() -> float:
    """Monotonic milliseconds, for phase timings only."""
    return time.perf_counter() * 1000.0


def mono() -> float:
    """Monotonic seconds. Every deadline uses this, never the wall clock.

    The wall clock may jump forward (NTP sync, manual change) and end a healthy
    job early, or jump back and let a runaway job outlive its timeout. Durations
    must therefore never be measured against it; only timestamps (`now_iso`) may.
    """
    return time.monotonic()


def decode_bytes(raw: bytes) -> str:
    """Decode subprocess output without ever raising (D-009)."""
    for enc in ("utf-8-sig", "utf-8", "cp936", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _atomic_write_json(path: str, obj: dict, durable: bool = True) -> None:
    """Atomic replace. `durable=False` skips fsync (heartbeat merges).

    The rename is atomic either way, so a reader never sees a half written
    file; skipping fsync only gives up crash-durability of that one update,
    which is exactly what a heartbeat does not need.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        if durable:
            os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _sha256_file(path: str, limit: int = 0) -> str:
    if not os.path.isfile(path):
        return ""
    digest = hashlib.sha256()
    remaining = limit if limit > 0 else None
    with open(path, "rb") as fh:
        while True:
            want = 1 << 20 if remaining is None else min(1 << 20, remaining)
            if want <= 0:
                break
            chunk = fh.read(want)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def _file_bytes(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class _Capture:
    """A real file object for the child's stdout/stderr plus its bookkeeping.

    subprocess needs an object with fileno(), so this wraps a plain file handle
    instead of inventing a writer. The size cap is applied after the job ends:
    a runaway producer can never exhaust the disk because the check runs on the
    polling tick as well (see enforce_cap).
    """

    def __init__(self, path: str, limit: int = MAX_CAPTURE_BYTES):
        self.path = path
        self.limit = limit
        self.truncated = False
        self._fh = open(path, "wb", buffering=0)

    @property
    def fh(self):
        return self._fh

    def fileno(self) -> int:
        return self._fh.fileno()

    def flush(self) -> None:
        try:
            self._fh.flush()
        except Exception:
            pass

    def enforce_cap(self) -> None:
        """Truncates in place once the file grew past the cap.

        The write handle is shared with the child, so after shrinking the file it
        has to be moved to the new end: an offset left past EOF would make the
        next write pad the gap with NUL bytes.
        """
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size <= self.limit:
            return
        try:
            self.flush()
            self._fh.truncate(self.limit)
            self._fh.seek(0, os.SEEK_END)
            self.truncated = True
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        self.enforce_cap()
        try:
            self._fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# argv construction (single place, so /run and /job agree)
# --------------------------------------------------------------------------- #
def build_argv(payload: dict, temp_dir: str) -> tuple:
    """Returns (argv, temp_path_or_None, shell_label).

    Preferred form is structured: {"exe": "...", "argv": ["a", "b"]} which avoids
    every layer of shell quoting. The legacy {"cmd": "...", "shell": "..."} form
    is still supported and is translated here.
    """
    shell = str(payload.get("shell") or "auto").lower()
    exe = payload.get("exe")
    argv_in = payload.get("argv")

    if exe:
        argv = [str(exe)]
        if isinstance(argv_in, list):
            argv += [str(x) for x in argv_in]
        return argv, None, "exec"

    cmd = payload.get("cmd")
    if cmd is None:
        raise ValueError("either 'exe'+'argv' or 'cmd' is required")

    if isinstance(cmd, list):
        return [str(x) for x in cmd], None, "argv"

    script = str(cmd)
    if shell == "auto":
        shell = "powershell"

    if shell == "none":
        # Popen passes a string directly to CreateProcess on Windows.
        return script, None, "none"

    if shell in ("powershell", "pwsh"):
        exe_name = "pwsh" if shell == "pwsh" else "powershell"
        path = _mkstemp(temp_dir, ".ps1")
        with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
            fh.write(POWERSHELL_PREAMBLE + script)
        return [
            exe_name, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", path,
        ], path, shell

    if shell in ("cmd", "batch"):
        path = _mkstemp(temp_dir, ".cmd")
        body = "@echo off\r\n" + script.replace("\r\n", "\n").replace("\n", "\r\n")
        with open(path, "w", encoding="mbcs", errors="replace", newline="") as fh:
            fh.write(body)
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", path], path, shell

    raise ValueError("unsupported shell: %s" % shell)


def _mkstemp(directory: str, suffix: str) -> str:
    """Creates a temp file and CLOSES the descriptor.

    tempfile.mkstemp returns an open handle; leaving it open keeps the file
    locked on Windows, and the child (PowerShell -File ...) then fails with
    "the file is being used by another process".
    """
    import tempfile

    fd, path = tempfile.mkstemp(suffix=suffix, prefix="wb_", dir=directory)
    os.close(fd)
    return path


# --------------------------------------------------------------------------- #
# the manager
# --------------------------------------------------------------------------- #
class JobManager:
    def __init__(self, root: str):
        self.root = root
        self.jobs_dir = os.path.join(root, "jobs")
        self.requests_dir = os.path.join(self.jobs_dir, "requests")
        self.temp_dir = os.path.join(root, "tmp")
        for path in (self.jobs_dir, self.requests_dir, self.temp_dir):
            os.makedirs(path, exist_ok=True)

        self._lock = threading.RLock()          # re-entrant: meta writes nest
        self._cond = threading.Condition(self._lock)
        self._resource_locks = {}
        self._procs = {}
        self._stop_flags = {}
        self._started = now_iso()
        self._started_ms = now_ms()

        # In-memory truth for every job this agent has touched. Disk is the
        # durable copy, memory is what queries read, so a status poll never has
        # to open a file while the job is running.
        self._meta = {}
        self._dirty = set()
        self._timings = {}
        self._write_ms = {}
        self._t_created = {}
        self._closing = False
        self._close_event = threading.Event()
        self._flush_event = threading.Event()
        self._flusher = threading.Thread(target=self._flush_loop,
                                         name="job-meta-flush", daemon=True)
        self._flusher.start()

    def _flush_loop(self) -> None:
        while not self._close_event.is_set():
            self._flush_event.wait()
            self._flush_event.clear()
            if self._close_event.wait(META_FLUSH_S):
                break
            try:
                self.flush_dirty()
            except OSError:
                log.exception("metadata flush failed")
                self._flush_event.set()

    def flush_dirty(self) -> int:
        with self._lock:
            pending = list(self._dirty)
            for job_id in pending:
                meta = self._meta.get(job_id)
                if meta is not None:
                    self._store_meta(job_id, meta, durable=False)
            return len(pending)

    def request_flush(self) -> None:
        self._flush_event.set()

    def close(self) -> None:
        self._closing = True
        self._close_event.set()
        self._flush_event.set()
        self._flusher.join()
        self.flush_dirty()

    def _load_meta(self, job_id: str) -> dict:
        with self._lock:
            meta = self._meta.get(job_id)
            if meta is None:
                meta = _read_json(self._meta_path(job_id))
                if meta:
                    self._meta[job_id] = meta
            return dict(meta or {})

    def _store_meta(self, job_id: str, meta: dict, durable: bool) -> None:
        with self._lock:
            started = now_ms()
            try:
                _atomic_write_json(self._meta_path(job_id), meta, durable=durable)
                self._meta[job_id] = meta
                self._dirty.discard(job_id)
            finally:
                self._write_ms[job_id] = self._write_ms.get(job_id, 0.0) + now_ms() - started

    def _timing(self, job_id: str, key: str, value: float) -> None:
        with self._lock:
            self._timings.setdefault(job_id, {})[key] = round(float(value), 3)

    def _created_ms(self, job_id: str) -> float:
        with self._lock:
            return self._t_created.get(job_id, now_ms())

    # -- paths ------------------------------------------------------------ #
    def job_dir(self, job_id: str) -> str:
        return os.path.join(self.jobs_dir, job_id)

    def _meta_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "job.json")

    def _request_path(self, request_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", request_id)[:120]
        return os.path.join(self.requests_dir, safe + ".json")

    def _stdout_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "stdout.log")

    def _stderr_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "stderr.log")

    # -- startup recovery -------------------------------------------------- #
    def recover(self) -> int:
        """Marks jobs that were running when the agent died as LOST.

        Without this a restart leaves phantom RUNNING jobs that a client would
        wait on forever. Returns how many were reclassified.
        """
        fixed = 0
        try:
            names = [n for n in os.listdir(self.jobs_dir) if n.startswith("j")]
        except OSError:
            return 0
        for name in names:
            meta = _read_json(self._meta_path(name))
            if not meta or meta.get("state") not in (RUNNING, QUEUED):
                continue
            pid = int(meta.get("pid") or 0)
            if pid and _pid_alive(pid):
                # Still alive: the child outlived us (detached). Keep RUNNING but
                # note that we can no longer stream to it.
                meta["orphaned"] = True
                _atomic_write_json(self._meta_path(name), meta)
                with self._lock:
                    self._meta[name] = meta
                continue
            meta["state"] = LOST
            meta["error_code"] = "JOB_LOST"
            meta["finished_at"] = now_iso()
            meta["last_activity"] = now_iso()
            meta["note"] = "agent restarted and the recorded pid is gone; outcome unknown"
            _atomic_write_json(self._meta_path(name), meta)
            with self._lock:
                self._meta[name] = meta
            fixed += 1
        if fixed:
            log.info("recovered %d job(s) as LOST after restart", fixed)
        return fixed

    def prune(self, max_age_days: int = 7) -> int:
        """Deletes finished job directories older than max_age_days."""
        cutoff = datetime.now() - timedelta(days=max_age_days)
        removed = 0
        try:
            names = [n for n in os.listdir(self.jobs_dir) if n.startswith("j")]
        except OSError:
            return 0
        for name in names:
            path = self.job_dir(name)
            meta = _read_json(os.path.join(path, "job.json"))
            if meta.get("state") not in TERMINAL_STATES:
                continue
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path))
            except OSError:
                continue
            if mtime > cutoff:
                continue
            try:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
                with self._lock:
                    self._meta.pop(name, None)
                    self._timings.pop(name, None)
                    self._write_ms.pop(name, None)
                    self._t_created.pop(name, None)
                    self._dirty.discard(name)
            except Exception:
                pass
        return removed

    # -- start ------------------------------------------------------------- #
    def start(self, payload: dict) -> dict:
        """Starts a job and returns immediately. Never blocks on the job."""
        t_entry = now_ms()
        recv_ms = payload.get("_perf_recv_ms")
        request_id = str(payload.get("request_id") or "").strip()

        if request_id:
            prior = self._lookup_request(request_id)
            if prior:
                info = self.status(prior, wait=0.0)
                info["deduplicated"] = True
                info["note"] = "request_id already seen; returned the original job"
                return info

        timeout = payload.get("timeout")
        try:
            timeout = float(timeout) if timeout not in (None, "") else DEFAULT_TIMEOUT
        except (TypeError, ValueError):
            raise ValueError("timeout must be a number of seconds")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        timeout = min(timeout, MAX_TIMEOUT)

        resource = str(payload.get("resource") or "").strip()
        cwd = str(payload.get("cwd") or os.path.expanduser("~"))
        if not os.path.isdir(cwd):
            return self._spawn_error_record(payload, request_id, resource,
                                            "cwd does not exist: %s" % cwd)

        argv, temp_path, shell_label = build_argv(payload, self.temp_dir)
        t_argv = now_ms()

        job_id = "j%s_%s" % (datetime.now().strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:8])
        os.makedirs(self.job_dir(job_id), exist_ok=True)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        extra_env = payload.get("env") or {}
        if isinstance(extra_env, dict):
            for key, value in extra_env.items():
                env[str(key)] = "" if value is None else str(value)

        meta = {
            "job_id": job_id,
            "request_id": request_id,
            "state": QUEUED,
            "resource": resource,
            "shell": shell_label,
            "cwd": cwd,
            "argv": argv,
            "timeout": timeout,
            "on_timeout": str(payload.get("on_timeout") or "kill").lower(),
            "created_at": now_iso(),
            "created_at_ms": now_iso(ms=True),
            "started_at": "",
            "finished_at": "",
            "last_activity": now_iso(),
            "pid": 0,
            "exit_code": None,
            "error_code": "",
            "transport_ok": True,
            "deduplicated": False,
            "cancel_requested": False,
            "timeout_hit": False,
            "agent_version": VERSION_OF_AGENT,
        }
        self._store_meta(job_id, meta, durable=True)
        with self._lock:
            self._meta[job_id] = meta
        if request_id:
            _atomic_write_json(self._request_path(request_id), {"job_id": job_id})

        self._timings[job_id] = {
            "request_parse_ms": round(max(0.0, t_entry - recv_ms), 3) if recv_ms else 0.0,
            "build_argv_ms": round(max(0.0, t_argv - t_entry), 3),
            "metadata_write_ms": 0.0,
            "queue_wait_ms": 0.0,
            "process_spawn_ms": 0.0,
            "process_run_ms": 0.0,
            "completion_detect_ms": 0.0,
            "result_build_ms": 0.0,
        }
        self._t_created[job_id] = t_entry
        self._timings[job_id]["metadata_write_ms"] = round(
            self._write_ms.get(job_id, 0.0), 3)

        self._stop_flags[job_id] = threading.Event()
        runner = threading.Thread(
            target=self._run_job,
            args=(job_id, argv, cwd, env, timeout, resource, temp_path, payload),
            name="job-" + job_id,
            daemon=True,
        )
        runner.start()
        self._timing(job_id, "start_return_ms", now_ms() - t_entry)

        info = self.status(job_id, wait=0.0)
        info["request_id"] = request_id
        info["deduplicated"] = False
        return info

    def _spawn_error_record(self, payload: dict, request_id: str, resource: str,
                            detail: str) -> dict:
        job_id = "j%s_%s" % (datetime.now().strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:8])
        os.makedirs(self.job_dir(job_id), exist_ok=True)
        meta = {
            "job_id": job_id,
            "request_id": request_id,
            "state": SPAWN_ERROR,
            "resource": resource,
            "shell": "unknown",
            "cwd": str(payload.get("cwd") or ""),
            "argv": payload.get("argv") or payload.get("cmd"),
            "timeout": 0,
            "created_at": now_iso(),
            "started_at": "",
            "finished_at": now_iso(),
            "last_activity": now_iso(),
            "pid": 0,
            "exit_code": None,
            "error_code": "SPAWN_FAILED",
            "transport_ok": True,
            "detail": detail,
            "agent_version": VERSION_OF_AGENT,
        }
        self._store_meta(job_id, meta, durable=True)
        with self._lock:
            self._meta[job_id] = meta
        if request_id:
            _atomic_write_json(self._request_path(request_id), {"job_id": job_id})
        info = self.status(job_id, wait=0.0)
        info["deduplicated"] = False
        return info

    # -- execution --------------------------------------------------------- #
    def _run_job(self, job_id: str, argv: list | str, cwd: str, env: dict,
                 timeout: float, resource: str, temp_path: str, payload: dict) -> None:
        out_cap = _Capture(self._stdout_path(job_id))
        err_cap = _Capture(self._stderr_path(job_id))
        proc = None
        acquired = None
        stdin_path = None
        run_started = 0.0
        try:
            if resource:
                acquired = self._acquire_resource(resource)
                # threading.Semaphore exposes acquire(timeout=...), not wait().
                if not acquired.acquire(timeout=max(timeout, 60.0)):
                    self._finish(job_id, TIMEOUT, None, "RESOURCE_WAIT_TIMEOUT",
                                 detail="another job kept resource %s busy too long" % resource,
                                 out=None, err=None)
                    return
                self._timing(job_id, "queue_wait_ms", now_ms() - self._created_ms(job_id))
                self._patch(job_id, state=RUNNING, started_at=now_iso(),
                            last_activity=now_iso(),
                            note="resource acquired: %s" % resource)
            else:
                self._timing(job_id, "queue_wait_ms", now_ms() - self._created_ms(job_id))
                self._patch(job_id, state=RUNNING, started_at=now_iso(), last_activity=now_iso())

            stdin_data = payload.get("stdin")
            stdin_fh = subprocess.DEVNULL
            if stdin_data is not None:
                stdin_path = _mkstemp(self.temp_dir, ".stdin")
                with open(stdin_path, "wb") as fh:
                    fh.write(str(stdin_data).encode("utf-8"))
                stdin_fh = open(stdin_path, "rb")

            flags = 0
            if os.name == "nt":
                flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

            kwargs = {
                "cwd": cwd,
                "env": env,
                "stdout": out_cap.fh,
                "stderr": err_cap.fh,
                "stdin": stdin_fh,
                "creationflags": flags,
            }
            t_spawn = now_ms()
            try:
                proc = subprocess.Popen(argv, **kwargs)
            except (OSError, ValueError) as exc:
                self._timing(job_id, "process_spawn_ms", now_ms() - t_spawn)
                self._finish(job_id, SPAWN_ERROR, None, "SPAWN_FAILED",
                             detail="cannot start process: %s" % exc, out=out_cap, err=err_cap)
                return
            finally:
                if stdin_fh is not subprocess.DEVNULL:
                    try:
                        stdin_fh.close()
                    except Exception:
                        pass
            self._timing(job_id, "process_spawn_ms", now_ms() - t_spawn)

            with self._lock:
                self._procs[job_id] = proc
            self._patch(job_id, pid=proc.pid, last_activity=now_iso())

            stop = self._stop_flags.get(job_id)
            deadline = mono() + timeout
            run_started = now_ms()

            # Event driven instead of "sleep 150 ms and poll": the watcher thread
            # blocks in proc.wait() (a real OS wait) and sets `done` the moment
            # the child exits, so a 5 ms command is noticed in ~0 ms.
            done = threading.Event()

            def _watch(p=proc, ev=done):
                try:
                    p.wait()
                except Exception:  # noqa: BLE001
                    pass
                ev.set()

            threading.Thread(target=_watch, name="job-watch-" + job_id,
                             daemon=True).start()

            next_beat = mono() + HEARTBEAT_S
            while not done.is_set():
                wait_for = min(HEARTBEAT_S, max(0.0, deadline - mono()))
                done.wait(wait_for)
                if done.is_set():
                    break
                if stop is not None and stop.is_set():
                    self._kill_tree(proc.pid)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    self._timing(job_id, "process_run_ms", now_ms() - run_started)
                    cleanup = self._cleanup_report(proc.pid)
                    self._finish(job_id, CANCELLED, proc.returncode, "CANCELLED_BY_REQUEST",
                                 detail="cancelled on request", out=out_cap, err=err_cap,
                                 extra={"cancel_cleanup": cleanup})
                    return
                if mono() >= deadline:
                    killed = self._kill_tree(proc.pid)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    self._timing(job_id, "process_run_ms", now_ms() - run_started)
                    cleanup = self._cleanup_report(proc.pid)
                    self._finish(job_id, TIMEOUT, proc.returncode, "TIMEOUT_KILLED",
                                 detail="execution timeout after %gs" % timeout,
                                 out=out_cap, err=err_cap,
                                 extra={"timeout_hit": True,
                                        "kill_result": killed,
                                        "cancel_cleanup": cleanup})
                    return
                out_cap.enforce_cap()
                err_cap.enforce_cap()
                if mono() >= next_beat:
                    # Heartbeat: memory only. The flusher merges it to disk.
                    self._patch(job_id, last_activity=now_iso())
                    next_beat = mono() + HEARTBEAT_S

            t_exit = now_ms()
            self._timing(job_id, "process_run_ms", t_exit - run_started)
            rc = proc.returncode
            if stop is not None and stop.is_set():
                self._finish(job_id, CANCELLED, rc, "CANCELLED_BY_REQUEST",
                             detail="cancelled on request", out=out_cap, err=err_cap,
                             extra={"cancel_cleanup": self._cleanup_report(proc.pid)})
                return
            self._finish(job_id, SUCCEEDED if rc == 0 else FAILED, rc,
                         "" if rc == 0 else "PROCESS_EXIT_NONZERO",
                         detail="", out=out_cap, err=err_cap,
                         detect_from=t_exit)
        except Exception as exc:  # noqa: BLE001
            log.error("job %s crashed: %s", job_id, exc)
            self._finish(job_id, SPAWN_ERROR, None, "SPAWN_FAILED",
                         detail="agent error: %s" % exc, out=out_cap, err=err_cap)
        finally:
            out_cap.close()
            err_cap.close()
            with self._lock:
                self._procs.pop(job_id, None)
                self._timings.pop(job_id, None)
                self._t_created.pop(job_id, None)
            self._stop_flags.pop(job_id, None)
            if acquired is not None:
                try:
                    acquired.release()
                except RuntimeError:
                    pass
            for path in (temp_path, stdin_path):
                if path and os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def _finish(self, job_id: str, state: str, exit_code, error_code: str,
                detail: str = "", out=None, err=None,
                extra: dict = None, detect_from: float = None) -> None:
        if out is not None:
            out.flush()
            out.enforce_cap()
        if err is not None:
            err.flush()
            err.enforce_cap()
        stdout_path = self._stdout_path(job_id)
        stderr_path = self._stderr_path(job_id)
        t0 = now_ms()
        with self._lock:
            timings = dict(self._timings.get(job_id, {}))
        timings["metadata_write_ms"] = round(self._write_ms.get(job_id, 0.0), 3)
        if detect_from:
            timings["completion_detect_ms"] = round(max(0.0, t0 - detect_from), 3)
        timings["total_ms"] = round(max(0.0, t0 - self._created_ms(job_id)), 3)
        patch = {
            "state": state,
            "exit_code": exit_code,
            "error_code": error_code,
            "detail": detail or "",
            "finished_at": now_iso(),
            "last_activity": now_iso(),
            "stdout_bytes": _file_bytes(stdout_path),
            "stderr_bytes": _file_bytes(stderr_path),
            "stdout_sha256": _sha256_file(stdout_path),
            "stderr_sha256": _sha256_file(stderr_path),
            "stdout_truncated": bool(out.truncated) if out is not None else False,
            "stderr_truncated": bool(err.truncated) if err is not None else False,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "timings": timings,
        }
        if extra:
            patch.update(extra)
        self._patch(job_id, **patch)
        # The durable write above is itself part of metadata_write_ms; fold the
        # final figure back in memory and let the flusher merge it (no fsync).
        with self._lock:
            meta = self._meta.get(job_id)
            if meta:
                merged = dict(meta.get("timings") or {})
                merged["metadata_write_ms"] = round(self._write_ms.get(job_id, 0.0), 3)
                meta["timings"] = merged
                self._dirty.add(job_id)

    def _patch(self, job_id: str, durable=None, **fields) -> None:
        """Updates a job record.

        `durable=None` (default) decides from the field names: anything that
        changes the meaning of the job is written and fsynced at once, while a
        plain heartbeat only marks the record dirty and is merged by the
        flusher thread. Callers waiting in `status(wait=...)` are woken here,
        so a poll never has to sit through a fixed 200 ms sleep.
        """
        with self._cond:
            meta = self._meta.get(job_id)
            if meta is None:
                meta = _read_json(self._meta_path(job_id))
                if not meta:
                    return
            meta.update(fields)
            must_write = any(key in DURABLE_FIELDS for key in fields)
            if durable is not None:
                must_write = bool(durable) or must_write
            self._meta[job_id] = meta
            if must_write:
                self._store_meta(job_id, meta, durable=True)
                # Only a change that carries meaning wakes the waiters. A
                # heartbeat must not wake every long poll (that would turn
                # notifications back into polling).
                self._cond.notify_all()
            else:
                self._dirty.add(job_id)
                self._flush_event.set()

    def _acquire_resource(self, key: str):
        with self._lock:
            event = self._resource_locks.get(key)
            if event is None:
                event = threading.Semaphore(1)
                self._resource_locks[key] = event
        return event

    # -- cancel ------------------------------------------------------------ #
    def cancel(self, job_id: str, note: str = "") -> dict:
        meta = self._load_meta(job_id)
        if not meta:
            raise FileNotFoundError("no such job: %s" % job_id)
        state = meta.get("state")
        if state in TERMINAL_STATES:
            info = self.status(job_id, wait=0.0)
            info["cancel_result"] = {"killed": 0, "already_finished": True,
                                     "state": state}
            return info

        pid = int(meta.get("pid") or 0)
        self._patch(job_id, cancel_requested=True,
                    last_activity=now_iso(),
                    cancel_note=note or "cancelled via /job/cancel")
        flag = self._stop_flags.get(job_id)
        if flag is not None:
            flag.set()
        killed = self._kill_tree(pid) if pid else 0
        # The runner wakes us through the condition variable the instant it
        # records the final state; no fixed-interval sleeping.
        deadline = mono() + 5.0
        with self._cond:
            while (self._meta.get(job_id) or {}).get("state") not in TERMINAL_STATES:
                remaining = deadline - mono()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
        info = self.status(job_id, wait=0.0)
        info["cancel_result"] = {"killed": killed,
                                 "already_finished": False,
                                 "state": info.get("state"),
                                 "cleanup": self._cleanup_report(pid) if pid else {}}
        return info

    def _kill_tree(self, pid: int) -> int:
        """Kills pid and its descendants only.

        Returns how many processes were confirmed gone. taskkill's own message is
        localised ("SUCCESS" vs Chinese), so the exit code decides and liveness
        is verified afterwards instead of trusting the text.
        """
        if not pid:
            return 0
        was_alive = _pid_alive(pid)
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("taskkill failed for pid %s: %s", pid, exc)
            return 0
        # Real kernel wait on the process handle: no 100 ms sleep loop.
        gone = _wait_pid_gone(pid, 5.0)
        return 1 if (gone and was_alive) else 0

    def _cleanup_report(self, pid: int) -> dict:
        if not pid:
            return {"pid": 0, "alive": False}
        return {"pid": pid, "alive": _pid_alive(pid)}

    # -- queries ----------------------------------------------------------- #
    def _lookup_request(self, request_id: str) -> str:
        data = _read_json(self._request_path(request_id))
        job_id = str(data.get("job_id") or "")
        if job_id and os.path.isdir(self.job_dir(job_id)):
            return job_id
        return ""

    def status(self, job_id: str, wait: float = 0.0) -> dict:
        """Returns the machine readable record. `wait` long-polls for a change.

        The record is read from memory. Waiting is a condition variable that
        `_patch` signals, so a state change is observed immediately instead of
        after a fixed 200 ms sleep; the socket timeout still applies because the
        wait is capped at the client's own budget.
        """
        window = max(0.0, min(float(wait or 0), 120.0))
        deadline = mono() + window
        with self._cond:
            meta = self._meta.get(job_id)
            if meta is None:
                meta = _read_json(self._meta_path(job_id))
                if not meta:
                    raise FileNotFoundError("no such job: %s" % job_id)
                self._meta[job_id] = meta
            snapshot = (meta.get("state"), meta.get("last_activity"))
            while meta.get("state") not in TERMINAL_STATES:
                remaining = deadline - mono()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.5))
                meta = self._meta.get(job_id) or meta
                current = (meta.get("state"), meta.get("last_activity"))
                if current != snapshot:
                    snapshot = current
                    break
        return self._render(job_id, meta)

    def _render(self, job_id: str, meta: dict) -> dict:
        stdout_path = self._stdout_path(job_id)
        stderr_path = self._stderr_path(job_id)
        info = {
            "job_id": job_id,
            "request_id": meta.get("request_id", ""),
            "state": meta.get("state", "UNKNOWN"),
            "transport_ok": True,
            "exit_code": meta.get("exit_code"),
            "error_code": meta.get("error_code", ""),
            "detail": meta.get("detail", ""),
            "resource": meta.get("resource", ""),
            "shell": meta.get("shell", ""),
            "cwd": meta.get("cwd", ""),
            "argv": meta.get("argv"),
            "timeout": meta.get("timeout"),
            "pid": meta.get("pid", 0),
            "created_at": meta.get("created_at", ""),
            "created_at_ms": meta.get("created_at_ms", ""),
            "started_at": meta.get("started_at", ""),
            "finished_at": meta.get("finished_at", ""),
            "last_activity": meta.get("last_activity", ""),
            "timings": meta.get("timings") or {},
            "stdout_truncated": bool(meta.get("stdout_truncated")),
            "stderr_truncated": bool(meta.get("stderr_truncated")),
        }
        for key in ("cancel_cleanup", "kill_result", "cancel_note", "note"):
            if meta.get(key):
                info[key] = meta[key]
        return info

    def wait_terminal(self, job_id: str, timeout: float) -> dict:
        """Blocks until the job reaches a terminal state or the budget expires.

        This is a condition-variable wait, not a poll: the moment the runner
        records the final state the caller is woken. `timeout` simply bounds how
        long a *client* is willing to wait; the job keeps running regardless.
        """
        window = max(0.0, min(float(timeout or 0), MAX_TIMEOUT))
        deadline = mono() + window
        with self._cond:
            meta = self._meta.get(job_id)
            if meta is None:
                meta = _read_json(self._meta_path(job_id))
                if not meta:
                    raise FileNotFoundError("no such job: %s" % job_id)
                self._meta[job_id] = meta
            while meta.get("state") not in TERMINAL_STATES:
                remaining = deadline - mono()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
                meta = self._meta.get(job_id) or meta
        info = self._render(job_id, meta)
        info["waited_ms"] = round(window * 1000.0, 1) if info.get("state") not in TERMINAL_STATES else 0.0
        return info

    def read_log(self, job_id: str, which: str = "stdout", since: int = 0,
                 max_bytes: int = 65536) -> dict:
        if not os.path.isdir(self.job_dir(job_id)):
            raise FileNotFoundError("no such job: %s" % job_id)
        path = self._stdout_path(job_id) if which != "stderr" else self._stderr_path(job_id)
        total = _file_bytes(path)
        since = max(0, int(since or 0))
        max_bytes = max(1, min(int(max_bytes or 65536), 4 * 1024 * 1024))
        if since > total:  # truncated by the capture cap, or the job was recreated
            since = 0
            reset = True
        else:
            reset = False
        raw = b""
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                fh.seek(since)
                raw = fh.read(max_bytes)
        return {
            "job_id": job_id,
            "stream": which,
            "from": since,
            "reset": reset,
            "next": since + len(raw),
            "bytes_total": total,
            "eof": (since + len(raw)) >= total,
            "text": decode_bytes(raw),
        }

    def read_result(self, job_id: str, head: int = SUMMARY_HEAD,
                    tail: int = SUMMARY_TAIL) -> dict:
        started = now_ms()
        info = self.status(job_id, wait=0.0)
        info["stdout"] = _summarize(self._stdout_path(job_id), head, tail)
        info["stderr"] = _summarize(self._stderr_path(job_id), head, tail)
        for stream in ("stdout", "stderr"):
            captured = bool(info.get(stream + "_truncated"))
            info[stream]["capture_truncated"] = captured
            info[stream]["truncated"] = info[stream]["truncated"] or captured
        timings = dict(info.get("timings") or {})
        timings["result_build_ms"] = round(now_ms() - started, 3)
        info["timings"] = timings
        return info

    def list_jobs(self, limit: int = 50, request_id: str = "") -> dict:
        items = []
        try:
            names = sorted((n for n in os.listdir(self.jobs_dir) if n.startswith("j")), reverse=True)
        except OSError:
            names = []
        for name in names[: max(1, int(limit or 50))]:
            meta = _read_json(self._meta_path(name))
            if not meta:
                continue
            if request_id and meta.get("request_id") != request_id:
                continue
            items.append({
                "job_id": name,
                "request_id": meta.get("request_id", ""),
                "state": meta.get("state", ""),
                "exit_code": meta.get("exit_code"),
                "error_code": meta.get("error_code", ""),
                "resource": meta.get("resource", ""),
                "pid": meta.get("pid", 0),
                "created_at": meta.get("created_at", ""),
                "finished_at": meta.get("finished_at", ""),
                "last_activity": meta.get("last_activity", ""),
            })
        return {"count": len(items), "jobs": items}

    def stats(self) -> dict:
        running = 0
        total = 0
        try:
            names = [n for n in os.listdir(self.jobs_dir) if n.startswith("j")]
        except OSError:
            names = []
        for name in names:
            meta = _read_json(self._meta_path(name))
            if not meta:
                continue
            total += 1
            if meta.get("state") in (RUNNING, QUEUED):
                running += 1
        return {
            "total": total,
            "active": running,
            "root": self.jobs_dir,
            "resource_keys": sorted(self._resource_locks.keys()),
            "since": self._started,
        }


def _summarize(path: str, head: int, tail: int) -> dict:
    """Head + tail + metadata, so a huge output never floods a context."""
    total = _file_bytes(path)
    if total == 0:
        return {"text": "", "bytes": 0, "lines": 0, "omitted_bytes": 0,
                "truncated": False, "sha256": "", "path": path}
    head_bytes = b""
    tail_bytes = b""
    raw_head = b""
    with open(path, "rb") as fh:
        raw_head = fh.read(head)
        head_bytes = raw_head
        if total > head + tail:
            fh.seek(max(0, total - tail))
            tail_bytes = fh.read(tail)
        else:
            head_bytes += fh.read(max(0, total - len(head_bytes)))
    omitted = max(0, total - len(head_bytes) - len(tail_bytes))
    if omitted:
        text = (decode_bytes(head_bytes) + "\n...[%d bytes omitted; full output at %s]...\n"
                % (omitted, path) + decode_bytes(tail_bytes))
    else:
        text = decode_bytes(head_bytes)
    return {
        "text": text,
        "bytes": total,
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "omitted_bytes": omitted,
        "truncated": omitted > 0,
        "sha256": _sha256_file(path),
        "path": path,
    }


def _pid_alive(pid: int) -> bool:
    """True when the pid exists. Uses OpenProcess so no console window appears."""
    if not pid:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _wait_pid_gone(pid: int, timeout: float) -> bool:
    """Blocks on the process handle until it exits. True when it is gone.

    SYNCHRONIZE + WaitForSingleObject is a kernel wait, so confirming a kill
    costs no polling at all — the thread sleeps until the process object is
    signalled.
    """
    if not pid:
        return True
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x0
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return True  # already gone, or not ours to watch
        try:
            rc = kernel32.WaitForSingleObject(handle, int(max(0.0, timeout) * 1000))
            return rc == WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return not _pid_alive(pid)


# The agent sets this before creating the manager so records can be audited.
VERSION_OF_AGENT = "unknown"


def set_agent_version(version: str) -> None:
    global VERSION_OF_AGENT
    VERSION_OF_AGENT = version
