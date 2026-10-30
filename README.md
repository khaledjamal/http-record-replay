# http-record-replay

Record and replay the HTTP calls of whole Python program runs, with a built-in HTTP log.

Run a program once with recording on, and every web service call it makes is saved, secrets redacted. Run it again
with replay on, and the same program gets the saved answers: no network, no sign-in, no cost. It is made for scripts
and SDK-based apps (AI agents, cloud SDKs) that run as normal commands, not only for unit tests.

Status: early development (0.3.0), not on PyPI yet. What each version added: [CHANGELOG.md](CHANGELOG.md).

## Why

A run of an AI agent or a cloud script is long (dozens to hundreds of calls, with polls, streams and uploads), it
costs money, and its answers differ every time. Checking a change by running it again is slow, paid and hard to
compare. With http-record-replay:

1. Record one real run: `HTTP_REPLAY=record python agent.py`.
2. Change the code.
3. Replay it and compare: `python -m http_record_replay compare runs/recordings/<folder>`. The program gets the
   recorded answers, and its output is compared with the record run's (see [Check a change](#check-a-change)).

A request the recording doesn't have fails with `HttpReplayMiss`, naming it, instead of reaching the service, so a
replay can't cost money. It started in [azure-ai-foundry-agents](https://github.com/khaledjamal/azure-ai-foundry-agents),
where refactors are checked by replaying its 27 recorded lab runs (Azure OpenAI, Foundry agents, AI Search,
evaluation); all 27 replay with this package too (checked 2026-10-29).

## Quick start

In your project, pinned to a tag (`[clock]` adds the recorded clock on replay, through time-machine):

```bash
uv pip install "http-record-replay[clock] @ git+https://github.com/khaledjamal/http-record-replay@v0.3.0"
```

Once at the start of the program:

```python
import http_record_replay

http_record_replay.enable()
```

Then choose with settings, in the environment or the project's `.env`:

```bash
python my_script.py                                                            # logs every call (HTTP_LOG=full)
HTTP_REPLAY=record python my_script.py                                         # real calls, saved
HTTP_REPLAY=replay HTTP_REPLAY_DIR=runs/recordings/<folder> python my_script.py  # the same run, from the files
```

For a program you don't want to edit, the runner does the `enable()` call and then runs it as `python` would (as
`__main__`, same arguments, same exit code):

```bash
HTTP_REPLAY=record python -m http_record_replay run their_script.py --their-option
HTTP_LOG=summary python -m http_record_replay run -m some_package.cli
```

Only that process is covered: Python processes the program starts are not, and neither is anything else in the
environment (pip, other tools).

## What you get

- **Whole program runs, switched by a setting.** The same command records or replays; `HTTP_REPLAY=record` in a
  private `.env` records every run, while other people's clones don't.
- **The calls SDKs make under the hood.** It hooks the transports of requests, urllib3, httpx (and httpx2) and
  aiohttp, so calls made by clients deep inside an SDK are caught too. Checked with the openai SDK and the azure-*
  SDKs (in the labs below) and botocore, the core of boto3 (in the tests).
- **Replays that send the same requests.**
  - A recorded random seed makes Python's `random` choices and `uuid.uuid4()` IDs repeat on replay.
  - JSON bodies are matched by value; fields that change every run can be left out (`HTTP_REPLAY_IGNORE_FIELDS`); a
    multipart upload is matched without its random boundary; any UUID can match any UUID
    (`HTTP_REPLAY_IGNORE_UUIDS`).
  - A request made several times (a job polled until it ends) gets its recorded answers in order.
  - The clock is the record run's: replay starts the wall clock (`time.time`, `datetime.now`, ...) at the time the
    record run started, so dates the program puts in requests match (needs the `clock` extra).
- **Fast replays.** Sleeps of the main thread (`time.sleep`, `asyncio.sleep`, anyio) return at once, and the clocks
  move on by the time skipped, so poll loops and deadlines behave as before. Other threads, and asyncio timeouts,
  keep the real time. When 20 sleeps in a row get no replayed answer, the program is waiting for something real (a
  local server, a child process): sleeps become real again until the next answer, and one line names the caller.
  The workshop's 27 recorded lab runs replay in 70 s instead of 150 s.
- **Secrets redacted by default**, in the log and the recordings: authorization and API key headers of the common
  providers, cookies, key and signature parameters (AWS presigned URLs, the Azure Functions `?code=` key), token,
  password and credential fields in JSON. A redacted object keeps its shape, so code that reads `credentials.key`
  still works. More names: `HTTP_REDACT_HEADERS`, `HTTP_REDACT_PARAMS`, `HTTP_REDACT_FIELDS`.
- **A network guard on replay**: connections and name lookups for anything but this machine are refused. It works
  inside Python only: a program the code starts (a CLI) isn't stopped by it.
- **Provider add-ons.** Azure: every azure-identity credential returns a placeholder token, so replay needs no
  `az login`; a secret the SDK parses (an Application Insights connection string) gets a fake value in the same
  format.
- **Recordings that describe themselves**: one JSON file per call (request and whole response); a
  `recording-info.txt` with the date, command, working folder, the command and settings that replay it, git commit,
  Python and package versions, the random seed, the start time and (with the runner) the exit code; and
  `output.txt`, the run's console output. Paths under the home folder are written as `~/...`, so a recording shows
  no user name.
- **A regression suite for free**: `compare` and `replay-all` replay recordings against the current code and
  compare the output with the record runs' (see below).

It also includes an **HTTP log**: every request and response in one readable format, the same for real and replayed
calls (replayed ones carry an `x-http-replay` header). It works on its own, with record and replay off.

```text
--- OUTBOUND REQUEST #3 ---
> POST https://api.example.com/v1/chat/completions
> Authorization: Bearer <redacted>
> Content-Type: application/json
>
{
  "model": "gpt-4o-mini",
  "messages": [...]
}

--- OUTBOUND RESPONSE #3 ---
< 200 OK
< Content-Type: application/json
<
{
  "choices": [...],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 3
  }
}
```

- Repeated GETs of the same URL (status checks) are one line each:
  `HTTP #7 GET <url> -> 200 OK, status: in_progress`.
- Long strings (base64 images) are shortened and binary bodies shown as type and size.
- Streamed replies are logged when the stream ends, without slowing it down.
- `HTTP_LOG=summary` gives one line per call; `HTTP_LOG_METRICS=true` adds time, sizes and token usage.

## Settings

| Variable | Values |
|---|---|
| `HTTP_LOG` | `full` (default): every request and response; `summary`: one line per call; `off` |
| `HTTP_LOG_METRICS` | `true`: time, sizes and token usage per call, and totals at the end |
| `HTTP_LOG_MAX_BODY` | characters shown of each body (default 8192) |
| `HTTP_LOG_POLLS` | `short` (default): a repeated GET of the same URL is one status line; `full` |
| `HTTP_REPLAY` | `off` (default), `record`, `replay` |
| `HTTP_REPLAY_DIR` | the recording's folder; `record` without it saves to `runs/recordings/<script>-<timestamp>/` |
| `HTTP_REPLAY_IGNORE_FIELDS` | JSON body fields left out when requests are matched (comma-separated) |
| `HTTP_REPLAY_IGNORE_UUIDS` | `1`: any UUID in a request matches any UUID |
| `HTTP_REPLAY_SKIP_SLEEP` | replay: `1` (default): sleeps return at once; `0`: real sleeps |
| `HTTP_REPLAY_CLOCK` | replay: `1` (default): the clock starts at the record run's start (needs time-machine); `0`: the real clock |
| `HTTP_REDACT_HEADERS`, `HTTP_REDACT_PARAMS`, `HTTP_REDACT_FIELDS` | more names to redact (comma-separated) |

A value in the environment wins over the `.env` file (the nearest folder upwards with `.git` or `pyproject.toml`).

## Check a change

```bash
python -m http_record_replay compare runs/recordings/<folder>   # one recording
python -m http_record_replay replay-all                        # the newest recording of each command
python -m http_record_replay replay-all --all --strict         # every recording; changed output fails too
python -m http_record_replay compare runs/recordings/<folder> --real-sleep  # with real sleeps
```

Each recording is replayed with the command, working folder and settings it was recorded with. A recording made on
another machine or in another clone replays too: a recorded Python that isn't there is replaced by the one running
`compare`, and a recorded working folder that isn't there by the same folder relative to the recording's project
root (one `[http_replay]` line says so). A replay:

1. fails on a request the recording doesn't have (a MISS). Telemetry the SDKs send on their own (Azure Monitor
   uploads, for example) is counted apart: its misses are expected.
2. fails when its exit code differs from the record run's (known when the runner made the recording; else it must
   be 0).
3. shows the output lines that differ from the record run's. Before comparing, the replay's own `[http_replay]`
   lines and the log's header lines are left out, and values that change every run (durations such as `0.53 s`,
   UUIDs, date-times, clock times) are replaced; `--ignore REGEX` adds patterns. Changed lines fail the replay only
   with `--strict`.

`replay-all` prints one line per recording, saves the summary in `runs/replays/<timestamp>-replay-all/`, and exits
with 1 when a replay fails, so it can run in CI.

```text
PASS  exit 0 (recorded 0)  MISS 0  telemetry MISS 0  changed lines 0  greet-20261029-220624
```

## How it works

```text
program -> requests / urllib3 / httpx / aiohttp
             └─ hooks: one patch per library, on the method every request passes through
                  ├─ responder: replay answers from the recording (or raises HttpReplayMiss); else the real call
                  └─ observers: the log prints the call; the recorder saves it
```

Each library is patched once, only while the log, record or replay is on, and every patch is undone by
`http_record_replay.disable()`.

## Compared with other tools

Checked on 2026-10-29 against each tool's documentation. "Code" means it can be done with your own hooks or
configuration.

| | http-record-replay | [vcrpy](https://vcrpy.readthedocs.io/) | [pytest-recording](https://github.com/kiwicom/pytest-recording) | [responses](https://github.com/getsentry/responses) / [respx](https://lundberg.github.io/respx/) | [WireMock](https://wiremock.org/docs/record-playback/), [Hoverfly](https://docs.hoverfly.io/) |
|---|---|---|---|---|---|
| Records real calls | yes | yes | yes (vcrpy) | responses: beta recorder; respx: no (mocks) | yes, as a proxy |
| Scope | whole program run | code inside a cassette (decorator or `with`) | pytest tests | tests | any client pointed at the proxy |
| Switch record / replay without editing code | yes (`HTTP_REPLAY`) | no (`record_mode` in code) | yes (`--record-mode`) | no | yes (admin API) |
| Python HTTP libraries | requests, urllib3, httpx, httpx2, aiohttp | more: also http.client, httplib2, tornado, urllib.request, boto3 | as vcrpy | responses: requests; respx: httpx | any (proxy) |
| Secrets redacted by default | yes | no (`filter_headers=()`; configurable) | as vcrpy | n/a | not checked |
| Body matched by default | yes, JSON by value | no (`match_on` has no `body`; a `body` matcher parses JSON) | as vcrpy | per mock | per stub |
| Random values repeat on replay | yes (recorded seed for `random`, `uuid4`) | no | no | n/a (mocks) | n/a |
| Sleeps skipped and the record run's clock on replay | yes | no | no | n/a (mocks) | n/a |
| Network blocked on replay | yes, sockets | no (`ignore_hosts` pass through) | yes, `--block-network` | responses: unmatched requests raise | n/a |
| Request and response log | yes | no | no | no | not checked |
| Cloud sign-in stand-in | Azure | code | code | n/a | n/a |
| Replay every recording and compare the output with the record run's | yes (`compare`, `replay-all`) | no (tests assert instead) | no (tests assert instead) | no | no |
| Recording format | one JSON file per call, plus run info | one YAML (or JSON) cassette | one cassette per test | YAML (responses recorder) | JSON mappings |
| Maturity | new, no users yet | mature, widely used | mature | mature | mature |

Use vcrpy (with pytest-recording) when you want cassettes for unit tests, or for HTTP libraries this package doesn't
hook. Use WireMock or Hoverfly for apps in other languages, or when a proxy is easier than code. Use
http-record-replay to record and replay whole runs of Python scripts and SDK apps, with the log alongside.

## Limits

- A replay shows that the code still handles the recorded answers, not that the service still answers the same way.
  A new prompt, model or SDK version needs a new recording.
- Random values the seed doesn't reach (`os.urandom`, `secrets`, numpy, the order of concurrent tasks) need
  `HTTP_REPLAY_IGNORE_FIELDS` or a fixed value from the program; so do timestamps in request bodies without the
  `clock` extra, or in recordings made before version 0.2 (they have no clock start).
- Time: a sleep imported by name before `enable()` (`from time import sleep`) isn't skipped; waits that don't sleep
  (`Event.wait`, `queue.get` with a timeout) take real time; the recorded clock doesn't reach child processes, and
  local times use the replay machine's time zone.
- Only HTTP: not gRPC, AMQP or WebSocket.
- With aiohttp, a body is captured when it is read with `read()`.
- `output.txt` copies what the program writes through `sys.stdout` and `sys.stderr` (prints, logging, tracebacks),
  not the output of child processes or of C code writing to the file descriptors directly.
- Recordings keep real hosts and IDs: keep them private (`runs/` is git-ignored here).

## Development

```bash
git clone https://github.com/khaledjamal/http-record-replay.git && cd http-record-replay
uv venv && uv pip install -e ".[test]"
python -m unittest discover -s tests -v
ruff check . && ruff format --check .
```

The tests run against a local server: no network, no cost.

## License

MIT; see [LICENSE](LICENSE).
