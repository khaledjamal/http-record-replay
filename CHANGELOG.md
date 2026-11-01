# Changelog

Each version lists the settings and options it added and the behavior it changed, so a project pinned to an older
version can tell what it has. Install a version by its tag:
`pip install "http-record-replay[clock] @ git+https://github.com/khaledjamal/http-record-replay@v0.3.1"`.

## 0.3.1 (2026-11-01)

Fixed:
- A response with `Content-Encoding: identity` (not compressed; the MCP extension of Azure Functions sends it) keeps
  its body in the log and the recording. Before, both held `<identity-compressed body, ...>`, and the replay failed
  with `TypeError: sequence item 0: expected a bytes-like object, str found`. Recordings made with 0.3.0 from such a
  server have no body: record them again.

## 0.3.0 (2026-10-30)

Added:
- Recordings replay on another machine or in another clone: `compare` and `replay-all` replace a recorded
  interpreter that isn't there with the one running them, and a recorded working folder that isn't there with the
  same folder relative to the recording's project root (the new `project cwd:` line in `recording-info.txt`). Each
  change is one `[http_replay]` note line.
- `examples/github_repo.py` and its committed recording (GitHub's public API, no key); CI replays it with
  `compare --strict`.

Changed:
- `recording-info.txt` writes paths under the home folder as `~/...` (the command, the working folder, the replay
  command), so a recording shows no user name. Replay expands them again; recordings made with 0.2 still replay.

## 0.2.0 (2026-10-30, tag v0.2.0)

Added:
- Skipped sleeps on replay, on by default: `time.sleep`, `asyncio.sleep` and anyio's sleep return at once in the
  main thread, and the clocks move on by the time skipped. Off: `HTTP_REPLAY_SKIP_SLEEP=0`, or `--real-sleep` on
  `compare` and `replay-all`.
- Stuck-wait guard: after 20 sleeps in a row with no replayed answer, sleeps are real until the next answer, with
  one `[http_replay] sleeping for real at <file>:<line>` line.
- One line at the end of a replay: `[http_replay] skipped <n> sleeps (<seconds> s)`.
- The recorded clock: record runs note `clock start:` in `recording-info.txt`; replay starts the wall clock there
  (`time.time`, `datetime.now`, ...), on by default. Needs the new `clock` extra (time-machine). Off:
  `HTTP_REPLAY_CLOCK=0`.
- `HTTP_REPLAY_SKIP_SLEEP` and `HTTP_REPLAY_CLOCK` are saved with a recording's settings and reused by its replays.

Changed:
- Replays of polling code run much faster; output that prints elapsed times shows the skipped time too (the clocks
  move on).
- Recordings made with 0.1 have no clock start: they replay with the real clock and one `[http_replay]` note.

## 0.1.0.dev0 (2026-10-30, commit 4bf6c53, not tagged)

First version:
- Record, replay and log the HTTP calls of requests, urllib3, httpx (and httpx2) and aiohttp.
- Settings: `HTTP_LOG`, `HTTP_LOG_METRICS`, `HTTP_LOG_MAX_BODY`, `HTTP_LOG_POLLS`, `HTTP_REPLAY`,
  `HTTP_REPLAY_DIR`, `HTTP_REPLAY_IGNORE_FIELDS`, `HTTP_REPLAY_IGNORE_UUIDS`, `HTTP_REDACT_HEADERS`,
  `HTTP_REDACT_PARAMS`, `HTTP_REDACT_FIELDS`.
- Recorded random seed for `random` and `uuid.uuid4()`; network guard on replay; Azure add-on (placeholder tokens,
  fake connection strings).
- Commands: `python -m http_record_replay run`, `compare`, `replay-all`.
