"""Tests for the runner, `python -m http_record_replay run`: an unchanged script and
module are logged, recorded and replayed. Each run is a real subprocess against a local
HTTP server (no network, no cost).

Run from the repo root:
    python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import support

SCRIPT = """\
import sys

import requests

reply = requests.get(sys.argv[1] + "/greeting", timeout=10).json()
print("argv:", sys.argv[2:], "reply:", reply["text"], "main:", __name__)
sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
"""


def setUpModule():
    support.isolate_settings()


class Handler(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).hits += 1
        body = json.dumps({"text": "hello"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="http-replay-runner-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.script = self.folder / "greet.py"
        self.script.write_text(SCRIPT)
        self.recording = self.folder / "recording"

    def run_program(self, *args, **settings):
        """Run `python -m http_record_replay run <args>` in the test folder; returns
        the finished process."""
        env = {**os.environ, "HTTP_LOG": "summary", **settings}
        command = [sys.executable, "-m", "http_record_replay", "run", *args]
        return subprocess.run(
            command, check=False, cwd=self.folder, env=env, capture_output=True, text=True, timeout=60
        )

    def test_unchanged_script_is_logged_with_its_argv_and_exit_code(self):
        finished = self.run_program(str(self.script), self.base, "3")
        self.assertEqual(finished.returncode, 3, finished.stderr)
        self.assertIn("argv: ['3'] reply: hello main: __main__", finished.stdout)
        self.assertIn(f"HTTP #1 GET {self.base}/greeting -> 200 OK", finished.stdout)

    def test_unchanged_script_is_recorded_then_replayed_without_the_server(self):
        recorded = self.run_program(
            str(self.script), self.base, HTTP_REPLAY="record", HTTP_REPLAY_DIR=str(self.recording)
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(len(list(self.recording.glob("*.json"))), 1)
        hits = Handler.hits
        replayed = self.run_program(
            str(self.script), self.base, HTTP_REPLAY="replay", HTTP_REPLAY_DIR=str(self.recording)
        )
        self.assertEqual(replayed.returncode, 0, replayed.stderr)
        self.assertIn("reply: hello", replayed.stdout)
        self.assertEqual(Handler.hits, hits, "the server was called during replay")

    def test_module_form(self):
        package = self.folder / "tools"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "greet.py").write_text(SCRIPT)
        finished = self.run_program("-m", "tools.greet", self.base)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("reply: hello main: __main__", finished.stdout)
        self.assertIn("HTTP #1 GET", finished.stdout)

    def test_run_needs_a_program(self):
        finished = self.run_program()
        self.assertEqual(finished.returncode, 2)
        self.assertIn("run needs a script", finished.stderr)


if __name__ == "__main__":
    unittest.main()
