"""Save every finished call of a run as one JSON file: the recorder behind
HTTP_REPLAY=record.

Each file holds the request (method, URL, headers, body) and the whole response
(status, headers, body, never shortened; a server-sent event stream is kept as its
text). Secrets are redacted (see redact.py), keeping the shape of a redacted object.
Real hosts and IDs stay as they are: recordings are private files.

Next to the calls, so a recording describes itself (workflow.py replays it from these):
- recording-info.txt: date, command, working folder, the command that replays it, git
  commit, Python and package versions, the random seed (see _seed.py), the start time
  (the recorded clock, see _clock.py), and the exit code when the runner started the
  program.
- output.txt: the run's console output, copied from sys.stdout and sys.stderr while
  recording (prints, logging, tracebacks; not the output of child processes or of C
  code writing to the file descriptors directly).
"""

import base64
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import threading
from datetime import datetime
from importlib import metadata
from urllib.parse import urlsplit

from . import _clock, _hooks, _seed, redact, settings

# Response headers that describe the bytes on the wire; recordings keep the decompressed
# body, so they are dropped.
WIRE_HEADERS = {"content-length", "content-encoding", "transfer-encoding"}
INFO_FILE = "recording-info.txt"
OUTPUT_FILE = "output.txt"
REPLAY_COMMAND_KEY = "replay command"  # a JSON list in recording-info.txt
SETTINGS_KEY = "settings"  # a JSON object: the HTTP_* settings a replay should reuse
EXIT_CODE_KEY = "exit code"
# Settings that change the output or the matching, kept so a replay uses the same ones
# (none of them holds a secret).
REPLAYED_SETTINGS = (
    "HTTP_LOG",
    "HTTP_LOG_METRICS",
    "HTTP_LOG_MAX_BODY",
    "HTTP_LOG_POLLS",
    "HTTP_REPLAY_IGNORE_FIELDS",
    "HTTP_REPLAY_IGNORE_UUIDS",
    "HTTP_REPLAY_SKIP_SLEEP",
    "HTTP_REPLAY_CLOCK",
    "HTTP_REDACT_HEADERS",
    "HTTP_REDACT_PARAMS",
    "HTTP_REDACT_FIELDS",
)

# The program's name for the default folder; the runner sets it (sys.argv[0] is the
# runner's own file then).
program_name = None

_lock = threading.Lock()


class Recorder(_hooks.Observer):
    def __init__(self, directory):
        self.directory = directory

    def capture_limit(self, exchange):
        return float("inf")

    def on_response(self, exchange, body, note):
        # A note means the body wasn't read to the end, except for answers that have no
        # body.
        bodiless = exchange.status in (204, 304) or exchange.method == "HEAD"
        if exchange.status is None or (note and not (bodiless and not exchange.received)):
            print(f"[http_replay] NOT RECORDED (body not complete: {note}): {exchange.method} {exchange.url}")
            return
        record = {
            "number": exchange.number,
            "request": {
                "method": exchange.method,
                "url": exchange.url,
                "headers": [[n, redact.header(n, v)] for n, v in exchange.request_headers],
                **body_fields(exchange.request_body, exchange.request_content_type or ""),
            },
            "response": {
                "status": exchange.status,
                "reason": exchange.reason,
                "headers": [[n, redact.header(n, v)] for n, v in exchange.headers if n.lower() not in WIRE_HEADERS],
                **body_fields(body, exchange.content_type or ""),
            },
        }
        path = urlsplit(exchange.url).path.strip("/").replace("/", "_")[:60] or "root"
        name = f"{exchange.number:04d}-{exchange.method}-{re.sub(r'[^A-Za-z0-9_.-]', '-', path)}.json"
        text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        with _lock:
            (self.directory / name).write_text(text)


def default_directory():
    """runs/recordings/<program>-<timestamp>/ at the project root."""
    name = program_name or (sys.argv[0] and _stem(sys.argv[0])) or "python"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return settings.project_root() / "runs" / "recordings" / f"{name}-{stamp}"


def _stem(path):
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def replay_command():
    """The command that runs this program again: python -m <module> for a module run,
    else python <argv>."""
    main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if main_spec is not None and main_spec.name:
        module = main_spec.name.removesuffix(".__main__")
        return [sys.executable, "-m", module, *sys.argv[1:]]
    return [sys.executable, *sys.argv]


def write_recording_info(directory, random_seed):
    """recording-info.txt: when and how the recording was made, so a later replay can be
    run and judged against it, and the random seed that replay reuses."""
    packages = sorted(f"{dist.metadata['Name']}=={dist.version}" for dist in metadata.distributions())
    commit = _git_commit()
    used_settings = {name: value for name in REPLAYED_SETTINGS if (value := settings.setting(name)) is not None}
    lines = [
        f"date: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"command: {shlex.join(sys.argv)}",
        f"cwd: {os.getcwd()}",
        f"{REPLAY_COMMAND_KEY}: {json.dumps(replay_command())}",
        f"{SETTINGS_KEY}: {json.dumps(used_settings)}",
        f"git commit: {commit}",
        f"python: {platform.python_version()}",
        f"{_seed.SEED_LABEL} {random_seed}",
        f"{_clock.CLOCK_LABEL}: {_clock.record_start()}",
        "packages:",
        *(f"  {package}" for package in packages),
    ]
    (directory / INFO_FILE).write_text("\n".join(lines) + "\n")


def note_exit_code(directory, exit_code):
    """Add the program's exit code to recording-info.txt (the runner knows it)."""
    with (directory / INFO_FILE).open("a") as info:
        info.write(f"{EXIT_CODE_KEY}: {exit_code}\n")


def read_info(directory):
    """The top-level `name: value` lines of a recording-info.txt, as a dict ({} when
    there is none)."""
    path = directory / INFO_FILE
    if not path.exists():
        return {}
    info = {}
    for line in path.read_text().splitlines():
        name, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            info[name.strip()] = value.strip()
    return info


class OutputTee:
    """Stands in for sys.stdout or sys.stderr: every write also goes to the recording's
    output.txt."""

    def __init__(self, stream, file):
        self._stream, self._file = stream, file

    def write(self, text):
        written = self._stream.write(text)
        if not self._file.closed:
            self._file.write(text)
        return written

    def flush(self):
        self._stream.flush()
        if not self._file.closed:
            self._file.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def start_output_copy(directory, patches):
    """Copy sys.stdout and sys.stderr into output.txt (through patches, so they are put
    back with them); returns the open file, for the caller to close."""
    output_file = (directory / OUTPUT_FILE).open("w", encoding="utf-8", errors="replace", buffering=1)
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None:
            patches.set(sys, name, OutputTee(stream, output_file))
    return output_file


def _git_commit():
    root = str(settings.project_root())
    try:
        commit = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        changes = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit} (uncommitted changes)" if changes else commit


def body_fields(body, content_type):
    """The body as JSON (body_json), text (body_text) or base64 (body_base64), secrets
    redacted; a note (a str) as body_note."""
    if not body:
        return {}
    if isinstance(body, str):
        return {"body_note": body}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {"body_base64": base64.b64encode(body).decode()}
    if "json" in content_type.lower() or (not content_type and text.lstrip()[:1] in "{["):
        try:
            return {"body_json": redact.json_keep_shape(json.loads(text))}
        except ValueError:
            pass
    if "x-www-form-urlencoded" in content_type.lower():
        text = redact.params(text)
    return {"body_text": text}


def body_bytes(fields):
    """The body saved by body_fields(), as bytes (or the note, as a str)."""
    if "body_note" in fields:
        return fields["body_note"]
    if "body_json" in fields:
        return json.dumps(fields["body_json"], ensure_ascii=False).encode()
    if "body_text" in fields:
        return fields["body_text"].encode()
    if "body_base64" in fields:
        return base64.b64decode(fields["body_base64"])
    return b""
