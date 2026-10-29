"""The workflow around replay: replay a recording against the current code and compare
its output with the record run's, one recording (`compare`) or all of them
(`replay-all`, a free regression suite).

A recording describes itself (record.py): recording-info.txt holds the command that
replays it, the working folder, the HTTP_* settings it was made with (the replay uses
them too, so the log looks the same) and, when the runner made it, the exit code;
output.txt holds the record run's console output.

How a replay is judged:
1. It fails when the program makes a call the recording doesn't have (a MISS), except
   telemetry the SDKs send on their own (the providers' TELEMETRY_HOSTS: counted
   apart, expected).
2. It fails when its exit code differs from the record run's (or isn't 0, when the
   record run's isn't known).
3. Output lines that differ from the record run's are counted and shown, after leaving
   out the replay's own `[http_replay]` lines and the HTTP header lines of the log (they
   change every run: dates, request IDs), and after replacing values that change every
   run: durations (`0.53 s`, `120 ms`), UUIDs, date-times and clock times. More
   patterns: `--ignore REGEX`. Differences fail the replay only with `--strict`.
"""

import difflib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import providers, record, settings

MISS_LINE = re.compile(r"\[http_replay\] MISS: no recorded answer for \S+ (\S+)")
REQUEST_LINE = re.compile(r"> (GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) ")
STATUS_LINE = re.compile(r"< \d{3}\b")
CHANGING_VALUES = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<date-time>"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "<uuid>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s)\b"), "<duration>"),
]


@dataclass
class Result:
    recording: Path
    command: list
    exit_code: int
    recorded_exit_code: int | None
    output: Path
    own_misses: list = field(default_factory=list)
    telemetry_misses: int = 0
    diff: list | None = None  # None: the record run's output isn't in the recording

    @property
    def changed_lines(self):
        return sum(1 for line in self.diff or [] if line[:1] in "+-" and not line.startswith(("+++", "---")))

    def passed(self, strict=False):
        expected_exit = self.recorded_exit_code if self.recorded_exit_code is not None else 0
        return not self.own_misses and self.exit_code == expected_exit and not (strict and self.changed_lines)


def replay(recording, output, extra_env=None, ignore=()):
    """Replay one recording with its own command and settings; output gets the
    program's output."""
    recording = Path(recording).resolve()
    info = record.read_info(recording)
    if record.REPLAY_COMMAND_KEY not in info:
        raise ValueError(f"{recording}: no replay command in {record.INFO_FILE} (made before version 0.1)")
    command = json.loads(info[record.REPLAY_COMMAND_KEY])
    recorded_settings = json.loads(info.get(record.SETTINGS_KEY, "{}"))
    cwd = info.get("cwd") if Path(info.get("cwd", "")).is_dir() else None
    env = {**os.environ, **recorded_settings, **(extra_env or {})}
    env.update(HTTP_REPLAY="replay", HTTP_REPLAY_DIR=str(recording))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as out:
        exit_code = subprocess.run(
            command, check=False, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT
        ).returncode
    recorded_exit = info.get(record.EXIT_CODE_KEY)
    result = Result(recording, command, exit_code, int(recorded_exit) if recorded_exit else None, output)
    replay_text = output.read_text(encoding="utf-8", errors="replace")
    telemetry = providers.telemetry_hosts()
    for match in MISS_LINE.finditer(replay_text):
        if any(host in match.group(1) for host in telemetry):
            result.telemetry_misses += 1
        else:
            result.own_misses.append(match.group(1))
    record_output = recording / record.OUTPUT_FILE
    if record_output.exists():
        before = comparable_lines(record_output.read_text(encoding="utf-8", errors="replace"), ignore)
        after = comparable_lines(replay_text, ignore)
        result.diff = list(difflib.unified_diff(before, after, "record run", "replay", lineterm="", n=0))
    return result


def comparable_lines(text, ignore=()):
    """The output lines that should be the same in a record run and its replay, with
    values that change every run replaced."""
    extra = [(re.compile(pattern), "<ignored>") for pattern in ignore]
    lines = []
    for line in text.splitlines():
        if line.startswith("[http_replay]") or line in (">", "<"):
            continue
        if line.startswith(("> ", "< ")) and not REQUEST_LINE.match(line) and not STATUS_LINE.match(line):
            continue
        for pattern, placeholder in CHANGING_VALUES + extra:
            line = pattern.sub(placeholder, line)
        lines.append(line)
    return lines


def find_recordings(folder):
    """The recordings in folder (subfolders with a recording-info.txt), oldest first (by
    the time recording-info.txt was written: its date has whole seconds only)."""
    found = [path for path in Path(folder).iterdir() if (path / record.INFO_FILE).exists()]
    return sorted(found, key=lambda path: ((path / record.INFO_FILE).stat().st_mtime_ns, path.name))


def newest_per_command(recordings):
    """One recording per replay command: the newest whose record run didn't fail (else
    the newest); recordings without a replay command are left out."""
    chosen = {}
    for path in recordings:  # oldest first, so later ones replace earlier ones
        info = record.read_info(path)
        command = info.get(record.REPLAY_COMMAND_KEY)
        if command is None:
            continue
        failed = info.get(record.EXIT_CODE_KEY, "0") != "0"
        if command not in chosen or not failed or chosen[command][1]:
            chosen[command] = (path, failed)
    return [path for path, _ in chosen.values()]


def runs_folder(name):
    """A new runs/replays/<timestamp>-<name>/ path at the project root (-2, -3, ... when
    one was made in the same second)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = settings.project_root() / "runs" / "replays" / f"{stamp}-{name}"
    folder, number = base, 1
    while folder.exists():
        number += 1
        folder = base.with_name(f"{base.name}-{number}")
    return folder


def describe(result, strict=False):
    """One summary line for a replay."""
    verdict = "PASS" if result.passed(strict) else "FAIL"
    changed = "n/a" if result.diff is None else str(result.changed_lines)
    expected = "?" if result.recorded_exit_code is None else str(result.recorded_exit_code)
    return (
        f"{verdict}  exit {result.exit_code} (recorded {expected})  MISS {len(result.own_misses)}"
        f"  telemetry MISS {result.telemetry_misses}  changed lines {changed}  {result.recording.name}"
    )


def shown_command(result):
    return shlex.join(result.command)
