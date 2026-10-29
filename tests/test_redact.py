"""Tests for http_record_replay.redact and settings: which values are hidden, and where
the settings come from.

Run from the repo root:
    python -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

from http_record_replay import redact, settings


def setUpModule():
    support.isolate_settings()


class RedactTest(unittest.TestCase):
    def tearDown(self):
        redact.load_extra_names()

    def test_provider_key_headers_are_redacted(self):
        for name in ("x-api-key", "X-Goog-Api-Key", "api-key", "Ocp-Apim-Subscription-Key", "X-Amz-Security-Token"):
            with self.subTest(header=name):
                self.assertEqual(redact.header(name, "real-key"), redact.REDACTED)
        self.assertEqual(redact.header("Authorization", "Bearer real-token"), "Bearer <redacted>")
        self.assertEqual(redact.header("Content-Type", "application/json"), "application/json")

    def test_aws_presigned_url_parameters_are_redacted(self):
        url = (
            "https://bucket.s3.amazonaws.com/file?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKID%2F20261029%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Signature=abc123"
            "&X-Amz-Security-Token=session"
        )
        redacted = redact.url(url)
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", redacted)
        for secret in ("AKID", "abc123", "session"):
            self.assertNotIn(secret, redacted)

    def test_extra_names_come_from_the_settings(self):
        extra = {"HTTP_REDACT_HEADERS": "X-Custom-Token", "HTTP_REDACT_PARAMS": "ticket", "HTTP_REDACT_FIELDS": "pin"}
        with mock.patch.dict(os.environ, extra):
            redact.load_extra_names()
        self.assertEqual(redact.header("x-custom-token", "real"), redact.REDACTED)
        self.assertEqual(redact.params("ticket=real&page=2"), "ticket=<redacted>&page=2")
        self.assertEqual(redact.json_keep_shape({"pin": "1234", "name": "a"}), {"pin": "<redacted>", "name": "a"})

    def test_a_redacted_object_keeps_its_shape_and_type(self):
        connection = {"credentials": {"type": "ApiKey", "key": "real", "nested": {"secret": "real"}}}
        self.assertEqual(
            redact.json_keep_shape(connection),
            {"credentials": {"type": "ApiKey", "key": "<redacted>", "nested": {"secret": "<redacted>"}}},
        )


class SettingsTest(unittest.TestCase):
    def env_file(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / ".env"
        path.write_text(text)
        patcher = mock.patch.object(settings, "ENV_FILE", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_env_file_lines_are_parsed(self):
        self.env_file(
            "# comment\n"
            "export HTTP_LOG=summary\n"
            "HTTP_REPLAY_DIR='runs/recordings/a b'\n"
            "HTTP_REPLAY_IGNORE_FIELDS=ts, request_id  # changes every run\n"
            "not a setting\n"
        )
        self.assertEqual(settings.setting("HTTP_LOG"), "summary")
        self.assertEqual(settings.setting("HTTP_REPLAY_DIR"), "runs/recordings/a b")
        self.assertEqual(settings.names("HTTP_REPLAY_IGNORE_FIELDS"), ["ts", "request_id"])
        self.assertIsNone(settings.setting("HTTP_LOG_POLLS"))

    def test_environment_wins_over_the_file(self):
        self.env_file("HTTP_LOG=summary\n")
        with mock.patch.dict(os.environ, {"HTTP_LOG": "off"}):
            self.assertEqual(settings.setting("HTTP_LOG"), "off")

    def test_project_root_is_the_nearest_folder_with_a_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "pyproject.toml").write_text("")
            (root / "src" / "pkg").mkdir(parents=True)
            with mock.patch.object(Path, "cwd", return_value=root / "src" / "pkg"):
                self.assertEqual(settings.project_root(), root)


if __name__ == "__main__":
    unittest.main()
