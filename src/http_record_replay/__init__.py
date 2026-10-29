"""Record and replay the HTTP calls of whole Python program runs, with a built-in HTTP
log.

One call turns everything on as the settings say:

    import http_record_replay

    http_record_replay.enable()

- `HTTP_LOG` (full by default) logs every request and response (log.py).
- `HTTP_REPLAY=record` saves every call of the run; `HTTP_REPLAY=replay` answers the
  next run from the saved files, with no network (replay.py).
- The settings come from the environment or the project's `.env` (settings.py).

Modules: log, replay, record, redact, settings; providers/ for SDK add-ons (Azure). They
all plug into one layer of transport hooks (_hooks.py).
"""

from . import log, replay
from .replay import HttpReplayMiss, HttpReplayNetworkBlocked

__version__ = "0.1.0.dev0"
__all__ = ["HttpReplayMiss", "HttpReplayNetworkBlocked", "disable", "enable", "log", "replay"]


def enable():
    """Start record or replay (HTTP_REPLAY), then the log (HTTP_LOG)."""
    replay.install()
    log.enable()


def disable():
    """Stop the log, record and replay, and undo every patch."""
    log.disable()
    replay.uninstall()
