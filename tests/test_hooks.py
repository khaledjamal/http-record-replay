"""Tests for the shared transport hooks (http_record_replay._hooks) and the libraries
they cover beyond the log and replay tests: urllib3 called directly and botocore (the
AWS SDK), against a local HTTP server (no network, no cost).

Run from the repo root (libraries that aren't installed skip their tests):
    python -m unittest discover -s tests -v
"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import support
import urllib3

import http_record_replay
from http_record_replay import _hooks, log, replay

try:
    import botocore.session
except ImportError:
    botocore = None

TABLES_REPLY = {"TableNames": ["orders", "customers"]}
STREAM_BODY = b"line\n" * 20_000  # 100 KB, read in pieces


def setUpModule():
    support.isolate_settings()


class Handler(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *args):
        pass

    def send_body(self, body, content_type):
        type(self).hits += 1
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/stream"):
            self.send_body(STREAM_BODY, "text/plain")
        else:
            self.send_body(json.dumps({"status": "ok", "path": self.path}).encode(), "application/json")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("X-Amz-Target", "").startswith("DynamoDB_"):
            self.send_body(json.dumps(TABLES_REPLY).encode(), "application/x-amz-json-1.0")
        else:
            self.send_body(b'{"echo": true}', "application/json")


class CountingObserver(_hooks.Observer):
    """Collects what the observers are told."""

    def __init__(self):
        self.requests, self.responses = [], []

    def capture_limit(self, exchange):
        return float("inf")

    def on_request(self, exchange):
        self.requests.append(exchange.url)

    def on_response(self, exchange, body, note):
        self.responses.append((exchange.url, body, note))


class HooksTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="http-replay-test-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def tearDown(self):
        http_record_replay.disable()

    def observe(self, calls):
        observer = CountingObserver()
        _hooks.add_observer(observer)
        try:
            result = calls()
        finally:
            _hooks.remove_observer(observer)
        return observer, result

    def record_then_replay(self, calls):
        """(recorded result, replayed result); the server must get no request on
        replay."""
        replay.install(mode="record", directory=self.folder)
        try:
            recorded = calls()
        finally:
            http_record_replay.disable()
        hits = Handler.hits
        replay.install(mode="replay", directory=self.folder)
        try:
            replayed = calls()
        finally:
            http_record_replay.disable()
        self.assertEqual(Handler.hits, hits, "the server was called during replay")
        return recorded, replayed

    def recording_text(self):
        return "\n".join(path.read_text() for path in sorted(self.folder.glob("*.json")))


class HookLayerTest(HooksTestCase):
    def test_a_requests_call_is_seen_once_although_urllib3_is_hooked_too(self):
        observer, _ = self.observe(lambda: requests.get(f"{self.base}/once", timeout=10))
        self.assertEqual(observer.requests, [f"{self.base}/once"])
        self.assertEqual(len(observer.responses), 1)

    def test_patches_are_in_place_only_while_something_uses_them(self):
        from requests.adapters import HTTPAdapter

        original_send = HTTPAdapter.send
        log.enable(level="summary")
        self.assertIsNot(HTTPAdapter.send, original_send)
        log.disable()
        self.assertIs(HTTPAdapter.send, original_send)

    def test_log_and_recorder_see_the_same_call_number(self):
        replay.install(mode="record", directory=self.folder)
        with self.assertLogs("http_log") as captured:
            log.enable(level="summary")
            requests.get(f"{self.base}/numbered", timeout=10)
        number = int(next(self.folder.glob("*.json")).name[:4])
        self.assertIn(f"HTTP #{number} GET {self.base}/numbered", "\n".join(captured.output))


class Urllib3Test(HooksTestCase):
    def test_preloaded_response_is_seen_recorded_and_replayed(self):
        def calls():
            with urllib3.PoolManager() as pool:
                return pool.request("GET", f"{self.base}/direct?x=1").json()

        observer, result = self.observe(calls)
        self.assertEqual(result["path"], "/direct?x=1")
        self.assertEqual(observer.requests, [f"{self.base}/direct?x=1"])
        self.assertEqual(json.loads(observer.responses[0][1])["status"], "ok")
        recorded, replayed = self.record_then_replay(calls)
        self.assertEqual(replayed, recorded)

    def test_streamed_response_is_kept_whole_as_it_is_read(self):
        def calls():
            with urllib3.PoolManager() as pool:
                response = pool.request("GET", f"{self.base}/stream", preload_content=False)
                body = b"".join(response.stream(4096))
                response.release_conn()
                return body

        observer, body = self.observe(calls)
        self.assertEqual(body, STREAM_BODY)
        self.assertEqual(observer.responses, [(f"{self.base}/stream", STREAM_BODY, None)])
        recorded, replayed = self.record_then_replay(calls)
        self.assertEqual((recorded, replayed), (STREAM_BODY, STREAM_BODY))


@unittest.skipIf(botocore is None, "botocore is not installed")
class BotocoreTest(HooksTestCase):
    def dynamodb(self):
        session = botocore.session.get_session()
        return session.create_client(
            "dynamodb",
            region_name="us-east-1",
            endpoint_url=self.base,
            aws_access_key_id="AKIDEXAMPLE",
            aws_secret_access_key="real-secret-access-key",
            aws_session_token="real-session-token",
        )

    def test_aws_calls_are_recorded_with_secrets_redacted_and_replayed(self):
        def calls():
            return self.dynamodb().list_tables()["TableNames"]

        recorded, replayed = self.record_then_replay(calls)
        self.assertEqual(recorded, ["orders", "customers"])
        self.assertEqual(replayed, recorded)
        text = self.recording_text()
        self.assertIn('"AWS4-HMAC-SHA256 <redacted>"', text)
        for secret in ("real-session-token", "real-secret-access-key", "Signature="):
            self.assertNotIn(secret, text)

    def test_aws_calls_are_logged(self):
        with self.assertLogs("http_log") as captured:
            log.enable(level="full")
            self.dynamodb().list_tables()
        text = "\n".join(captured.output)
        self.assertIn(f"> POST {self.base}/", text)
        self.assertIn("> X-Amz-Target: DynamoDB_20120810.ListTables", text)
        self.assertIn("> X-Amz-Security-Token: <redacted>", text)
        self.assertIn('"orders"', text)
        self.assertNotIn("real-session-token", text)


if __name__ == "__main__":
    unittest.main()
