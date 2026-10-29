"""Shared test setup: each test module runs with only the settings its tests set."""

import os
import unittest
from unittest import mock

from http_record_replay import redact, settings

SETTING_PREFIXES = ("HTTP_REPLAY", "HTTP_LOG", "HTTP_REDACT")


def isolate_settings():
    """For setUpModule(): drop the shell's HTTP_* settings and ignore the developer's
    .env, for the whole module."""
    kept = {name: value for name, value in os.environ.items() if not name.startswith(SETTING_PREFIXES)}
    unittest.enterModuleContext(mock.patch.dict(os.environ, kept, clear=True))
    unittest.enterModuleContext(mock.patch.object(settings, "ENV_FILE", os.devnull))
    redact.load_extra_names()
