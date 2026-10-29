#!/usr/bin/env python3
"""WinBridge job client (Linux side).

Called by `wl`; can also be used directly. Talks to the Windows agent through
the reverse tunnel at 127.0.0.1:${WL_AGENT_PORT}.

Three timeouts are kept separate on purpose
-------------------------------------------
connect timeout     short, 5 s default          -> the tunnel is down
execution timeout   the job's own --timeout     -> enforced on Windows
client wait timeout connect + execution + margin -> how long *this* process waits

A client that stops waiting prints "state unknown / still running" and the job
id. It never claims the job failed and never kills it. Use `wl job status` /
`wl job result` afterwards, from any machine, even after reconnecting.

Exit codes (stable, script friendly)
------------------------------------
0    job SUCCEEDED
1    job FAILED (program ran and returned non-zero) - a real test failure
124  client wait timeout; job state unknown / still running
125  bridge unreachable (tunnel down, agent down, bad token)
126  the program could not be started (SPAWN_FAILED)
130  job was cancelled
2    usage error
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WAIT_TIMEOUT = 124
EXIT_TRANSPORT = 125
EXIT_SPAWN = 126
EXIT_CANCELLED = 130
EXIT_USAGE = 2

TERMINAL = ("SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "LOST", "SPAWN_ERROR")


def env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
        return value if value > 0 else default
    except ValueError:
        return default


CONNECT_TIMEOUT = env_float("WL_CONNECT_TIMEOUT", 5.0)
WAIT_MARGIN = env_float("WL_WAIT_MARGIN", 30.0)
PORT = int(os.environ.get("WL_AGENT_PORT") or 18080)
TOKEN = (os.environ.get("WL_AGENT_TOKEN") or "").strip()


class TransportError(Exception):
    def __init__(self, message: str, code: str = "BRIDGE_UNREACHABLE"):
        super().__init__(message)
        self.code = code


def api(path: str, payload: dict = None, timeout: float = None) -> dict:
    """One HTTP call to the agent. Raises TransportError, never OSError."""
    if not TOKEN:
        raise TransportError(
            "no token: run install-win.ps1 on the Windows side, or check "
            "~/WinLinuxBridge/config/bridge.env", "NO_TOKEN")
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = None
    headers = {"Authorization": "Bearer " + TOKEN}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout or CONNECT_TIMEOUT * 4) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            parsed = json.loads(body)
            detail = parsed.get("detail") or parsed.get("error") or body
        except Exception:
            detail = body or str(exc)
        raise TransportError("agent rejected the request (HTTP %d): %s"
                             % (exc.code, detail), "AGENT_ERROR")
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
        raise TransportError(
            "cannot reach the bridge at 127.0.0.1:%d (%s). "
            "The Windows side is down or the tunnel dropped; it self-heals in "
            "10-30 s, or start it with win\\start-gui.cmd." % (PORT, exc))
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise TransportError("agent returned invalid JSON: %s" % exc, "AGENT_ERROR")


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def emit_json(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def render_result(result: dict, mode: str) -> None:
    """text = program output; summary = metadata only; json = everything."""
    if mode == "json":
        emit_json(result)
        return

    stdout = result.get("stdout") or {}
    stderr = result.get("stderr") or {}

    if mode == "summary":
        emit_json({
            "job_id": result.get("job_id"),
            "state": result.get("state"),
            "exit_code": result.get("exit_code"),
            "error_code": result.get("error_code"),
            "detail": result.get("detail"),
            "stdout_bytes": stdout.get("bytes"),
            "stderr_bytes": stderr.get("bytes"),
            "stdout_truncated": stdout.get("truncated"),
            "stdout_path": stdout.get("path"),
            "stderr_path": stderr.get("path"),
            "stdout_sha256": stdout.get("sha256"),
            "stderr_sha256": stderr.get("sha256"),
            "stdout_head": (stdout.get("text") or "")[:400],
            "stderr_head": (stderr.get("text") or "")[:400],
        })
        return

    text = stdout.get("text") or ""
    if text:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    err = stderr.get("text") or ""
    if err:
        sys.stderr.write(err)
        if not err.endswith("\n"):
            sys.stderr.write("\n")
    detail = result.get("detail") or ""
    if detail:
        sys.stderr.write("[winbridge] %s\n" % detail)


def exit_code_for(result: dict) -> int:
    state = result.get("state")
    if state == "SUCCEEDED":
        return EXIT_OK
    if state == "SPAWN_ERROR":
        return EXIT_SPAWN
    if state in ("CANCELLED",):
        return EXIT_CANCELLED
    if state in ("TIMEOUT", "LOST", "FAILED"):
        rc = result.get("exit_code")
        if isinstance(rc, int) and 0 < rc < 256:
            return rc
        return EXIT_FAILED
    return EXIT_FAILED


# --------------------------------------------------------------------------- #
# wait loop: polling a durable job, never blocking on one HTTP request
# --------------------------------------------------------------------------- #
def wait_for(job_id: str, deadline: float, poll: float = 2.0) -> dict:
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return api("/job/status", {"job_id": job_id, "wait": 0})
        try:
            info = api("/job/status",
                       {"job_id": job_id, "wait": min(poll, remaining)},
                       timeout=max(CONNECT_TIMEOUT, min(poll + 10, remaining + 10)))
        except TransportError:
            # Losing the connection while waiting is not a job failure.
            raise
        if info.get("state") in TERMINAL:
            return info
        if remaining <= 0:
            return info


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    request_id = args.request_id or ("cli-" + uuid.uuid4().hex[:16])
    # --json is the short form of --output json, and it must win over text.
    out_mode = "json" if args.json else args.output

    payload = {
        "request_id": request_id,
        "timeout": float(args.timeout),
        "shell": args.shell,
    }
    if args.resource:
        payload["resource"] = args.resource
    if args.cwd:
        payload["cwd"] = args.cwd
    if args.env:
        payload["env"] = dict(item.split("=", 1) for item in args.env if "=" in item)
    if args.stdin_text is not None:
        payload["stdin"] = args.stdin_text

    # argparse REMAINDER keeps a literal "--" boundary token; drop it so the
    # program never receives it as an argument.
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]

    if args.exe:
        payload["exe"] = args.exe
        payload["argv"] = command
    else:
        if not command:
            sys.stderr.write("wl: run needs a command\n")
            return EXIT_USAGE
        payload["cmd"] = " ".join(command)

    try:
        info = api("/job/start", payload, timeout=max(CONNECT_TIMEOUT, 15.0))
    except TransportError as exc:
        if args.json:
            emit_json({"request_id": request_id, "job_id": None,
                       "transport_ok": False, "state": "UNKNOWN",
                       "error_code": exc.code, "detail": str(exc)})
        else:
            sys.stderr.write("wl: %s\n" % exc)
        return EXIT_TRANSPORT

    job_id = info.get("job_id") or ""
    if info.get("deduplicated"):
        sys.stderr.write("[winbridge] request_id already ran; reusing job %s\n" % job_id)

    if args.no_wait:
        if args.json:
            emit_json(info)
        else:
            sys.stdout.write(job_id + "\n")
        return EXIT_OK

    # Client wait = execution timeout + transfer margin, unless overridden.
    wait_seconds = args.wait if args.wait and args.wait > 0 else (float(args.timeout) + WAIT_MARGIN)
    deadline = time.time() + wait_seconds

    try:
        info = wait_for(job_id, deadline)
    except TransportError as exc:
        # The job keeps running on Windows. Say so instead of pretending.
        if args.json:
            emit_json({"request_id": request_id, "job_id": job_id,
                       "transport_ok": False, "state": "UNKNOWN",
                       "error_code": exc.code, "detail": str(exc),
                       "hint": "the job may still be running: wl job status %s" % job_id})
        else:
            sys.stderr.write(
                "wl: lost the bridge while waiting (%s).\n"
                "    The job is NOT known to have failed; its state is unknown.\n"
                "    Query it later with:  wl job status %s\n" % (exc, job_id))
        return EXIT_TRANSPORT

    if info.get("state") not in TERMINAL:
        if args.json:
            emit_json({
                "request_id": request_id, "job_id": job_id,
                "transport_ok": True, "state": info.get("state"),
                "error_code": "CLIENT_WAIT_TIMEOUT",
                "detail": "still running after %.0fs; state is unknown, not failed"
                          % wait_seconds,
                "pid": info.get("pid"), "started_at": info.get("started_at"),
                "hint": "wl job status %s | wl job logs %s | wl job cancel %s"
                        % (job_id, job_id, job_id),
            })
        else:
            sys.stderr.write(
                "wl: still running after %.0fs - STATE UNKNOWN, this is NOT a failure.\n"
                "    job id: %s  (pid %s, started %s)\n"
                "    follow it:  wl job status %s\n"
                "    its log  :  wl job logs %s -f\n"
                "    stop it  :  wl job cancel %s\n"
                % (wait_seconds, job_id, info.get("pid"), info.get("started_at"),
                   job_id, job_id, job_id))
        return EXIT_WAIT_TIMEOUT

    try:
        result = api("/job/result", {"job_id": job_id},
                     timeout=max(CONNECT_TIMEOUT * 4, 30.0))
    except TransportError as exc:
        sys.stderr.write("wl: job %s finished as %s but the result could not be "
                         "fetched (%s); retry: wl job result %s\n"
                         % (job_id, info.get("state"), exc, job_id))
        return EXIT_TRANSPORT

    result.setdefault("request_id", request_id)
    render_result(result, out_mode)
    return exit_code_for(result)


def cmd_batch(args) -> int:
    """Runs a plan of steps sequentially, each as its own durable job.

    Plan shape (JSON, from --file or stdin):
      {"resource": "serial:COM139", "continue_on_error": false, "timeout": 60,
       "steps": [{"name": "probe", "cmd": "echo hi", "shell": "cmd"},
                 {"name": "run", "exe": "tool.exe", "argv": ["cli", "info"]}]}

    A step naming the same resource key is serialised on the Windows side, so
    two serial operations can never interleave. Every step keeps its own job id
    and can be queried or cancelled afterwards.
    """
    try:
        if args.file:
            with open(args.file, "r", encoding="utf-8-sig") as fh:
                plan = json.load(fh)
        else:
            plan = json.load(sys.stdin)
    except Exception as exc:
        sys.stderr.write("wl: cannot read the plan: %s\n" % exc)
        return EXIT_USAGE

    steps = plan.get("steps") or []
    if not isinstance(steps, list) or not steps:
        sys.stderr.write("wl: the plan needs a non-empty 'steps' array\n")
        return EXIT_USAGE

    prefix = args.request_id or plan.get("request_id") or ("batch-" + uuid.uuid4().hex[:12])
    shared_resource = plan.get("resource") or ""
    continue_on_error = bool(plan.get("continue_on_error"))
    default_timeout = float(plan.get("timeout") or args.timeout)
    wait_seconds = args.wait if args.wait and args.wait > 0 else default_timeout + WAIT_MARGIN

    results = []
    started = time.time()
    for index, step in enumerate(steps):
        name = str(step.get("name") or ("step%d" % index))
        payload = {key: step[key] for key in
                   ("cmd", "shell", "cwd", "env", "exe", "argv", "stdin", "on_timeout")
                   if key in step}
        payload["timeout"] = float(step.get("timeout") or default_timeout)
        payload["resource"] = str(step.get("resource") or shared_resource)
        payload["request_id"] = "%s:%s" % (prefix, name)

        try:
            info = api("/job/start", payload, timeout=max(CONNECT_TIMEOUT, 15.0))
            job_id = info.get("job_id")
            final = wait_for(job_id, time.time() + wait_seconds)
        except TransportError as exc:
            results.append({"name": name, "job_id": None, "state": "UNKNOWN",
                            "transport_ok": False, "error_code": exc.code,
                            "detail": str(exc)})
            break

        entry = {
            "name": name,
            "job_id": job_id,
            "state": final.get("state"),
            "exit_code": final.get("exit_code"),
            "error_code": final.get("error_code"),
            "transport_ok": True,
            "deduplicated": bool(info.get("deduplicated")),
        }
        if final.get("state") not in TERMINAL:
            entry["error_code"] = "CLIENT_WAIT_TIMEOUT"
            entry["detail"] = "still running; query job %s later" % job_id
        results.append(entry)

        if final.get("state") != "SUCCEEDED" and not continue_on_error:
            break

    overall_ok = (all(item.get("state") == "SUCCEEDED" for item in results)
                  and len(results) == len(steps))
    summary = {
        "request_id": prefix,
        "ok": overall_ok,
        "transport_ok": all(item.get("transport_ok") for item in results),
        "step_count": len(steps),
        "ran": len(results),
        "duration_ms": int((time.time() - started) * 1000),
        "steps": results,
    }
    emit_json(summary)
    return EXIT_OK if overall_ok else EXIT_FAILED


def cmd_status(args) -> int:
    try:
        info = api("/job/status",
                   {"job_id": args.job_id, "wait": float(args.wait or 0)},
                   timeout=max(CONNECT_TIMEOUT * 4, float(args.wait or 0) + 15))
    except TransportError as exc:
        sys.stderr.write("wl: %s\n" % exc)
        return EXIT_TRANSPORT
    if args.json:
        emit_json(info)
        return EXIT_OK
    for key in ("job_id", "state", "exit_code", "error_code", "pid",
                "started_at", "finished_at", "last_activity", "stdout_bytes",
                "stderr_bytes", "resource", "detail"):
        value = info.get(key)
        if value not in (None, "", 0):
            sys.stdout.write("%-14s %s\n" % (key, value))
    return EXIT_OK


def cmd_logs(args) -> int:
    cursor = int(args.since or 0)
    chunk = 1 if args.follow else 0
    while True:
        try:
            data = api("/job/logs",
                       {"job_id": args.job_id, "stream": args.stream,
                        "since": cursor, "max_bytes": int(args.max_bytes)},
                       timeout=max(CONNECT_TIMEOUT * 4, 30.0))
        except TransportError as exc:
            sys.stderr.write("wl: %s\n" % exc)
            return EXIT_TRANSPORT
        if args.json:
            emit_json(data)
        else:
            sys.stdout.write(data.get("text") or "")
            sys.stdout.flush()
        cursor = int(data.get("next") or cursor)
        if not args.follow:
            break
        time.sleep(1.0)
        chunk += 1
        if chunk > int(args.max_polls):
            break
    if not args.json and not args.follow:
        sys.stderr.write("[cursor %d / %d bytes]\n"
                         % (cursor, data.get("bytes_total", 0)))
    return EXIT_OK


def cmd_result(args) -> int:
    try:
        result = api("/job/result", {"job_id": args.job_id},
                     timeout=max(CONNECT_TIMEOUT * 4, 30.0))
    except TransportError as exc:
        sys.stderr.write("wl: %s\n" % exc)
        return EXIT_TRANSPORT
    render_result(result, "json" if args.json else args.output)
    return EXIT_OK


def cmd_cancel(args) -> int:
    try:
        info = api("/job/cancel", {"job_id": args.job_id, "note": args.note or ""},
                   timeout=max(CONNECT_TIMEOUT * 4, 60.0))
    except TransportError as exc:
        sys.stderr.write("wl: %s\n" % exc)
        return EXIT_TRANSPORT
    if args.json:
        emit_json(info)
        return EXIT_OK
    cleanup = info.get("cancel_result") or {}
    sys.stdout.write("job %s -> %s\n" % (info.get("job_id"), info.get("state")))
    if cleanup:
        sys.stdout.write("killed: %s   already finished: %s\n"
                         % (cleanup.get("killed"), cleanup.get("already_finished")))
        alive = (cleanup.get("cleanup") or {}).get("alive")
        if alive is not None:
            sys.stdout.write("process still alive: %s\n" % alive)
    return EXIT_OK


def cmd_list(args) -> int:
    try:
        data = api("/job/list", {"limit": int(args.limit),
                                 "request_id": args.request_id or ""},
                   timeout=max(CONNECT_TIMEOUT * 4, 30.0))
    except TransportError as exc:
        sys.stderr.write("wl: %s\n" % exc)
        return EXIT_TRANSPORT
    if args.json:
        emit_json(data)
        return EXIT_OK
    sys.stdout.write("%-24s %-10s %-6s %-22s %s\n"
                     % ("job_id", "state", "exit", "error_code", "request_id"))
    for job in data.get("jobs", []):
        sys.stdout.write("%-24s %-10s %-6s %-22s %s\n"
                         % (job.get("job_id"), job.get("state"),
                            job.get("exit_code"), job.get("error_code"),
                            job.get("request_id")))
    return EXIT_OK


def cmd_doctor(args) -> int:
    try:
        data = api("/doctor", timeout=max(CONNECT_TIMEOUT * 4, 60.0))
    except TransportError as exc:
        if args.json:
            emit_json({"transport_ok": False, "error_code": exc.code,
                       "detail": str(exc)})
        else:
            sys.stderr.write("wl: %s\n" % exc)
        return EXIT_TRANSPORT
    if args.json:
        emit_json({"transport_ok": True, "client": client_info(), "agent": data})
        return EXIT_OK
    rt = data.get("runtime", {})
    shell = data.get("shells", {})
    paths = data.get("paths", {})
    timeouts = data.get("timeouts", {})
    jobs_info = data.get("jobs", {})
    sys.stdout.write("bridge     : OK  agent %s on %s\n"
                     % (rt.get("agent_version"), rt.get("host")))
    sys.stdout.write("windows    : %s\\%s  python %s  admin=%s\n"
                     % (rt.get("host"), rt.get("user"), rt.get("python"), rt.get("admin")))
    for name in ("powershell", "pwsh", "cmd"):
        entry = shell.get(name) or {}
        sys.stdout.write("%-10s: %s %s\n"
                         % (name, "yes" if entry.get("available") else "no",
                            entry.get("version") or entry.get("path") or ""))
    sys.stdout.write("bridge dir : %s\n" % paths.get("bridge_root_for_fs"))
    sys.stdout.write("allowed fs : %s\n" % ", ".join(paths.get("fs_allowed_roots") or []))
    sys.stdout.write("jobs       : %s total, %s active, dir %s\n"
                     % (jobs_info.get("total"), jobs_info.get("active"),
                        paths.get("jobs")))
    sys.stdout.write("timeouts   : job default %ss, sync wait %ss, capture cap %s bytes\n"
                     % (timeouts.get("run_default_s"), timeouts.get("sync_wait_ceiling_s"),
                        timeouts.get("capture_cap_bytes")))
    sys.stdout.write("client     : port %d, connect %.0fs, wait margin %.0fs\n"
                     % (PORT, CONNECT_TIMEOUT, WAIT_MARGIN))
    return EXIT_OK


def client_info() -> dict:
    return {"python": sys.version.split()[0], "port": PORT,
            "connect_timeout_s": CONNECT_TIMEOUT, "wait_margin_s": WAIT_MARGIN,
            "has_credentials": bool(TOKEN)}


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wl_job", add_help=True)
    sub = parser.add_subparsers(dest="action")

    common_json = dict(dest="json", action="store_true", help="machine readable output")

    run = sub.add_parser("run", help="start a job and wait for it")
    run.add_argument("--shell", default="auto", choices=["auto", "powershell", "pwsh", "cmd", "none"])
    run.add_argument("--timeout", type=float, default=120.0, help="execution timeout on Windows (s)")
    run.add_argument("--wait", type=float, default=0.0, help="client wait (s); default timeout+margin")
    run.add_argument("--no-wait", action="store_true", help="start only, print the job id")
    run.add_argument("--output", default="text", choices=["text", "json", "summary"])
    run.add_argument("--request-id", default="", help="retry-safe id; repeats return the same job")
    run.add_argument("--resource", default="", help="serialise on a resource key, e.g. serial:COM139")
    run.add_argument("--cwd", default="")
    run.add_argument("--env", action="append", default=[], metavar="K=V")
    run.add_argument("--stdin-text", default=None)
    run.add_argument("--exe", default="", help="run this program with argv after --")
    run.add_argument("--json", action="store_true", help="print the raw JSON result")
    run.add_argument("command", nargs=argparse.REMAINDER)

    status = sub.add_parser("status")
    status.add_argument("job_id")
    status.add_argument("--wait", type=float, default=0.0)
    status.add_argument("--json", action="store_true")

    logs = sub.add_parser("logs")
    logs.add_argument("job_id")
    logs.add_argument("--since", type=int, default=0, help="byte cursor from a previous call")
    logs.add_argument("--stream", default="stdout", choices=["stdout", "stderr"])
    logs.add_argument("--max-bytes", type=int, default=65536)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--max-polls", type=int, default=600)
    logs.add_argument("--json", action="store_true")

    result = sub.add_parser("result")
    result.add_argument("job_id")
    result.add_argument("--output", default="text", choices=["text", "json", "summary"])
    result.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("job_id")
    cancel.add_argument("--note", default="")
    cancel.add_argument("--json", action="store_true")

    listing = sub.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--request-id", default="")
    listing.add_argument("--json", action="store_true")

    doc = sub.add_parser("doctor")
    doc.add_argument("--json", action="store_true")

    batch = sub.add_parser("batch")
    batch.add_argument("--file", default="", help="plan JSON path; default reads stdin")
    batch.add_argument("--request-id", default="", help="prefix used for per-step dedupe")
    batch.add_argument("--timeout", type=float, default=120.0)
    batch.add_argument("--wait", type=float, default=0.0)
    batch.add_argument("--json", action="store_true")

    return parser


def main(argv: list) -> int:
    # `wl run --shell cmd "echo hi"`: argparse would treat --shell as the
    # program's own flag unless we split our options from the command line.
    if argv and argv[0] == "run":
        argv = _split_run(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "action", None):
        parser.print_help()
        return EXIT_USAGE

    handlers = {
        "run": cmd_run,
        "status": cmd_status,
        "logs": cmd_logs,
        "result": cmd_result,
        "cancel": cmd_cancel,
        "list": cmd_list,
        "doctor": cmd_doctor,
        "batch": cmd_batch,
    }
    handler = handlers[args.action]
    try:
        return handler(args)
    except TransportError as exc:
        sys.stderr.write("wl: %s\n" % exc)
        return EXIT_TRANSPORT


RUN_OPTS_WITH_VALUE = {"--shell", "--timeout", "--wait", "--output", "--request-id",
                       "--resource", "--cwd", "--env", "--stdin-text", "--exe"}


def _split_run(items: list) -> list:
    """Collects our flags first, then a literal `--` separator, then the command."""
    ours = []
    command = []
    index = 0
    while index < len(items):
        item = items[index]
        if item == "--":
            command = items[index + 1:]
            break
        if not command:
            if item in RUN_OPTS_WITH_VALUE:
                ours.append(item)
                if index + 1 < len(items):
                    ours.append(items[index + 1])
                    index += 2
                    continue
                index += 1
                continue
            if item in ("--no-wait", "--json"):
                ours.append(item)
                index += 1
                continue
        command.append(item)
        index += 1
    # A shell flag with a string command is the legacy `wl run --shell cmd "..."`.
    if command and command[0] == "--":
        command = command[1:]
    return ["run"] + ours + (["--"] + command if command else [])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
