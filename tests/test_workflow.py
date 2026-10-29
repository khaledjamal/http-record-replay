"""Tests for the workflow commands (`compare`, `replay-all`) and for what a record run
keeps for them (output.txt, the replay command, the exit code). Each program run is a
real subprocess against a local HTTP server (no network, no cost).

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

from http_record_replay import record, workflow

# Prints a value that changes every run (a duration, a UUID) and one that doesn't.
SCRIPT = """\
import sys
import time
import uuid

import requests

started = time.perf_counter()
reply = requests.get(sys.argv[1] + "/greeting", timeout=10).json()
print("greeting:", reply["text"])
print(f"took {time.perf_counter() - started + 1:.2f} s, request {uuid.UUID(int=time.time_ns(), version=4)}")
"""
SCRIPT_WITH_ENABLE = "import http_record_replay\n\nhttp_record_replay.enable()\n" + SCRIPT


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


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="http-replay-workflow-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        (self.folder / "pyproject.toml").write_text("")  # the project root: runs/ goes here
        self.script = self.folder / "greet.py"
        self.script.write_text(SCRIPT)
        self.recordings = self.folder / "runs" / "recordings"

    def python(self, *args, **settings):
        env = {**os.environ, "HTTP_LOG": "summary", **settings}
        command = [sys.executable, *args]
        return subprocess.run(
            command, check=False, cwd=self.folder, env=env, capture_output=True, text=True, timeout=60
        )

    def record_with_runner(self, name):
        recording = self.recordings / name
        finished = self.python(
            "-m",
            "http_record_replay",
            "run",
            "greet.py",
            self.base,
            HTTP_REPLAY="record",
            HTTP_REPLAY_DIR=str(recording),
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return recording

    def test_record_run_keeps_output_replay_command_and_exit_code(self):
        recording = self.record_with_runner("first")
        output = (recording / record.OUTPUT_FILE).read_text()
        self.assertIn("greeting: hello", output)
        self.assertIn(f"HTTP #1 GET {self.base}/greeting -> 200 OK", output)  # the log, too
        info = record.read_info(recording)
        self.assertEqual(info[record.EXIT_CODE_KEY], "0")
        self.assertEqual(
            json.loads(info[record.REPLAY_COMMAND_KEY])[1:5], ["-m", "http_record_replay", "run", "greet.py"]
        )

    def test_compare_passes_when_only_changing_values_differ(self):
        recording = self.record_with_runner("first")
        hits = Handler.hits
        finished = self.python("-m", "http_record_replay", "compare", str(recording), "--strict")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("(no differences in the output)", finished.stdout)
        self.assertIn("PASS  exit 0 (recorded 0)  MISS 0", finished.stdout)
        self.assertEqual(Handler.hits, hits, "the server was called during replay")

    def test_compare_shows_changed_output_and_fails_only_when_strict(self):
        recording = self.record_with_runner("first")
        self.script.write_text(SCRIPT.replace('"greeting:"', '"greeting now:"'))
        finished = self.python("-m", "http_record_replay", "compare", str(recording))
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("-greeting: hello", finished.stdout)
        self.assertIn("+greeting now: hello", finished.stdout)
        strict = self.python("-m", "http_record_replay", "compare", str(recording), "--strict")
        self.assertEqual(strict.returncode, 1)

    def test_compare_fails_on_a_request_the_recording_does_not_have(self):
        recording = self.record_with_runner("first")
        self.script.write_text(SCRIPT.replace('"/greeting"', '"/other"'))
        finished = self.python("-m", "http_record_replay", "compare", str(recording))
        self.assertEqual(finished.returncode, 1)
        self.assertIn(f"MISS: {self.base}/other", finished.stdout)

    def test_a_recording_made_with_enable_replays_too(self):
        self.script.write_text(SCRIPT_WITH_ENABLE)
        recording = self.recordings / "enabled"
        finished = self.python("greet.py", self.base, HTTP_REPLAY="record", HTTP_REPLAY_DIR=str(recording))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertNotIn(record.EXIT_CODE_KEY, record.read_info(recording))  # only the runner knows it
        result = workflow.replay(recording, self.folder / "replay.txt")
        self.assertTrue(result.passed(strict=True), "\n".join(result.diff or []))

    def test_replay_all_replays_the_newest_recording_per_command(self):
        self.record_with_runner("older")
        self.record_with_runner("newer")
        finished = self.python("-m", "http_record_replay", "replay-all")
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("Replaying 1 of 2 recordings", finished.stdout)
        self.assertIn("PASS  exit 0 (recorded 0)  MISS 0  telemetry MISS 0  changed lines 0  newer", finished.stdout)
        every = self.python("-m", "http_record_replay", "replay-all", "--all")
        self.assertIn("2 of 2 passed", every.stdout)
        summaries = list((self.folder / "runs" / "replays").glob("*-replay-all*/summary.txt"))
        self.assertEqual(len(summaries), 2)

    def test_changing_values_are_replaced_before_comparing(self):
        lines = workflow.comparable_lines(
            "took 0.53 s\nat 2026-10-29T21:14:43+04:00\nid 0ac57b06-6e86-4d53-9500-cf6e7d403006\n"
            "[http_replay] replay mode\n> Date: Thu\n> GET https://x/y\n< 200 OK\n< Content-Length: 3"
        )
        self.assertEqual(lines, ["took <duration>", "at <date-time>", "id <uuid>", "> GET https://x/y", "< 200 OK"])


if __name__ == "__main__":
    unittest.main()
