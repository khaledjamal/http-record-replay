# Changelog

Each version lists the settings and options it added and the behavior it changed, so a project pinned to an older
version can tell what it has. Install a version by its tag:
`pip install "http-record-replay[clock] @ git+https://github.com/khaledjamal/http-record-replay@v0.2.0"`.

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
