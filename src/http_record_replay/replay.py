"""Record a run's web service calls once, then replay them: the same program runs again
with no network, no sign-in and no cost.

Settings (the environment or the project's .env; the arguments of `install()` win):
    HTTP_REPLAY                off (default) | record | replay
    HTTP_REPLAY_DIR            folder of the recording (one folder per scenario); needed
                               for replay; a record run without it saves to
                               runs/recordings/<script>-<timestamp>/ at the project root
    HTTP_REPLAY_IGNORE_FIELDS  JSON body fields left out when requests are matched
                               (comma-separated names, at any depth); default none
    HTTP_REPLAY_IGNORE_UUIDS   1 = UUIDs anywhere in a request (path, query, body) match
                               any UUID; default off. For recordings made before the
                               random seed, whose random IDs can't be made again.
    HTTP_REPLAY_SKIP_SLEEP     replay: sleeps of the main thread return at once and
                               the clocks move on; default on, 0 = real sleeps
    HTTP_REPLAY_CLOCK          replay: the wall clock starts at the record run's start;
                               default on (needs time-machine), 0 = the real clock

record: every call is made for real and saved (record.py); the random seed (_seed.py)
and the start time (_clock.py) are noted.

replay: nothing leaves the machine.
- Matching: each request is matched to a recorded one by method, URL (host, path and
  sorted query) and body (JSON compared by value, HTTP_REPLAY_IGNORE_FIELDS left out; a
  multipart upload without its random boundary). Headers are not compared (auth,
  request IDs, dates, user agent).
- Order: when the same request was made several times, the 1st replay gets the 1st
  recorded answer, the 2nd the 2nd, and so on (a job polled until it ends, for example).
- A request with no recorded answer raises HttpReplayMiss naming it; there is no
  fall-back to the real service, so a replay can't cost money.
- Network guard: connections and name lookups for anything but this machine (loopback)
  are refused (HttpReplayNetworkBlocked). It works inside Python only: a program the
  code starts (a CLI) isn't stopped by it.
- Providers: the add-ons in providers/ give a sign-in stand-in and fake secrets the SDK
  can parse (Azure).
- Time: sleeps are skipped and the clock is the record run's (_clock.py).
- Replayed responses carry the header `x-http-replay: <file name>`.

Covered: requests, urllib3, httpx2 and httpx, and aiohttp (bodies read with `read()`).
Not covered: other protocols such as AMQP or gRPC.

Limits: a replay shows that the code still handles the recorded answers, not that the
service still answers the same way. A changed prompt, model or SDK version changes the
requests and needs a new recording; so do random or time-based values in request bodies
that the seed doesn't reach (list them in HTTP_REPLAY_IGNORE_FIELDS).
"""

import hashlib
import ipaddress
import json
import re
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import _clock, _hooks, _seed, providers, record, redact, settings
from ._patches import Patches

MODES = ("off", "record", "replay")
REPLAY_HEADER = "x-http-replay"
SEED_LABEL = _seed.SEED_LABEL
UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)


class HttpReplayMiss(RuntimeError):
    """A request in replay mode that has no recorded answer."""


class HttpReplayNetworkBlocked(ConnectionRefusedError):
    """A connection to another machine attempted in replay mode."""


_state = {
    "mode": "off",
    "dir": None,
    "ignore": frozenset(),
    "ignore_uuids": False,
    "recorder": None,
    "output_file": None,
}
_lock = threading.Lock()
_recorded = {}  # replay: match key -> list of recording files, in recorded order
_used = {}  # replay: match key -> how many of them have been replayed
_patches = Patches()  # the seed, the network guard and the providers' patches


def mode():
    return _state["mode"]


def directory():
    """The recording's folder while recording or replaying, else None."""
    return _state["dir"]


def install(mode=None, directory=None, ignore_fields=None, ignore_uuids=None):
    """Start record or replay as HTTP_REPLAY (or the arguments) say; does nothing when
    the mode is off. Safe to call more than once with the same mode."""
    mode = (mode or settings.setting("HTTP_REPLAY") or "off").strip().lower() or "off"
    if mode not in MODES:
        raise ValueError(f"HTTP_REPLAY must be one of {', '.join(MODES)}, not {mode!r}")
    if mode == "off":
        return
    if _state["mode"] != "off":
        if mode != _state["mode"]:
            raise RuntimeError(f"http_replay is already in {_state['mode']} mode")
        return
    redact.load_extra_names()
    directory = directory or settings.setting("HTTP_REPLAY_DIR")
    if not directory and mode == "record":
        directory = record.default_directory()
    if not directory:
        raise ValueError(f"HTTP_REPLAY={mode} needs HTTP_REPLAY_DIR (the recording's folder)")
    if ignore_fields is None:
        ignore_fields = settings.names("HTTP_REPLAY_IGNORE_FIELDS")
    if ignore_uuids is None:
        ignore_uuids = settings.flag("HTTP_REPLAY_IGNORE_UUIDS")
    _state.update(
        mode=mode,
        dir=Path(directory),
        ignore=frozenset(f.strip() for f in ignore_fields),
        ignore_uuids=ignore_uuids,
    )
    if mode == "record":
        _start_recording()
    else:
        _start_replay()
    print(f"[http_replay] {mode} mode, folder {_shown_path(_state['dir'])}")


def _start_recording():
    _state["dir"].mkdir(parents=True, exist_ok=True)
    _state["output_file"] = record.start_output_copy(_state["dir"], _patches)
    random_seed = _seed.new_seed()
    record.write_recording_info(_state["dir"], random_seed)
    _seed.apply(random_seed, _patches)
    recorder = _state["recorder"] = record.Recorder(_state["dir"])
    _hooks.add_observer(recorder)


def _start_replay():
    _load()
    random_seed = _seed.read_seed(_state["dir"] / record.INFO_FILE)
    if random_seed is None:
        print("[http_replay] no random seed in this recording: random values differ from the record run")
    else:
        _seed.apply(random_seed, _patches)
    recorded_start = record.read_info(_state["dir"]).get(_clock.CLOCK_LABEL)
    _clock.start_replay(recorded_start, _patches)
    _guard_network()
    for provider in providers.active():
        provider.install_replay(_patches)
    _hooks.set_responder(_respond)


def uninstall():
    """Stop recording or replaying and undo every patch; the log goes on if it is on."""
    if _state["recorder"] is not None:
        _hooks.remove_observer(_state["recorder"])
    _hooks.set_responder(None)
    _clock.stop()
    _patches.restore()
    if _state["output_file"] is not None:
        _state["output_file"].close()
    _seed.reset()
    _recorded.clear()
    _used.clear()
    _state.update(mode="off", dir=None, ignore=frozenset(), ignore_uuids=False, recorder=None, output_file=None)


def _shown_path(path):
    """The path relative to the project when it is inside it, so printed output carries
    no home folder."""
    try:
        return path.resolve().relative_to(settings.project_root().resolve())
    except ValueError:
        return path


# --------------------------------------------------------------------------------------
# Matching: the same key for the saved request and the live one.


def match_key(method, url, body):
    """Method, host, path, sorted query and the canonical body of a request, as one
    string."""
    parts = urlsplit(redact.url(str(url)))
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    key = f"{method.upper()} {parts.scheme}://{parts.netloc}{parts.path}?{query}\n{_canonical_body(body)}"
    if _state["ignore_uuids"]:
        key = UUID_PATTERN.sub("<uuid>", key)
    return key


def _canonical_body(body):
    if not body:
        return ""
    if isinstance(body, str):  # a note such as "<streamed body, not logged>"
        return body
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        pass
    else:
        data = _drop_fields(redact.json_keep_shape(data), _state["ignore"])
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "sha256:" + hashlib.sha256(body).hexdigest()
    if re.fullmatch(r"[\w.~%+\-]+=[^&]*(&[\w.~%+\-]+=[^&]*)*", text):  # form-encoded
        return redact.params("&".join(sorted(text.split("&"))))
    multipart = re.match(r"--([0-9A-Za-z'()+_,./:=?-]{1,70})\r\n", text)
    if multipart:  # file uploads: the boundary is random for each request, so it is left out of the match
        return text.replace(multipart.group(1), "<boundary>")
    return text


def _drop_fields(value, names):
    if not names:
        return value
    if isinstance(value, dict):
        return {k: _drop_fields(v, names) for k, v in value.items() if k not in names}
    if isinstance(value, list):
        return [_drop_fields(v, names) for v in value]
    return value


def _digest(method, url, body):
    return hashlib.sha256(match_key(method, url, body).encode()).hexdigest()


# --------------------------------------------------------------------------------------
# Replay: load the recording, then answer each request from it.


def _load():
    files = sorted(_state["dir"].glob("*.json"), key=lambda p: (json.loads(p.read_text())["number"], p.name))
    if not files:
        raise FileNotFoundError(f"no recorded calls in {_state['dir']}")
    for path in files:
        # The key is computed now, from the saved request, so HTTP_REPLAY_IGNORE_FIELDS
        # can change after recording.
        request = json.loads(path.read_text())["request"]
        digest = _digest(request["method"], request["url"], record.body_bytes(request))
        _recorded.setdefault(digest, []).append(path)


def _respond(exchange):
    """The recorded Answer to this request, or HttpReplayMiss."""
    digest = _digest(exchange.method, exchange.url, exchange.request_body)
    with _lock:
        count = _used.get(digest, 0)
        files = _recorded.get(digest, [])
        if count >= len(files):
            occurrence = f" (call {count + 1}; {len(files)} recorded)" if files else ""
            message = f"no recorded answer for {exchange.method} {exchange.url}{occurrence} in {_state['dir']}"
            print(f"[http_replay] MISS: {message}")
            raise HttpReplayMiss(message)
        _used[digest] = count + 1
        path = files[count]
    _clock.note_answer()
    response = json.loads(path.read_text())["response"]
    if "body_json" in response:
        for provider in providers.active():
            response["body_json"] = provider.fill_replayed_json(response["body_json"])
    body = record.body_bytes(response)
    headers = [(n, v) for n, v in response["headers"]] + [
        ("Content-Length", str(len(body))),
        (REPLAY_HEADER, path.name),
    ]
    return _hooks.Answer(response["status"], response["reason"], headers, body)


def _guard_network():
    """Refuse connections and name lookups for anything but this machine."""

    def local(host):
        if host in (None, "", "localhost") or isinstance(host, bytes) and host == b"localhost":
            return True
        try:
            return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
        except ValueError:
            return False

    def check(address):
        if isinstance(address, tuple) and not local(address[0]):
            raise HttpReplayNetworkBlocked(f"HTTP_REPLAY=replay: connection to {address[0]} refused")

    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        check(address)
        return original_connect(self, address)

    def connect_ex(self, address):
        check(address)
        return original_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not local(host):
            raise HttpReplayNetworkBlocked(f"HTTP_REPLAY=replay: name lookup of {host} refused")
        return original_getaddrinfo(host, *args, **kwargs)

    _patches.set(socket.socket, "connect", connect)
    _patches.set(socket.socket, "connect_ex", connect_ex)
    _patches.set(socket, "getaddrinfo", getaddrinfo)
