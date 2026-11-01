"""Tests for http_record_replay.replay: record calls to a local HTTP server, then
replay them with the server unused.

No network, no cost. Run from the repo root (libraries that aren't installed skip their
tests):
    python -m unittest discover -s tests -v
"""

import asyncio
import gzip
import json
import os
import random
import socket
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import requests
import support

import http_record_replay
from http_record_replay import log, replay, settings
from http_record_replay.providers import azure as azure_provider

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
try:
    import azure.identity
    import azure.identity.aio
    from azure.identity import DefaultAzureCredential
    from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
except ImportError:
    DefaultAzureCredential = None


def setUpModule():
    support.isolate_settings()


REPLY = {"output_text": "Hello", "access_token": "secret-token-value"}
CONNECTION = {"name": "search", "credentials": {"type": "ApiKey", "key": "real-search-key"}}
APP_INSIGHTS_CONNECTION = {
    "name": "appi",
    "type": "AppInsights",
    "credentials": {"type": "ApiKey", "key": "InstrumentationKey=real-ikey;IngestionEndpoint=https://real.example/"},
}
SSE_BODY = 'data: {"delta": "Hel"}\n\ndata: {"delta": "lo"}\n\ndata: [DONE]\n\n'


class Handler(BaseHTTPRequestHandler):
    hits = 0
    polls = 0

    def log_message(self, *args):
        pass

    def send_body(self, body, content_type, headers=None):
        type(self).hits += 1
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/connection"):
            self.send_body(json.dumps(CONNECTION).encode(), "application/json")
        elif self.path.startswith("/appinsights"):
            self.send_body(json.dumps(APP_INSIGHTS_CONNECTION).encode(), "application/json")
        elif self.path.startswith("/run"):
            type(self).polls += 1
            status = "completed" if self.polls >= 3 else "in_progress"
            self.send_body(json.dumps({"id": "run_1", "status": status}).encode(), "application/json")
        elif self.path.startswith("/gzip"):
            body = gzip.compress(json.dumps(REPLY).encode())
            self.send_body(body, "application/json", {"Content-Encoding": "gzip"})
        elif self.path.startswith("/identity-sse"):
            self.send_body(SSE_BODY.encode(), "text/event-stream", {"Content-Encoding": "identity"})
        elif self.path.startswith("/sse"):
            self.send_body(SSE_BODY.encode(), "text/event-stream")
        else:
            self.send_body(json.dumps(REPLY).encode(), "application/json")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        request = json.loads(body or b"{}") if "json" in self.headers.get("Content-Type", "") else {}
        self.send_body(json.dumps({"echo": request.get("input"), **REPLY}).encode(), "application/json")


class ReplayTestCase(unittest.TestCase):
    """A local server, and a fresh recording folder for each test."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.folder = self.new_folder()
        Handler.hits = Handler.polls = 0

    def new_folder(self):
        folder = tempfile.TemporaryDirectory(prefix="http-replay-test-")
        self.addCleanup(folder.cleanup)
        return Path(folder.name)

    def tearDown(self):
        self.stop()

    def stop(self):
        http_record_replay.disable()

    def record_run(self, calls, ignore_fields=()):
        replay.install(mode="record", directory=self.folder, ignore_fields=ignore_fields)
        log.enable(level="off")
        try:
            return calls()
        finally:
            self.stop()

    def replay_run(self, calls, ignore_fields=(), level="off", ignore_uuids=False):
        """Run calls() in replay mode and check that the server got no request."""
        hits = Handler.hits
        replay.install(mode="replay", directory=self.folder, ignore_fields=ignore_fields, ignore_uuids=ignore_uuids)
        log.enable(level=level)
        try:
            return calls()
        finally:
            self.stop()
            self.assertEqual(Handler.hits, hits, "the server was called during replay")

    def recording_text(self):
        return "\n".join(path.read_text() for path in sorted(self.folder.glob("*.json")))


class RequestsReplayTest(ReplayTestCase):
    def test_record_then_replay_without_the_server(self):
        def calls():
            first = requests.get(f"{self.base}/reply?api-version=1", timeout=10)
            second = requests.post(f"{self.base}/chat", json={"input": "Hi", "model": "m"}, timeout=10)
            return first.json(), second.json()

        recorded = self.record_run(calls)
        self.assertEqual(len(list(self.folder.glob("*.json"))), 2)
        replayed = self.replay_run(calls)
        self.assertEqual(replayed, (_redacted(recorded[0]), _redacted(recorded[1])))
        self.assertEqual(replayed[1]["echo"], "Hi")

    def test_replayed_response_is_marked_and_logged(self):
        self.record_run(lambda: requests.get(f"{self.base}/reply", timeout=10))
        with self.assertLogs("http_log") as captured:
            response = self.replay_run(lambda: requests.get(f"{self.base}/reply", timeout=10), level="full")
        self.assertTrue(response.headers[replay.REPLAY_HEADER].endswith(".json"))
        self.assertIn("< x-http-replay: ", "\n".join(captured.output))

    def test_secrets_are_redacted_and_the_shape_is_kept(self):
        self.record_run(
            lambda: requests.get(
                f"{self.base}/connection?api-key=query-key", headers={"Authorization": "Bearer t0ken"}, timeout=10
            )
        )
        text = self.recording_text()
        for secret in ("real-search-key", "query-key", "t0ken"):
            self.assertNotIn(secret, text)
        replayed = self.replay_run(lambda: requests.get(f"{self.base}/connection?api-key=other", timeout=10).json())
        self.assertEqual(replayed["credentials"], {"type": "ApiKey", "key": "<redacted>"})

    def test_parsed_secrets_get_a_fake_value_in_the_same_format(self):
        self.record_run(lambda: requests.get(f"{self.base}/appinsights", timeout=10))
        self.assertNotIn("real-ikey", self.recording_text())
        replayed = self.replay_run(lambda: requests.get(f"{self.base}/appinsights", timeout=10).json())
        self.assertEqual(replayed["credentials"]["key"], azure_provider.FAKE_SECRETS["AppInsights"])
        self.assertEqual(replayed["name"], "appi")

    def test_fake_secrets_skip_type_fields_that_are_not_names(self):
        # An agent definition echoes its tools' JSON Schema, where `type` may be a list.
        schema = {"type": "object", "properties": {"category": {"type": ["string", "null"]}}}
        self.assertEqual(azure_provider.fill_replayed_json(schema), schema)

    def test_unrecorded_request_fails_and_never_reaches_the_server(self):
        self.record_run(lambda: requests.get(f"{self.base}/reply", timeout=10))
        with self.assertRaises(replay.HttpReplayMiss) as error:
            self.replay_run(lambda: requests.get(f"{self.base}/other", timeout=10))
        self.assertIn("/other", str(error.exception))

    def test_repeated_requests_get_their_answers_in_order(self):
        def poll():
            return [requests.get(f"{self.base}/run/run_1", timeout=10).json()["status"] for _ in range(3)]

        self.assertEqual(self.record_run(poll), ["in_progress", "in_progress", "completed"])

        def poll_once_more():
            return poll() + [requests.get(f"{self.base}/run/run_1", timeout=10)]

        self.assertEqual(self.replay_run(poll), ["in_progress", "in_progress", "completed"])
        with self.assertRaises(replay.HttpReplayMiss) as error:
            self.replay_run(poll_once_more)
        self.assertIn("call 4; 3 recorded", str(error.exception))

    def test_multipart_bodies_match_without_their_random_boundary(self):
        # requests picks a new random boundary for each upload, as azure-core does for
        # file uploads.
        def upload():
            files = {"file": ("notes.md", b"# Notes\n"), "purpose": (None, "assistants")}
            return requests.post(f"{self.base}/files", files=files, timeout=10).status_code

        self.record_run(upload)
        self.assertEqual(self.replay_run(upload), 200)
        with self.assertRaises(replay.HttpReplayMiss):
            self.replay_run(lambda: requests.post(f"{self.base}/files", files={"file": ("other.md", b"x")}, timeout=10))

    def test_json_bodies_match_by_value_and_ignored_fields(self):
        self.record_run(lambda: requests.post(f"{self.base}/chat", json={"input": "Hi", "ts": 1}, timeout=10))
        reordered = '{"ts": 1, "input": "Hi"}'
        headers = {"Content-Type": "application/json"}
        self.replay_run(lambda: requests.post(f"{self.base}/chat", data=reordered, headers=headers, timeout=10))
        with self.assertRaises(replay.HttpReplayMiss):
            self.replay_run(lambda: requests.post(f"{self.base}/chat", json={"input": "Hi", "ts": 2}, timeout=10))
        self.replay_run(
            lambda: requests.post(f"{self.base}/chat", json={"input": "Hi", "ts": 2}, timeout=10),
            ignore_fields=["ts"],
        )


@unittest.skipIf(httpx2 is None and httpx is None, "neither httpx2 nor httpx is installed")
class HttpxReplayTest(ReplayTestCase):
    def clients(self):
        return [module for module in (httpx2, httpx) if module is not None]

    def test_streamed_events_and_async_post(self):
        for module in self.clients():
            with self.subTest(module=module.__name__):
                self.folder = self.new_folder()

                def calls(module=module):
                    with module.Client() as client, client.stream("GET", f"{self.base}/sse") as response:
                        lines = [line for line in response.iter_lines() if line]

                    async def post():
                        async with module.AsyncClient() as client:
                            return (await client.post(f"{self.base}/chat", json={"input": "Hi"})).json()

                    return lines, asyncio.run(post())

                recorded = self.record_run(calls)
                self.assertEqual(len(recorded[0]), 3)
                self.assertEqual(self.replay_run(calls), (recorded[0], _redacted(recorded[1])))

    def test_identity_encoding_keeps_the_body_in_the_recording(self):
        # Content-Encoding: identity means not compressed. The MCP extension of Azure
        # Functions sends it on its event streams.
        for module in self.clients():
            with self.subTest(module=module.__name__):
                self.folder = self.new_folder()

                def calls(module=module):
                    with module.Client() as client, client.stream("GET", f"{self.base}/identity-sse") as response:
                        return [line for line in response.iter_lines() if line]

                recorded = self.record_run(calls)
                self.assertEqual(len(recorded), 3)
                self.assertNotIn("compressed body", self.recording_text())
                self.assertEqual(self.replay_run(calls), recorded)

    def test_replayed_body_is_logged_when_read(self):
        # The log must see a replayed body as the caller reads it, as with a real one,
        # not only at exit.
        for module in self.clients():
            with self.subTest(module=module.__name__):
                self.folder = self.new_folder()

                def calls(module=module):
                    with module.Client() as client:
                        sync_reply = client.get(f"{self.base}/reply").json()

                    async def post():
                        async with module.AsyncClient() as client:
                            return (await client.post(f"{self.base}/chat", json={"input": "Hi"})).json()

                    return sync_reply, asyncio.run(post())

                self.record_run(calls)
                with self.assertLogs("http_log") as captured:
                    self.replay_run(calls, level="full")
                log = "\n".join(captured.output)
                self.assertEqual(log.count("--- OUTBOUND RESPONSE #"), 2)
                self.assertIn('"output_text": "Hello"', log)
                self.assertIn('"echo": "Hi"', log)
                self.assertNotIn("body not read", log)


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class AiohttpReplayTest(ReplayTestCase):
    def test_azure_core_style_reads(self):
        # azure-core: auto_decompress=False, read(), and decompression only when
        # Content-Encoding says so.
        def calls():
            async def main():
                async with aiohttp.ClientSession(auto_decompress=False) as session:
                    response = await session.request("GET", f"{self.base}/gzip")
                    body = await response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    response.release()
                    data = b'{"input": "Hi"}'
                    headers = {"Content-Type": "application/json"}
                    async with session.post(f"{self.base}/chat", data=data, headers=headers) as response:
                        return json.loads(body), await response.json()

            return asyncio.run(main())

        recorded = self.record_run(calls)
        replayed = self.replay_run(calls)
        self.assertEqual(replayed, (_redacted(recorded[0]), _redacted(recorded[1])))


class ReplaySafetyTest(ReplayTestCase):
    def test_network_is_blocked_but_loopback_works(self):
        self.record_run(lambda: requests.get(f"{self.base}/reply", timeout=10))

        def calls():
            with self.assertRaises(replay.HttpReplayNetworkBlocked):
                socket.create_connection(("192.0.2.1", 443), timeout=1)
            with self.assertRaises(replay.HttpReplayNetworkBlocked):
                socket.getaddrinfo("example.com", 443)
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1):
                pass

        self.replay_run(calls)
        # The guard is gone after replay.
        self.assertTrue(socket.getaddrinfo("localhost", 80))

    @unittest.skipIf(DefaultAzureCredential is None, "azure-identity is not installed")
    def test_azure_credentials_give_a_placeholder_token(self):
        self.record_run(lambda: requests.get(f"{self.base}/reply", timeout=10))

        def calls():
            token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token

            async def get_async():
                async with AsyncDefaultAzureCredential() as credential:
                    return (await credential.get_token("https://ai.azure.com/.default")).token

            return token, asyncio.run(get_async())

        self.assertEqual(self.replay_run(calls), (azure_provider.PLACEHOLDER_TOKEN, azure_provider.PLACEHOLDER_TOKEN))

    @unittest.skipIf(DefaultAzureCredential is None, "azure-identity is not installed")
    def test_every_public_credential_gives_the_placeholder_and_is_restored(self):
        # Every public credential, not only the ones a project uses: AzureCliCredential
        # and the other CLI credentials run a program the network guard can't see, so
        # one left unpatched would quietly get a real token on a signed-in machine.
        # Instances skip __init__ (most credentials need arguments); an unpatched
        # method fails on the missing attributes.
        scope = "https://ai.azure.com/.default"
        sync_classes = _public_credential_classes(azure.identity)
        async_classes = _public_credential_classes(azure.identity.aio)
        self.assertGreater(len(sync_classes), 10)
        self.assertGreater(len(async_classes), 10)
        method_names = ["get_token"]
        if all(hasattr(credential_class, "get_token_info") for credential_class in sync_classes + async_classes):
            method_names.append("get_token_info")
        original_methods = {
            (credential_class, name): getattr(credential_class, name)
            for credential_class in sync_classes + async_classes
            for name in method_names
        }
        self.record_run(lambda: requests.get(f"{self.base}/reply", timeout=10))

        def calls():
            tokens = {}
            for credential_class in sync_classes:
                credential = credential_class.__new__(credential_class)
                for name in method_names:
                    tokens[credential_class, name] = getattr(credential, name)(scope).token

            async def get_async_tokens():
                for credential_class in async_classes:
                    credential = credential_class.__new__(credential_class)
                    for name in method_names:
                        access_token = await getattr(credential, name)(scope)
                        tokens[credential_class, name] = access_token.token

            asyncio.run(get_async_tokens())
            return tokens

        tokens = self.replay_run(calls)
        self.assertEqual(tokens, dict.fromkeys(original_methods, azure_provider.PLACEHOLDER_TOKEN))
        for (credential_class, name), original_method in original_methods.items():
            with self.subTest(credential=credential_class.__module__ + "." + credential_class.__name__, method=name):
                self.assertIs(getattr(credential_class, name), original_method)

    def post_random_values(self):
        """A request whose body holds a random choice and a uuid4, as the attack
        simulators and evaluate() make them."""
        random_input = f"{random.randint(0, 10**6)}-{uuid.uuid4()}"
        return requests.post(f"{self.base}/reply", json={"input": random_input}, timeout=10).json()["echo"]

    def test_random_choices_and_uuids_repeat_on_replay(self):
        original_uuid4 = uuid.uuid4
        recorded = self.record_run(self.post_random_values)
        self.assertIn(replay.SEED_LABEL, (self.folder / "recording-info.txt").read_text())
        self.assertEqual(self.replay_run(self.post_random_values), recorded)
        self.assertIs(uuid.uuid4, original_uuid4)  # restored when replay stops

    def test_each_record_run_gets_a_new_seed(self):
        first = self.record_run(self.post_random_values)
        self.folder = self.new_folder()
        self.assertNotEqual(self.record_run(self.post_random_values), first)

    def test_recording_without_a_seed_still_replays(self):
        self.record_run(lambda: requests.get(f"{self.base}/reply", timeout=10))
        info_path = self.folder / "recording-info.txt"
        info_lines = info_path.read_text().splitlines()
        info_path.write_text("\n".join(line for line in info_lines if not line.startswith(replay.SEED_LABEL)))
        replayed = self.replay_run(lambda: requests.get(f"{self.base}/reply", timeout=10).json())
        self.assertEqual(replayed["output_text"], "Hello")

    def test_uuids_match_any_uuid_only_when_asked(self):
        recorded_id = "88318f61-84a3-4be7-a20b-8eabd84d586d"
        replayed_id = "0ac57b06-6e86-4d53-9500-cf6e7d403006"

        def post_with(run_id):
            return lambda: requests.post(f"{self.base}/results/{run_id}", json={"input": run_id}, timeout=10).json()

        self.record_run(post_with(recorded_id))
        self.assertEqual(self.replay_run(post_with(replayed_id), ignore_uuids=True)["echo"], recorded_id)
        with self.assertRaises(replay.HttpReplayMiss):
            self.replay_run(post_with(replayed_id))

    def test_replay_needs_a_recording(self):
        with self.assertRaises(FileNotFoundError):
            replay.install(mode="replay", directory=self.folder)

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"HTTP_LOG": "off"}):
            http_record_replay.enable()
        self.assertEqual(replay.mode(), "off")

    def with_env_file(self, text):
        """Point the settings at a .env with this text (and HTTP_LOG=off) and a project
        root in the test folder."""
        env_file = self.folder / ".env"
        env_file.write_text("HTTP_LOG=off\n" + text)
        for name, value in (("ENV_FILE", env_file), ("PROJECT_ROOT", self.folder)):
            patcher = mock.patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_env_file_turns_recording_on_with_a_default_folder(self):
        self.with_env_file("HTTP_REPLAY=record\n")
        http_record_replay.enable()
        self.assertEqual(replay.mode(), "record")
        folders = list((self.folder / "runs" / "recordings").iterdir())
        self.assertEqual(len(folders), 1)
        self.assertRegex(folders[0].name, r"-\d{8}-\d{6}$")
        info = (folders[0] / "recording-info.txt").read_text()
        for field in ("date: ", "command: ", "git commit: ", "python: ", "packages:"):
            self.assertIn(field, info)

    def test_shell_setting_wins_over_env_file(self):
        self.with_env_file("HTTP_REPLAY=record\n")
        with mock.patch.dict(os.environ, {"HTTP_REPLAY": "off"}):
            http_record_replay.enable()
        self.assertEqual(replay.mode(), "off")

    def test_record_into_a_given_folder_writes_recording_info(self):
        self.record_run(lambda: requests.get(f"{self.base}/reply", timeout=10))
        self.assertTrue((self.folder / "recording-info.txt").exists())
        self.assertEqual(len(list(self.folder.glob("*.json"))), 1)


def _public_credential_classes(module):
    """The classes azure.identity or azure.identity.aio exports that have get_token."""
    exported = [getattr(module, name) for name in module.__all__]
    return [item for item in exported if isinstance(item, type) and hasattr(item, "get_token")]


def _redacted(reply):
    """A reply as replayed: secret fields are "<redacted>" in the recording."""
    return {key: "<redacted>" if key == "access_token" else value for key, value in reply.items()}


if __name__ == "__main__":
    unittest.main()
