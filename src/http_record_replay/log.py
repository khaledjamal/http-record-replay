"""Log every HTTP request and response of a program in one readable format, with secrets
redacted.

It sees every call the transport hooks see (requests, urllib3, httpx, aiohttp), so the
calls an SDK makes under the hood are logged too. Output, in the format of a Spring
RestTemplate logging interceptor, with a call number to pair the two blocks:

    --- OUTBOUND REQUEST #1 ---
    > POST https://api.example.com/v1/chat/completions
    > Authorization: Bearer <redacted>
    > Content-Type: application/json
    >
    {
      "messages": [...]
    }

    --- OUTBOUND RESPONSE #1 ---
    < 200 OK
    < Content-Type: application/json
    <
    {
      "choices": [...]
    }

Settings (the environment or the project's .env; the arguments of `enable()` win):
    HTTP_LOG           full (default) | summary (one line per call) | off
    HTTP_LOG_METRICS   false (default) | true: time, sizes and token usage per call,
                       and totals when the program ends
    HTTP_LOG_MAX_BODY  characters shown of each body (default 8192); the rest is cut
                       with "...more..."
    HTTP_LOG_POLLS     short (default) | full: how status checks are logged

What it does with the bodies:
- Status checks: a GET to the same URL as an earlier GET (a job polled until it ends,
  for example) is one line with the reply's "status" field, `HTTP #7 GET <url> -> 200
  OK, status: in_progress`. The first GET of a URL is logged in full.
- Secrets are redacted (see redact.py). Long strings inside JSON (base64 images, for
  example) are shortened, and binary bodies are shown as type and size.
- The response block is written once its body has been read, so a streamed reply
  (server-sent events) is logged when the stream ends, without slowing it down. With
  aiohttp the body is logged when it is read with `read()`; a body read piece by piece
  from `response.content` is noted, not shown.
- Replayed calls are logged like real ones, with their `x-http-replay` header.
"""

import atexit
import json
import logging
import sys
import threading

from . import _hooks, redact, settings

logger = logging.getLogger("http_log")

LEVELS = ("full", "summary", "off")
POLL_MODES = ("short", "full")
DEFAULT_MAX_BODY = 8 * 1024
LONG_STRING = 1000
BINARY_TYPES = ("image/", "audio/", "video/", "application/octet-stream", "application/pdf", "application/zip")
MAX_CAPTURE = 1024 * 1024  # bytes of a body kept for the log; the rest is counted, not stored
# A JSON body is kept whole up to this size: only a complete body parses, so only then
# can its long strings (a base64 image, for example) be shortened one by one and the
# fields after them still be shown.
MAX_JSON_CAPTURE = 32 * 1024 * 1024

_settings = {"level": "full", "metrics": False, "max_body": DEFAULT_MAX_BODY, "polls": "short"}
_lock = threading.Lock()
_seen_gets = set()  # URLs already fetched with GET; a repeat is a status check
_totals = {"calls": 0, "seconds": 0.0, "sent": 0, "received": 0, "input_tokens": 0, "output_tokens": 0}


def enable(level=None, metrics=None, max_body=None, polls=None):
    """Start logging HTTP calls. Safe to call more than once; later calls only change
    the settings. Level "off" stops the log."""
    redact.load_extra_names()
    level = (level or settings.setting("HTTP_LOG", "full")).strip().lower() or "full"
    if level not in LEVELS:
        raise ValueError(f"HTTP_LOG must be one of {', '.join(LEVELS)}, not {level!r}")
    if metrics is None:
        metrics = settings.flag("HTTP_LOG_METRICS")
    if max_body is None:
        max_body = int(settings.setting("HTTP_LOG_MAX_BODY", DEFAULT_MAX_BODY))
    polls = (polls or settings.setting("HTTP_LOG_POLLS", "short")).strip().lower() or "short"
    if polls not in POLL_MODES:
        raise ValueError(f"HTTP_LOG_POLLS must be one of {', '.join(POLL_MODES)}, not {polls!r}")
    _settings.update(level=level, metrics=metrics, max_body=max_body, polls=polls)
    if level == "off":
        disable()
        return
    if not logger.handlers:
        # stdout, so the blocks stay in order with the program's prints
        handler = logging.StreamHandler(_CurrentStdout())
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    _hooks.add_observer(_observer)


def disable():
    """Stop logging (record and replay go on if they are on)."""
    _hooks.remove_observer(_observer)
    _seen_gets.clear()


def enabled():
    return _observer in _hooks._observers


class _CurrentStdout:
    """Writes to whatever sys.stdout is at the time of the write, so the log also lands
    in a record run's output copy that started after the log."""

    def write(self, text):
        return sys.stdout.write(text)

    def flush(self):
        sys.stdout.flush()


class _LogObserver(_hooks.Observer):
    def capture_limit(self, exchange):
        return MAX_JSON_CAPTURE if "json" in (exchange.content_type or "").lower() else MAX_CAPTURE

    def on_request(self, exchange):
        with _lock:
            poll = exchange.method == "GET" and _settings["polls"] == "short" and exchange.url in _seen_gets
            if exchange.method == "GET":
                _seen_gets.add(exchange.url)
        exchange.notes["log_poll"] = poll
        if _settings["level"] != "full" or poll:
            return
        lines = [f"--- OUTBOUND REQUEST #{exchange.number} ---", f"> {exchange.method} {exchange.url}"]
        lines += [f"> {name}: {redact.header(name, value)}" for name, value in exchange.request_headers]
        text = _format_body(exchange.request_body, exchange.request_content_type)
        if text:
            lines += [">", text]
        _write(lines)

    def on_response(self, exchange, body, note):
        content_type = exchange.content_type
        metrics = ""
        if _settings["metrics"]:
            usage = _usage(body, content_type)
            _add_to_totals(exchange, usage)
            metrics = _metrics_text(exchange.seconds, exchange.sent, exchange.received, usage)
        if _settings["level"] == "summary" or exchange.notes.get("log_poll"):
            line = f"HTTP #{exchange.number} {exchange.method} {exchange.url} -> {exchange.status} {exchange.reason}"
            line = line.rstrip()
            status = _status_field(body, content_type)
            line += f", status: {status}" if status else ""
            _write([line + (f"  ({metrics})" if metrics else "") + (f"  [{note}]" if note else "")])
            return
        lines = [f"--- OUTBOUND RESPONSE #{exchange.number} ---", f"< {exchange.status} {exchange.reason}".rstrip()]
        lines += [f"< {name}: {redact.header(name, value)}" for name, value in exchange.headers]
        text = _format_body(body, content_type)
        if exchange.received > exchange.captured:
            text += f"\n...more... ({exchange.received} bytes received, first {exchange.captured} kept)"
        if text:
            lines += ["<", text]
        if note:
            lines.append(f"< [{note}]")
        if metrics:
            lines.append(f"< [metrics] {metrics}")
        _write(lines)

    def on_error(self, exchange, error):
        if _settings["level"] == "summary":
            _write([f"HTTP #{exchange.number} {exchange.method} {exchange.url} -> {type(error).__name__}: {error}"])
        else:
            _write([f"--- OUTBOUND ERROR #{exchange.number} ---", f"! {type(error).__name__}: {error}"])


_observer = _LogObserver()


def _write(lines):
    logger.info("\n" + "\n".join(lines))


def _add_to_totals(exchange, usage):
    with _lock:
        _totals["calls"] += 1
        _totals["seconds"] += exchange.seconds or 0.0
        _totals["sent"] += exchange.sent
        _totals["received"] += exchange.received
        if usage:
            _totals["input_tokens"] += usage[0]
            _totals["output_tokens"] += usage[1]


@atexit.register
def _at_exit():
    if not enabled():
        return
    _hooks.finish_pending()  # before the totals: atexit runs this before the hooks' own
    if _settings["metrics"] and _totals["calls"]:
        t = _totals
        tokens = f", tokens {t['input_tokens']} in / {t['output_tokens']} out" if t["input_tokens"] else ""
        sizes = f"sent {_size(t['sent'])}, received {_size(t['received'])}"
        _write([f"HTTP totals: {t['calls']} calls, {t['seconds']:.2f} s in calls, {sizes}{tokens}"])


# --------------------------------------------------------------------------------------
# Formatting.


def _redact_json(value):
    if isinstance(value, dict):
        return {
            key: redact.REDACTED if redact.is_secret_field(key) else _redact_json(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str) and len(value) > LONG_STRING:
        return f"{value[:80]}...({len(value)} characters)"
    return value


def _format_body(body, content_type):
    if not body:
        return ""
    if isinstance(body, str):  # a note such as "<streamed body, not logged>"
        return body
    content_type = (content_type or "").lower()
    if content_type.startswith(BINARY_TYPES):
        return f"<binary body, {_size(len(body))}, {content_type}>"
    if content_type.startswith("multipart/"):
        return f"<multipart body, {_size(len(body))}>"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary body, {_size(len(body))}, {content_type or 'no content type'}>"
    if "json" in content_type or (not content_type and text.lstrip()[:1] in "{["):
        try:
            text = json.dumps(_redact_json(json.loads(text)), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    elif content_type.startswith("application/x-www-form-urlencoded"):
        text = redact.params(text)
    limit = _settings["max_body"]
    return text if len(text) <= limit else text[:limit] + "...more..."


def _status_field(body, content_type):
    """The top-level "status" of a JSON reply (a job's in_progress / completed, for
    example), or None."""
    if not isinstance(body, bytes) or "json" not in (content_type or "").lower():
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    status = data.get("status") if isinstance(data, dict) else None
    return status if isinstance(status, str) else None


def _usage(body, content_type):
    """(input tokens, output tokens) from a model response's usage block, or None."""
    if not isinstance(body, bytes) or not body:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "event-stream" in (content_type or ""):
        candidates = [line[5:].strip() for line in reversed(text.splitlines()) if line.startswith("data:")]
    else:
        candidates = [text]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        usage = data.get("usage") if isinstance(data, dict) else None
        if not usage and isinstance(data, dict) and isinstance(data.get("response"), dict):
            usage = data["response"].get("usage")  # Responses API stream: the final event carries the response
        if isinstance(usage, dict):
            tokens_in = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
            tokens_out = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
            return tokens_in, tokens_out
    return None


def _size(count):
    return f"{count} B" if count < 1024 else f"{count / 1024:.1f} KB"


def _metrics_text(seconds, sent, received, usage):
    text = f"{seconds or 0.0:.2f} s, sent {_size(sent)}, received {_size(received)}"
    return text + (f", tokens {usage[0]} in / {usage[1]} out" if usage else "")
