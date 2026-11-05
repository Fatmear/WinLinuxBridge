"""Regressions for the linux/wl file transfer subcommands (put/get/rm).

The static tests need nothing but the script itself. The integration tests
start a stand-in agent in-process and drive the real bash script against it,
so a body that never leaves the machine as a single argv word is proven by
transferring a file far larger than MAX_ARG_STRLEN.
"""

import base64
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WL = ROOT / "linux" / "wl"
MAX_FILE = 64 * 1024 * 1024
TOKEN = "test-token"


def find_bash():
    candidates = [os.environ.get("WL_BASH"), r"D:\cygwin64\bin\bash.exe"]
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).exists():
            return candidate
    return None


BASH = find_bash()


class StaticTests(unittest.TestCase):
    """The shape of the script that made large uploads possible."""

    def setUp(self):
        self.text = WL.read_text(encoding="utf-8")

    def test_body_is_not_an_argv_word(self):
        self.assertIn("--data-binary @-", self.text)
        self.assertNotIn('--data-binary "$body"', self.text)

    def test_fs_requests_keep_the_curl_status(self):
        # ls, get, put and rm all run curl outside the pipeline that parses the
        # reply, so "cannot reach" is never replaced by a parse error.
        self.assertEqual(self.text.count('answer=$(api POST /fs "$body")'), 4)
        self.assertEqual(self.text.count("| fs_ack"), 2)
        self.assertNotIn('api POST /fs "$body" | pretty', self.text)
        self.assertNotIn('api POST /fs "$body" | python3 -c', self.text)

    def test_curl_status_survives_the_reply_parser(self):
        self.assertIn('[ "$rc" -eq 0 ] || exit "$rc"', self.text)

    def test_get_handles_local_write_failures(self):
        self.assertIn("os.replace(tmp, dst)", self.text)
        self.assertIn("cannot write %s", self.text)

    def test_output_names_split_windows_paths(self):
        self.assertIn('out="${2:-$(base_name "$1")}"', self.text)
        self.assertIn('dst="${2:-$(base_name "$1")}"', self.text)

    def test_usage_errors_use_the_documented_exit_code(self):
        self.assertIn("usage_die()", self.text)
        self.assertNotIn('die "usage: ', self.text)


class AgentStub(http.server.BaseHTTPRequestHandler):
    root = None
    seen = []

    def log_message(self, *args):
        pass

    def _payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _resolve(self, path):
        target = (self.root / path.replace("\\", "/").lstrip("/")).resolve()
        if self.root not in target.parents and target != self.root:
            raise PermissionError("path is outside the allowed roots")
        return target

    def _reply(self, code, body):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._reply(200, {"host": "stub", "user": "stub", "version": "stub"})

    def do_POST(self):
        try:
            request = self._payload()
            AgentStub.seen.append(request.get("op"))
            op = request.get("op")
            target = self._resolve(request["path"])
            if op == "write":
                data = base64.b64decode(request.get("b64") or "")
                if len(data) > MAX_FILE:
                    raise ValueError("payload too large: %d" % len(data))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                self._reply(200, {"path": str(target), "size": len(data), "ok": True})
            elif op == "read":
                data = target.read_bytes()
                if len(data) > MAX_FILE:
                    raise ValueError("payload too large: %d" % len(data))
                self._reply(200, {"path": str(target),
                                  "b64": base64.b64encode(data).decode("ascii")})
            elif op == "rm":
                if target.is_dir():
                    if request.get("recursive"):
                        shutil.rmtree(target)
                    else:
                        target.rmdir()
                else:
                    target.unlink()
                self._reply(200, {"path": str(target), "ok": True})
            elif op == "stat":
                self._reply(200, {"path": str(target), "size": target.stat().st_size})
            else:
                raise ValueError("unknown op")
        except PermissionError as exc:
            self._reply(403, {"error": "PermissionError", "detail": str(exc)})
        except FileNotFoundError as exc:
            self._reply(404, {"error": "FileNotFoundError", "detail": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._reply(400, {"error": type(exc).__name__, "detail": str(exc)})


@unittest.skipIf(BASH is None, "bash is required for the wl integration tests")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        (self.base / "linux").mkdir()
        (self.base / "config").mkdir()
        shutil.copyfile(WL, self.base / "linux" / "wl")
        self.win_root = (self.base / "win").resolve()
        self.win_root.mkdir()
        AgentStub.root = self.win_root
        AgentStub.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AgentStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port = self.server.server_address[1]
        (self.base / "config" / "bridge.env").write_text(
            "WL_AGENT_TOKEN='%s'\nWL_AGENT_PORT='%d'\n" % (TOKEN, port),
            encoding="utf-8")

    def wl(self, *args):
        return subprocess.run(
            [BASH, str(self.base / "linux" / "wl")] + list(args),
            cwd=str(self.base), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)

    def test_large_upload_arrives_intact(self):
        payload = os.urandom(5 * 1024 * 1024)
        (self.base / "big.bin").write_bytes(payload)
        result = self.wl("put", "big.bin", "big.bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.win_root / "big.bin").read_bytes(), payload)

    def test_oversized_upload_is_refused_locally(self):
        (self.base / "over.bin").write_bytes(b"x" * (MAX_FILE + 1024))
        result = self.wl("put", "over.bin", "over.bin")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the limit for wl put", result.stderr)
        self.assertEqual(AgentStub.seen, [])

    def test_refused_upload_reports_the_error(self):
        (self.base / "s.txt").write_text("hi", encoding="utf-8")
        result = self.wl("put", "s.txt", "../outside.txt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("PermissionError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_rm_of_a_missing_file_reports_the_error(self):
        result = self.wl("rm", "gone.txt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("FileNotFoundError", result.stderr)

    def test_download_creates_the_parent_directory(self):
        (self.win_root / "hello.txt").write_text("hello", encoding="utf-8")
        result = self.wl("get", "hello.txt", "nested/deep/hello.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.base / "nested" / "deep" / "hello.txt").read_text(),
                         "hello")

    def test_download_refuses_a_directory_target(self):
        (self.win_root / "hello.txt").write_text("hello", encoding="utf-8")
        (self.base / "adir").mkdir()
        result = self.wl("get", "hello.txt", "adir")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is a directory", result.stderr)

    def test_download_of_a_missing_file_reports_the_error(self):
        result = self.wl("get", "nope.txt", "nope.txt")
        self.assertEqual(result.returncode, 2)
        self.assertIn("FileNotFoundError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_output_name_comes_from_a_windows_path(self):
        (self.win_root / "dotted.txt").write_text("x", encoding="utf-8")
        result = self.wl("get", "dotted.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.base / "dotted.txt").exists())

    def test_unreachable_agent_is_reported_as_such(self):
        (self.base / "config" / "bridge.env").write_text(
            "WL_AGENT_TOKEN='%s'\nWL_AGENT_PORT='1'\n" % TOKEN, encoding="utf-8")
        (self.base / "s.txt").write_text("hi", encoding="utf-8")
        result = self.wl("put", "s.txt", "s.txt")
        self.assertEqual(result.returncode, 7)
        self.assertIn("cannot reach", result.stderr)
        self.assertNotIn("unexpected answer", result.stderr)

    def test_unreachable_agent_during_download_is_reported_as_such(self):
        (self.base / "config" / "bridge.env").write_text(
            "WL_AGENT_TOKEN='%s'\nWL_AGENT_PORT='1'\n" % TOKEN, encoding="utf-8")
        result = self.wl("get", "hello.txt", "hello.txt")
        self.assertEqual(result.returncode, 7)
        self.assertIn("cannot reach", result.stderr)
        self.assertNotIn("unexpected answer", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
