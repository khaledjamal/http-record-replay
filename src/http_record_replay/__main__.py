"""Command line of http_record_replay.

    python -m http_record_replay run script.py [args ...]
    python -m http_record_replay run -m package.module [args ...]
    python -m http_record_replay compare RECORDING [--strict] [--ignore REGEX ...]
                                         [--real-sleep]
    python -m http_record_replay replay-all [--recordings DIR] [--all] [--strict]
                                            [--ignore REGEX ...] [--real-sleep]

- run: calls `http_record_replay.enable()` (settings as usual: HTTP_LOG, HTTP_REPLAY,
  ...), then runs the program as `python script.py` or `python -m module` would: as
  `__main__`, with the same sys.argv and with the script's folder first on sys.path.
  Its exit code is the program's (and is noted in the recording when recording). Only
  this process is covered; Python processes the program starts are not.
- compare: replays one recording against the current code and shows how its output
  differs from the record run's (workflow.py says how a replay is judged).
- replay-all: does that for every recording in DIR (default runs/recordings at the
  project root): the newest per command, or each one with --all. One line per
  recording, and the summary saved in runs/replays/<timestamp>-replay-all/.
compare and replay-all exit with 1 when a replay fails; --real-sleep makes their replays
sleep for real (HTTP_REPLAY_SKIP_SLEEP=0).
"""

import argparse
import os
import runpy
import sys
from pathlib import Path

import http_record_replay
from http_record_replay import record, replay, settings, workflow


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m http_record_replay",
        description="Run a Python program with the HTTP log, record and replay on; compare replays.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run a script or module (-m)")
    run_parser.add_argument("-m", dest="module", help="run a module, as python -m does")
    run_parser.add_argument("target", nargs=argparse.REMAINDER, help="script.py [args ...], or the module's args")
    compare_parser = commands.add_parser("compare", help="replay one recording and compare its output")
    compare_parser.add_argument("recording", type=Path)
    all_parser = commands.add_parser("replay-all", help="replay every recording as a regression suite")
    all_parser.add_argument("--recordings", type=Path, help="folder of recordings (default: runs/recordings)")
    all_parser.add_argument("--all", action="store_true", help="every recording, not the newest per command")
    for judged in (compare_parser, all_parser):
        judged.add_argument("--strict", action="store_true", help="changed output lines fail the replay too")
        judged.add_argument("--ignore", action="append", default=[], metavar="REGEX", help="more changing values")
        judged.add_argument("--real-sleep", action="store_true", help="sleep for real (HTTP_REPLAY_SKIP_SLEEP=0)")
    args = parser.parse_args(argv)
    if args.command == "run":
        if args.module is None and not args.target:
            parser.error("run needs a script (or -m module)")
        run(args.module, args.target)
        return
    extra_env = {"HTTP_REPLAY_SKIP_SLEEP": "0"} if args.real_sleep else {}
    if args.command == "compare":
        sys.exit(compare(args.recording, args.strict, args.ignore, extra_env))
    recordings = args.recordings or settings.project_root() / "runs" / "recordings"
    sys.exit(replay_all(recordings, args.all, args.strict, args.ignore, extra_env))


# --------------------------------------------------------------------------------------
# run


def run(module, target):
    record.program_name = module.rsplit(".", 1)[-1] if module else Path(target[0]).stem
    http_record_replay.enable()
    exit_code = 1
    try:
        if module is not None:
            run_module(module, target)
        else:
            run_script(target[0], target[1:])
        exit_code = 0
    except SystemExit as stop:
        exit_code = _exit_code(stop.code)
        raise
    finally:
        if replay.mode() == "record":
            record.note_exit_code(replay.directory(), exit_code)


def _exit_code(code):
    if code is None:
        return 0
    return code if isinstance(code, int) else 1


def run_script(script, script_args):
    sys.argv = [script, *script_args]
    sys.path[0] = os.path.dirname(os.path.abspath(script))
    runpy.run_path(script, run_name="__main__")


def run_module(module, module_args):
    sys.argv = [module, *module_args]  # runpy puts the module's file path in argv[0]
    sys.path[0] = os.getcwd()
    runpy.run_module(module, run_name="__main__", alter_sys=True)


# --------------------------------------------------------------------------------------
# compare and replay-all


def compare(recording, strict, ignore, extra_env):
    replay_dir = workflow.runs_folder(f"compare-{Path(recording).name}")
    result = workflow.replay(recording, replay_dir / record.OUTPUT_FILE, extra_env, ignore)
    print(f"$ {workflow.shown_command(result)}")
    print(f"replay output: {result.output}")
    for miss in result.own_misses:
        print(f"MISS: {miss}")
    if result.diff is None:
        print(f"(no {record.OUTPUT_FILE} in the recording: output not compared)")
    else:
        print("\n".join(result.diff) if result.diff else "(no differences in the output)")
    print(workflow.describe(result, strict))
    return 0 if result.passed(strict) else 1


def replay_all(recordings_folder, every, strict, ignore, extra_env):
    found = workflow.find_recordings(recordings_folder)
    chosen = found if every else workflow.newest_per_command(found)
    skipped = [path for path in found if path not in chosen]
    replay_dir = workflow.runs_folder("replay-all")
    lines = [f"Replaying {len(chosen)} of {len(found)} recordings in {recordings_folder}"]
    print(lines[0], flush=True)
    failures = 0
    for recording in chosen:
        try:
            result = workflow.replay(recording, replay_dir / f"{recording.name}.txt", extra_env, ignore)
        except ValueError as error:
            line = f"SKIP  {error}"
        else:
            line = workflow.describe(result, strict)
            failures += not result.passed(strict)
        print(line, flush=True)
        lines.append(line)
    if skipped and not every:
        lines.append(f"{len(skipped)} older recordings not replayed (--all replays them)")
        print(lines[-1])
    summary = f"{len(chosen) - failures} of {len(chosen)} passed"
    lines.append(summary)
    print(summary)
    replay_dir.mkdir(parents=True, exist_ok=True)
    (replay_dir / "summary.txt").write_text("\n".join(lines) + "\n")
    print(f"Saved: {replay_dir / 'summary.txt'}")
    return 1 if failures else 0


if __name__ == "__main__":
    main()
