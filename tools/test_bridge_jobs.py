"""Offline regressions for bridge execution, output and client semantics."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = load_module("bridge_client", ROOT / "linux" / "wl_job.py")
jobs = load_module("bridge_jobs", ROOT / "win" / "winbridge_jobs.py")


class OutputTests(unittest.TestCase):
    def test_summary_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "stdout.log"
            for size in (0, 3, 4, 5, 7, 8, 9, 20):
                with self.subTest(size=size):
                    raw = b"x" * size
                    path.write_bytes(raw)
                    result = jobs._summarize(str(path), 4, 4)
                    if size <= 8:
                        self.assertEqual(result["text"], raw.decode())
                        self.assertEqual(result["omitted_bytes"], 0)
                    else:
                        self.assertEqual(result["omitted_bytes"], size - 8)

    def test_capture_cap_is_enforced_after_subsequent_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "stdout.log"
            capture = jobs._Capture(str(path), limit=4)
            try:
                capture.fh.write(b"abcdefgh")
                capture.enforce_cap()
                capture.fh.write(b"ijklmnop")
            finally:
                capture.close()
            self.assertEqual(path.stat().st_size, 4)
            self.assertTrue(capture.truncated)

    def test_capture_cap_leaves_no_nul_padding(self):
        """Repeated cap enforcement must not create a sparse NUL-filled gap."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "stdout.log"
            capture = jobs._Capture(str(path), limit=4)
            try:
                for chunk in (b"abcdefgh", b"ijkl", b"mnopqrst"):
                    capture.fh.write(chunk)
                    capture.enforce_cap()
            finally:
                capture.close()
            raw = path.read_bytes()
            self.assertEqual(raw, b"abcd")
            self.assertNotIn(b"\x00", raw)

    def test_read_log_reports_cursor_reset(self):
        manager = object.__new__(jobs.JobManager)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "stdout.log"
            path.write_bytes(b"short\n")
            with mock.patch.object(manager, "job_dir", return_value=directory), \
                    mock.patch.object(manager, "_stdout_path", return_value=str(path)):
                info = manager.read_log("j-test", "stdout", since=9999, max_bytes=64)
            self.assertTrue(info["reset"])
            self.assertEqual(info["from"], 0)
            self.assertEqual(info["text"], "short\n")

    def test_read_log_reports_no_reset_below_total(self):
        manager = object.__new__(jobs.JobManager)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "stdout.log"
            path.write_bytes(b"abcdef\n")
            with mock.patch.object(manager, "job_dir", return_value=directory), \
                    mock.patch.object(manager, "_stdout_path", return_value=str(path)):
                info = manager.read_log("j-test", "stdout", since=3, max_bytes=64)
            self.assertFalse(info["reset"])
            self.assertEqual(info["text"], "def\n")

    def test_result_distinguishes_capture_loss_from_summary(self):
        manager = object.__new__(jobs.JobManager)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "stdout.log"
            path.write_bytes(b"small\n")
            with mock.patch.object(manager, "status", return_value={
                "state": "SUCCEEDED", "stdout_truncated": True,
            }), mock.patch.object(manager, "_stdout_path", return_value=str(path)), \
                    mock.patch.object(manager, "_stderr_path", return_value=str(path)):
                result = manager.read_result("j-test")
            self.assertTrue(result["stdout"]["capture_truncated"])
            self.assertTrue(result["stdout"]["truncated"])
            self.assertEqual(result["stdout"]["omitted_bytes"], 0)

    def test_text_warns_about_truncation_without_polluting_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            client.render_result({
                "job_id": "j-test",
                "stdout": {"text": "data\n", "capture_truncated": True,
                           "truncated": True, "path": "stdout.log"},
            }, "text")
        self.assertEqual(out.getvalue(), "data\n")
        self.assertIn("truncated", err.getvalue())


class ClientTests(unittest.TestCase):
    def run_client(self, argv, responder):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(client, "api", side_effect=responder), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = client.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_structured_none_preserves_arguments(self):
        command = [r"C:\Program Files\tool.exe", 'a "quoted" b', "&|<>^%", ""]
        payloads = []

        def responder(path, payload, **kwargs):
            payloads.append(payload)
            return {"job_id": "j-test", "state": "SUCCEEDED", "completed": True}

        rc, _, _ = self.run_client(["run", "--shell", "none", "--"] + command, responder)
        self.assertEqual(rc, 0)
        self.assertEqual(payloads[0]["cmd"], command)
        with tempfile.TemporaryDirectory() as directory:
            argv, _, _ = jobs.build_argv(payloads[0], directory)
        self.assertEqual(argv, command)

    def test_explicit_exe_preserves_arguments(self):
        payloads = []

        def responder(path, payload, **kwargs):
            payloads.append(payload)
            return {"job_id": "j-test"}

        rc, _, _ = self.run_client(
            ["run", "--no-wait", "--exe", "tool.exe", "--", "", "&", 'a"b'], responder)
        self.assertEqual(rc, 0)
        self.assertEqual(payloads[0]["argv"], ["", "&", 'a"b'])

    def test_shell_script_stays_literal(self):
        payloads = []

        def responder(path, payload, **kwargs):
            payloads.append(payload)
            return {"job_id": "j-test"}

        script = 'echo "a & b"'
        self.run_client(["run", "--no-wait", "--shell", "cmd", script], responder)
        self.assertEqual(payloads[0]["cmd"], script)

    def test_legacy_none_command_line_is_not_parsed_as_posix(self):
        script = r'"C:\Program Files\tool.exe" "space value"'
        with tempfile.TemporaryDirectory() as directory:
            argv, _, shell = jobs.build_argv({"shell": "none", "cmd": script}, directory)
        self.assertEqual(argv, script)
        self.assertEqual(shell, "none")

    def test_result_exit_codes(self):
        for state, code in (("SUCCEEDED", 0), ("FAILED", 7), ("SPAWN_ERROR", 126),
                            ("CANCELLED", 130), ("RUNNING", 124)):
            with self.subTest(state=state):
                rc, _, _ = self.run_client(["result", "j-test", "--json"],
                                          lambda *a, **k: {"state": state, "exit_code": 7})
                self.assertEqual(rc, code)

    def test_client_wait_includes_start_request(self):
        clock = [10.0]
        deadlines = []

        def responder(path, payload, **kwargs):
            clock[0] += 3.0
            return {"job_id": "j-test", "state": "RUNNING"}

        def wait_for(job_id, deadline):
            deadlines.append(deadline)
            return {"state": "RUNNING"}

        with mock.patch.object(client.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(client, "wait_for", side_effect=wait_for):
            rc, _, _ = self.run_client(["run", "--wait", "3", "sleep"], responder)
        self.assertEqual(rc, 124)
        self.assertEqual(deadlines, [13.0])

    def test_poll_request_respects_remaining_budget(self):
        with mock.patch.object(client.time, "monotonic", side_effect=[10.0, 11.0]), \
                mock.patch.object(client, "api", return_value={"state": "RUNNING"}) as api:
            client.wait_for("j-test", 10.1)
        self.assertLessEqual(api.call_args.kwargs["timeout"], 0.11)

    def test_no_wait_requires_job_id(self):
        rc, _, err = self.run_client(["run", "--no-wait", "echo"], lambda *a, **k: {})
        self.assertEqual(rc, 125)
        self.assertIn("job_id", err)

    def test_result_fetch_failure_stays_json(self):
        count = [0]

        def responder(path, payload, **kwargs):
            count[0] += 1
            if count[0] == 1:
                return {"job_id": "j-test"}
            raise client.TransportError("connection lost")

        with mock.patch.object(client, "wait_for", return_value={"state": "SUCCEEDED"}):
            rc, out, _ = self.run_client(["run", "--json", "echo"], responder)
        self.assertEqual(rc, 125)
        self.assertEqual(json.loads(out)["job_id"], "j-test")

    def test_output_json_reports_transport_failure_as_json(self):
        rc, out, _ = self.run_client(
            ["run", "--output", "json", "echo"],
            mock.Mock(side_effect=client.TransportError("disconnected")))
        self.assertEqual(rc, 125)
        self.assertFalse(json.loads(out)["transport_ok"])

    def test_batch_keeps_job_id_after_disconnect(self):
        plan = {"steps": [{"cmd": "echo test"}]}
        with mock.patch.object(client.sys, "stdin", io.StringIO(json.dumps(plan))), \
                mock.patch.object(client, "wait_for",
                                  side_effect=client.TransportError("disconnected")):
            rc, out, _ = self.run_client(["batch"], lambda *a, **k: {"job_id": "j-test"})
        self.assertEqual(rc, 125)
        self.assertEqual(json.loads(out)["steps"][0]["job_id"], "j-test")

    def test_batch_wait_includes_start_request(self):
        clock = [10.0]
        plan = {"steps": [{"cmd": "echo test"}]}
        deadlines = []

        def responder(*args, **kwargs):
            clock[0] += 3
            return {"job_id": "j-test"}

        def wait_for(job_id, deadline):
            deadlines.append(deadline)
            return {"state": "RUNNING"}

        with mock.patch.object(client.sys, "stdin", io.StringIO(json.dumps(plan))), \
                mock.patch.object(client.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(client, "wait_for", side_effect=wait_for):
            rc, _, _ = self.run_client(["batch", "--wait", "3"], responder)
        self.assertEqual(rc, 124)
        self.assertEqual(deadlines, [13.0])

    def test_status_wait_reports_expired_budget_as_124(self):
        rc, _, err = self.run_client(
            ["status", "j-test", "--wait", "1"],
            lambda *a, **k: {"job_id": "j-test", "state": "RUNNING"})
        self.assertEqual(rc, 124)
        self.assertIn("unknown", err)

    def test_status_without_wait_stays_zero_while_running(self):
        rc, out, _ = self.run_client(
            ["status", "j-test"], lambda *a, **k: {"job_id": "j-test", "state": "RUNNING"})
        self.assertEqual(rc, 0)
        self.assertIn("RUNNING", out)

    def test_api_rejects_error_envelopes(self):
        for data in ({"error": "serial unavailable"}, {"ok": False}, [], None):
            with self.subTest(data=data):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(data).encode()
                with mock.patch.object(client, "TOKEN", "test-token"), \
                        mock.patch.object(client.urllib.request, "urlopen", return_value=response):
                    with self.assertRaises(client.TransportError):
                        client.api("/job/start", {})

    def test_api_does_not_interpret_vendor_stdout_as_bridge_error(self):
        data = {"state": "SUCCEEDED", "stdout": {"text": '{"ok": false, "error": "device"}'}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(data).encode()
        with mock.patch.object(client, "TOKEN", "test-token"), \
                mock.patch.object(client.urllib.request, "urlopen", return_value=response):
            self.assertEqual(client.api("/job/result", {}), data)


class ClockTests(unittest.TestCase):
    """Deadlines must survive a wall-clock jump; only timestamps may use it."""

    def test_module_deadlines_never_use_wall_clock(self):
        source = (ROOT / "win" / "winbridge_jobs.py").read_text(encoding="utf-8")
        offenders = [line.strip() for line in source.splitlines()
                     if "time.time()" in line and not line.strip().startswith("#")]
        self.assertEqual(offenders, [])

    def test_status_wait_survives_a_clock_jump(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = jobs.JobManager(directory)
            try:
                job_id = "j20000101_000000aaaaaaaa"
                os.makedirs(manager.job_dir(job_id), exist_ok=True)
                manager._meta[job_id] = {"job_id": job_id, "state": "RUNNING",
                                         "last_activity": "2024-01-01T00:00:00"}
                # A wall clock that keeps jumping forward, like an NTP correction
                # storm. Deadlines must not observe it: the pre-fix code compared
                # against time.time(), so the budget looked expired and the 0.3 s
                # long-poll returned after 0.0 s.
                jumps = {"n": 0}

                def jumping_clock():
                    jumps["n"] += 1
                    return time.perf_counter() + 3600.0 * jumps["n"]

                with mock.patch.object(jobs.time, "time", side_effect=jumping_clock):
                    began = time.monotonic()
                    info = manager.status(job_id, wait=0.3)
                    elapsed = time.monotonic() - began
                self.assertEqual(info["state"], "RUNNING")
                self.assertGreaterEqual(elapsed, 0.2)
                self.assertLess(elapsed, 2.0)
            finally:
                manager.close()


@unittest.skipUnless(os.name == "nt", "Windows CreateProcess integration")
class WindowsExecutionTests(unittest.TestCase):
    def test_real_process_round_trip_and_capture_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = jobs.JobManager(directory)
            try:
                literals = ["space value", 'a"b', "&|<>^%", "", "C:\\"]
                code = "import json,sys; print(json.dumps(sys.argv[1:])); sys.exit(7)"
                for payload in (
                    {"exe": sys.executable, "argv": ["-c", code] + literals},
                    {"cmd": [sys.executable, "-c", code] + literals, "shell": "none"},
                    {"cmd": jobs.subprocess.list2cmdline(
                        [sys.executable, "-c", code] + literals), "shell": "none"},
                ):
                    info = manager.start(dict(payload, timeout=5))
                    job_id = info["job_id"]
                    final = manager.wait_terminal(job_id, 10)
                    self.assertEqual(final["state"], "FAILED")
                    result = manager.read_result(job_id)
                    self.assertEqual(json.loads(result["stdout"]["text"]), literals)
                    self.assertEqual(client.exit_code_for(result), 7)

                capture_class = jobs._Capture
                with mock.patch.object(jobs, "_Capture",
                                       side_effect=lambda path: capture_class(path, limit=64)):
                    info = manager.start({"exe": sys.executable,
                                          "argv": ["-c", "print('x' * 256)"], "timeout": 5})
                    job_id = info["job_id"]
                    final = manager.wait_terminal(job_id, 10)
                    self.assertEqual(final["state"], "SUCCEEDED")
                    result = manager.read_result(job_id)
                    self.assertEqual(result["stdout"]["bytes"], 64)
                    self.assertTrue(result["stdout"]["capture_truncated"])
                    self.assertEqual(result["stdout"]["omitted_bytes"], 0)
                    with open(manager._meta_path(job_id), encoding="utf-8") as fh:
                        self.assertTrue(json.load(fh)["stdout_truncated"])
            finally:
                # Terminal metadata is published just before worker handles close.
                deadline = time.monotonic() + 5
                while manager._stop_flags and time.monotonic() < deadline:
                    time.sleep(0.01)
                manager.close()


if __name__ == "__main__":
    unittest.main()
