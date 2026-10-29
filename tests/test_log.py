"""Tests for http_record_replay.log against a local HTTP server (no network, no cost).

Run from the repo root (libraries that aren't installed skip their tests):
    python -m unittest discover -s tests -v
"""

import asyncio
import gzip
import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
import support

from http_record_replay import log

try:
    import httpx2
except ImportError:
    httpx2 = None
try:
    import httpx
except ImportError:
    httpx = None
try:
    import aiohttp
except ImportError:
    aiohttp = None


def setUpModule():
    support.isolate_settings()


CHAT_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    "access_token": "secret-token-value",
}
# Like a gpt-image reply: about 2 MB of base64 in one field, more than MAX_CAPTURE, with
# fields after it.
BIG_JSON_REPLY = {"data": [{"b64_json": "A" * 2_000_000}], "usage": {"input_tokens": 19, "output_tokens": 196}}
SSE_EVENTS = [
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo"}}]},
    {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, body, content_type, headers=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/gzip"):
            body = gzip.compress(json.dumps(CHAT_REPLY).encode())
            self.send_body(body, "application/json", {"Content-Encoding": "gzip"})
        elif self.path.startswith("/image"):
            self.send_body(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4, "image/png")
        elif self.path.startswith("/long"):
            self.send_body(b"x" * 5000, "text/plain")
        elif self.path.startswith("/big-json"):
            self.send_body(json.dumps(BIG_JSON_REPLY).encode(), "application/json")
        elif self.path.startswith("/sse"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in SSE_EVENTS:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
        elif self.path.startswith("/run"):
            self.send_body(json.dumps({"id": "run_1", "status": "in_progress"}).encode(), "application/json")
        else:
            self.send_body(json.dumps(CHAT_REPLY).encode(), "application/json")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_body(json.dumps(CHAT_REPLY).encode(), "application/json")


class LogTestCase(unittest.TestCase):
    """Starts the local server and turns logging on for each test."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        log.disable()

    def setUp(self):
        log.enable(level="full", metrics=False, max_body=log.DEFAULT_MAX_BODY, polls="short")

    def tearDown(self):
        log.disable()

    def logged(self, call):
        """Run call() and return everything http_log wrote."""
        with self.assertLogs("http_log", level="INFO") as captured:
            result = call()
        return "\n".join(captured.output), result


class RequestsLogTest(LogTestCase):
    def test_requests_post_redacts_secrets_and_pretty_prints(self):
        text, response = self.logged(
            lambda: requests.post(
                f"{self.base}/chat?api-version=1&code=function-key",
                json={"messages": [{"role": "user", "content": "Hi"}]},
                headers={"Authorization": "Bearer real-token", "api-key": "real-key"},
                timeout=10,
            )
        )
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "Hello")
        self.assertIn("--- OUTBOUND REQUEST #", text)
        self.assertIn("> POST " + self.base + "/chat?api-version=1&code=<redacted>", text)
        self.assertIn("> Authorization: Bearer <redacted>", text)
        self.assertIn("> api-key: <redacted>", text)
        self.assertIn('"content": "Hi"', text)
        self.assertIn("< 200 OK", text)
        self.assertIn('"access_token": "<redacted>"', text)
        for secret in ("real-token", "real-key", "function-key", "secret-token-value"):
            self.assertNotIn(secret, text)

    def test_requests_raw_gzip_stream_is_decoded(self):
        # azure-core reads with stream=True and raw.read(decode_content=False).
        def call():
            response = requests.get(f"{self.base}/gzip", stream=True, timeout=10)
            chunks = []
            while chunk := response.raw.read(64, decode_content=False):
                chunks.append(chunk)
            return gzip.decompress(b"".join(chunks))

        text, body = self.logged(call)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "Hello")
        self.assertIn('"content": "Hello"', text)

    def test_binary_and_long_bodies_are_shortened(self):
        log.enable(max_body=100)
        text, _ = self.logged(lambda: requests.get(f"{self.base}/image", timeout=10))
        self.assertIn("<binary body, 1.0 KB, image/png>", text)
        text, response = self.logged(lambda: requests.get(f"{self.base}/long", timeout=10))
        self.assertEqual(len(response.text), 5000)
        self.assertIn("x" * 100 + "...more...", text)
        self.assertNotIn("x" * 101, text)

    def test_summary_level_with_metrics(self):
        log.enable(level="summary", metrics=True)
        text, _ = self.logged(lambda: requests.get(f"{self.base}/chat", timeout=10))
        self.assertNotIn("OUTBOUND REQUEST", text)
        self.assertRegex(
            text, r"HTTP #\d+ GET .*/chat -> 200 OK  \(\d+\.\d\d s, sent 0 B, received .*tokens 12 in / 3 out\)"
        )

    def test_metrics_off_by_default(self):
        text, _ = self.logged(lambda: requests.get(f"{self.base}/chat", timeout=10))
        self.assertNotIn("[metrics]", text)

    def test_enable_twice_logs_once(self):
        log.enable()
        text, _ = self.logged(lambda: requests.get(f"{self.base}/chat", timeout=10))
        self.assertEqual(text.count("--- OUTBOUND REQUEST"), 1)

    def test_off_level_logs_nothing(self):
        log.enable(level="off")
        with self.assertNoLogs("http_log"):
            requests.get(f"{self.base}/chat", timeout=10)

    def test_repeated_get_is_a_short_status_line(self):
        def poll():
            for _ in range(3):
                requests.get(f"{self.base}/run/run_1", timeout=10)

        text, _ = self.logged(poll)
        self.assertEqual(text.count("--- OUTBOUND REQUEST"), 1)  # only the first GET in full
        self.assertEqual(text.count('"status": "in_progress"'), 1)
        short_lines = re.findall(r"HTTP #\d+ GET .*/run/run_1 -> 200 OK, status: in_progress", text)
        self.assertEqual(len(short_lines), 2)

    def test_repeated_get_in_full_when_asked(self):
        log.enable(polls="full")
        text, _ = self.logged(lambda: [requests.get(f"{self.base}/run/run_1", timeout=10) for _ in range(2)])
        self.assertEqual(text.count("--- OUTBOUND REQUEST"), 2)
        self.assertNotIn(", status: in_progress", text)

    def test_connection_error_is_logged(self):
        with self.assertLogs("http_log") as captured, self.assertRaises(requests.ConnectionError):
            requests.get("http://127.0.0.1:9/nothing", timeout=2)
        self.assertIn("--- OUTBOUND ERROR #", "\n".join(captured.output))


@unittest.skipIf(httpx2 is None and httpx is None, "neither httpx2 nor httpx is installed")
class HttpxLogTest(LogTestCase):
    """The same transport hooks for every installed httpx flavour (the openai SDK uses
    httpx2)."""

    def clients(self):
        return [module for module in (httpx2, httpx) if module is not None]

    def test_json_body_over_the_capture_limit_is_shortened_by_field(self):
        # A JSON body is kept whole (up to MAX_JSON_CAPTURE), so its long strings are
        # shortened one by one and the fields after them still show, instead of the raw
        # text being cut in the middle of the base64.
        for module in self.clients():
            with self.subTest(module=module.__name__), module.Client() as client:
                url = f"{self.base}/big-json?client={module.__name__}"
                text, response = self.logged(lambda url=url: client.get(url))
                self.assertEqual(len(response.json()["data"][0]["b64_json"]), 2_000_000)
                self.assertIn("...(2000000 characters)", text)
                self.assertIn('"output_tokens": 196', text)
                self.assertNotIn("bytes received, first", text)
                self.assertLess(len(text), 5000)

    def test_sync_gzip_json(self):
        for module in self.clients():
            with self.subTest(module=module.__name__), module.Client() as client:
                # A query per client: a second GET of the same URL would be logged as a
                # status check.
                url = f"{self.base}/gzip?client={module.__name__}"
                text, response = self.logged(lambda url=url: client.get(url, headers={"Cookie": "a=b"}))
                self.assertEqual(response.json()["usage"]["completion_tokens"], 3)
                self.assertIn('"content": "Hello"', text)
                self.assertIn("> Cookie: <redacted>", text)

    def test_streamed_events_pass_through_and_are_logged_at_the_end(self):
        log.enable(metrics=True)
        for module in self.clients():
            with self.subTest(module=module.__name__), module.Client() as client:

                def call(module=module):
                    with client.stream("GET", f"{self.base}/sse?client={module.__name__}") as response:
                        return [line for line in response.iter_lines() if line.startswith("data:")]

                text, lines = self.logged(call)
                self.assertEqual(len(lines), 4)
                self.assertIn('data: {"choices": [{"delta": {"content": "Hel"}}]}', text)
                self.assertIn("tokens 7 in / 2 out", text)

    def test_async_post(self):
        for module in self.clients():
            with self.subTest(module=module.__name__):

                async def call(module=module):
                    async with module.AsyncClient() as client:
                        return await client.post(f"{self.base}/chat", json={"input": "Hi"})

                text, response = self.logged(lambda: asyncio.run(call()))
                self.assertEqual(response.status_code, 200)
                self.assertIn('"input": "Hi"', text)
                self.assertIn("--- OUTBOUND RESPONSE #", text)


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class AiohttpLogTest(LogTestCase):
    """aiohttp, the transport of the async azure-* SDK clients."""

    def run_session(self, call, **session_args):
        async def main():
            async with aiohttp.ClientSession(**session_args) as session:
                return await call(session)

        return self.logged(lambda: asyncio.run(main()))

    def test_post_body_and_secret_headers(self):
        async def call(session):
            async with session.post(
                f"{self.base}/chat?api-key=real-key",
                data=b'{"input": "Hi"}',  # bytes with a JSON content type, as azure-core sends them
                headers={"api-key": "real-key", "Content-Type": "application/json"},
            ) as response:
                return await response.json()

        text, reply = self.run_session(call)
        self.assertEqual(reply["usage"]["prompt_tokens"], 12)
        self.assertIn("> POST " + self.base + "/chat?api-key=<redacted>", text)
        self.assertIn("> api-key: <redacted>", text)
        self.assertIn('"input": "Hi"', text)
        self.assertIn('"access_token": "<redacted>"', text)
        self.assertNotIn("real-key", text)
        self.assertNotIn("secret-token-value", text)

    def test_gzip_without_automatic_decompression_is_decoded(self):
        # azure-core makes its sessions with auto_decompress=False and reads the
        # compressed bytes with read().
        async def call(session):
            response = await session.request("GET", f"{self.base}/gzip")
            body = await response.read()
            response.release()
            return gzip.decompress(body)

        text, body = self.run_session(call, auto_decompress=False)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "Hello")
        self.assertIn('"content": "Hello"', text)
        self.assertEqual(text.count("--- OUTBOUND RESPONSE #"), 1)

    def test_body_read_as_a_stream_is_noted(self):
        async def call(session):
            async with session.get(f"{self.base}/sse") as response:
                return [line async for line in response.content]

        text, lines = self.run_session(call)
        self.assertEqual(len([line for line in lines if line.startswith(b"data:")]), 4)
        self.assertIn("< 200 OK", text)
        self.assertIn("body not read with read(): not logged", text)

    def test_connection_error_is_logged(self):
        async def main():
            async with aiohttp.ClientSession() as session:
                await session.get("http://127.0.0.1:9/nothing")

        with self.assertLogs("http_log") as captured, self.assertRaises(aiohttp.ClientConnectionError):
            asyncio.run(main())
        self.assertIn("--- OUTBOUND ERROR #", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
