"""Time on replay: sleeps are skipped and the clock is the record run's, so a replay
runs fast and sees the dates and times its record run saw.

Skipped sleeps (HTTP_REPLAY_SKIP_SLEEP; on by default on replay, 0 turns it off):
- time.sleep, asyncio.sleep and anyio's sleep (on asyncio) return at once in the main
  thread; a sleep of 0 still yields to other tasks. Other threads sleep for real:
  telemetry exporters and workers sleep in loops and would spin.
- A fake offset moves time.monotonic, time.perf_counter and time.time (and their _ns
  forms) forward by the time skipped, so a loop that waits for a deadline still ends.
  The asyncio event loop keeps the real time, so the timeouts of other tasks don't fire
  early.
- Stuck-wait guard: STUCK_SLEEPS skipped sleeps in a row with no replayed answer
  between them mean the program waits for something the recording doesn't have (a
  local server starting, a child process, a file). Sleeps are then real until the next
  replayed answer, and one line names the caller.
- At the end, one line says how many sleeps and seconds were skipped.
- Not reached: a sleep imported by name before enable() (`from time import sleep`), and
  waits that don't sleep (threading.Event.wait, queue.get with a timeout, select).

The recorded clock (HTTP_REPLAY_CLOCK; on by default on replay, 0 turns it off):
- A record run notes its start in recording-info.txt (`clock start: <ISO time>`).
- Replay starts the wall clock there and lets it tick (time.time, datetime.now,
  date.today, localtime, strftime, ...), so dates the program puts in requests or
  prints match the record run's. Skipped sleeps move it forward too.
- Needs time-machine (`pip install "http-record-replay[clock]"`): it patches the clock
  functions in C, so code that imported them by name is reached too. Without it, the
  replay uses the real clock and says so.
- Not reached: child processes, and the time zone (local times use the replay
  machine's).
"""

import asyncio
import asyncio.base_events
import atexit
import math
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import settings

CLOCK_LABEL = "clock start"  # key in recording-info.txt
STUCK_SLEEPS = 20
OFFSET_CLOCKS = ("monotonic", "perf_counter", "time")

# The real functions, kept before anything here replaces them.
_real_sleep = time.sleep
_real_asyncio_sleep = asyncio.sleep
_real_clocks = {name: getattr(time, name) for name in OFFSET_CLOCKS}
_real_clocks_ns = {name: getattr(time, f"{name}_ns") for name in OFFSET_CLOCKS}
_LIBRARY_FOLDERS = (str(Path(__file__).parent), str(Path(asyncio.__file__).parent), f"{os.sep}anyio{os.sep}")

_lock = threading.Lock()
_state = {
    "offset": 0.0,  # seconds the skipped sleeps moved the clocks forward
    "skipped": 0,
    "slept_for_real": 0,
    "in_a_row": 0,  # skipped sleeps since the last replayed answer
    "traveller": None,  # time-machine's, while the recorded clock runs
    "coordinates": None,
}
_exit_report = {"registered": False}


def record_start():
    """The record run's start, for recording-info.txt."""
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def start_replay(recorded_start, patches):
    """Start the recorded clock and skip sleeps, as the settings say (the replaced
    functions are put back with patches, the clock by stop())."""
    if settings.flag("HTTP_REPLAY_CLOCK", default=True):
        _start_clock(recorded_start)
    if settings.flag("HTTP_REPLAY_SKIP_SLEEP", default=True):
        _skip_sleeps(patches)


def note_answer():
    """A replayed answer came: the program isn't stuck."""
    with _lock:
        _state["in_a_row"] = 0


def stop():
    """Report the skipped sleeps and put the real clock back."""
    _report()
    if _state["traveller"] is not None:
        _state["traveller"].stop()
    _state.update(offset=0.0, in_a_row=0, traveller=None, coordinates=None)


# --------------------------------------------------------------------------------------
# The recorded clock


def _start_clock(recorded_start):
    if recorded_start is None:
        print("[http_replay] no clock start in this recording: the real clock is used")
        return
    try:
        import time_machine
    except ImportError:
        print(
            "[http_replay] the recorded clock needs time-machine"
            ' (pip install "http-record-replay[clock]"): the real clock is used'
        )
        return
    traveller = time_machine.travel(datetime.fromisoformat(recorded_start), tick=True)
    _state["coordinates"] = traveller.start()
    _state["traveller"] = traveller


# --------------------------------------------------------------------------------------
# Skipped sleeps


def _skip_sleeps(patches):
    # time-machine already moves the wall clock (and shift() moves it on).
    offset_clocks = [name for name in OFFSET_CLOCKS if not (name == "time" and _state["traveller"])]
    for name in offset_clocks:
        patches.set(time, name, _offset_clock(_real_clocks[name]))
        patches.set(time, f"{name}_ns", _offset_clock_ns(_real_clocks_ns[name]))
    patches.set(asyncio.base_events.BaseEventLoop, "time", _real_loop_time)
    patches.set(time, "sleep", _sleep)
    patches.set(asyncio, "sleep", _asyncio_sleep)
    patches.set(asyncio.tasks, "sleep", _asyncio_sleep)
    try:
        from anyio._backends._asyncio import AsyncIOBackend
    except ImportError:
        pass
    else:
        patches.set(AsyncIOBackend, "sleep", classmethod(_anyio_sleep))
    if not _exit_report["registered"]:
        atexit.register(_report)
        _exit_report["registered"] = True


def _offset_clock(real_clock):
    def offset_clock():
        return real_clock() + _state["offset"]

    return offset_clock


def _offset_clock_ns(real_clock_ns):
    def offset_clock_ns():
        return real_clock_ns() + int(_state["offset"] * 1e9)

    return offset_clock_ns


def _real_loop_time(loop):
    return _real_clocks["monotonic"]()


def _sleep(seconds):
    if not _skipped(seconds):
        _real_sleep(seconds)


async def _asyncio_sleep(delay, result=None):
    return await _real_asyncio_sleep(0 if _skipped(delay) else delay, result)


async def _anyio_sleep(cls, delay):
    await _asyncio_sleep(delay)


def _skipped(delay):
    """Whether this sleep is skipped (and the clocks moved on by delay)."""
    if not 0 < delay < math.inf or threading.current_thread() is not threading.main_thread():
        return False
    with _lock:
        _state["in_a_row"] += 1
        stuck = _state["in_a_row"] > STUCK_SLEEPS
        if stuck:
            _state["slept_for_real"] += 1
        else:
            _state["skipped"] += 1
            _state["offset"] += delay
        first_stuck = _state["in_a_row"] == STUCK_SLEEPS + 1
    if first_stuck:
        caller = _caller()
        print(
            f"[http_replay] sleeping for real at {caller}: {STUCK_SLEEPS} sleeps in a row got no replayed answer"
            " (HTTP_REPLAY_SKIP_SLEEP=0 turns skipping off)"
        )
    if stuck:
        return False
    if _state["coordinates"] is not None:
        _state["coordinates"].shift(delay)
    return True


def _caller():
    """file:line of the code that slept, outside this package, asyncio and anyio."""
    frame = sys._getframe(1)
    while frame is not None and any(folder in frame.f_code.co_filename for folder in _LIBRARY_FOLDERS):
        frame = frame.f_back
    if frame is None:
        return "unknown"
    path = Path(frame.f_code.co_filename)
    try:
        shown = path.resolve().relative_to(settings.project_root().resolve())
    except ValueError:
        shown = path.name
    return f"{shown}:{frame.f_lineno}"


def _report():
    with _lock:
        skipped, slept_for_real, seconds = _state["skipped"], _state["slept_for_real"], _state["offset"]
        _state.update(skipped=0, slept_for_real=0)
    if skipped or slept_for_real:
        real = f", {slept_for_real} slept for real" if slept_for_real else ""
        print(f"[http_replay] skipped {_sleeps(skipped)} ({seconds:.1f} s){real}")


def _sleeps(count):
    return "1 sleep" if count == 1 else f"{count} sleeps"
