"""Tests for time on replay (_clock.py): skipped sleeps, the stuck-wait guard, the
report line and the recorded clock. Record runs call a local HTTP server; replays don't.

No network, no cost. Run from the repo root (the clock tests skip without time-machine):
    python -m unittest discover -s tests -v
"""

import asyncio
import contextlib
import io
import os
import sys
import threading
import time
import unittest
from datetime import UTC, datetime
from unittest import mock

import requests
import support
from test_replay import ReplayTestCase

from http_record_replay import _clock, record

try:
    import time_machine
except ImportError:
    time_machine = None

RECORDED_START = "2026-01-02T03:04:05.000000+00:00"
real_monotonic = time.monotonic  # the clock tests measure real time with


def setUpModule():
    support.isolate_settings()


class SkippedSleepsTest(ReplayTestCase):
    def setUp(self):
        super().setUp()
        self.record_run(self.get_reply)

    def get_reply(self):
        return requests.get(f"{self.base}/reply", timeout=10).json()

    def replay_quietly(self, calls):
        """replay_run(calls), returning what calls() returned and the printed lines."""
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            returned = self.replay_run(calls)
        return returned, printed.getvalue()

    def test_sleeps_return_at_once_and_the_clocks_move_on(self):
        def calls():
            started_real, started_fake, started_wall = real_monotonic(), time.monotonic(), time.time()
            time.sleep(30)
            asyncio.run(asyncio.sleep(30))
            return real_monotonic() - started_real, time.monotonic() - started_fake, time.time() - started_wall

        (real, fake, wall), printed = self.replay_quietly(calls)
        self.assertLess(real, 5)
        self.assertGreaterEqual(fake, 60)
        self.assertGreaterEqual(wall, 60)
        self.assertIn("[http_replay] skipped 2 sleeps (60.0 s)", printed)

    def test_the_wall_clock_moves_on_without_the_recorded_clock(self):
        def calls():
            started = time.time()
            time.sleep(30)
            return time.time() - started

        with mock.patch.dict(os.environ, {"HTTP_REPLAY_CLOCK": "0"}):
            wall, _ = self.replay_quietly(calls)
        self.assertGreaterEqual(wall, 30)

    def test_other_threads_sleep_for_real(self):
        def calls():
            slept = []

            def worker():
                started = real_monotonic()
                time.sleep(0.2)
                slept.append(real_monotonic() - started)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
            return slept[0]

        slept, _ = self.replay_quietly(calls)
        self.assertGreaterEqual(slept, 0.2)

    def test_a_stuck_wait_sleeps_for_real_until_the_next_answer(self):
        def calls():
            started = real_monotonic()
            for _ in range(5):  # 3 skipped, then 2 real
                time.sleep(0.1)
            stuck = real_monotonic() - started
            self.get_reply()  # a replayed answer: skipping again
            started = real_monotonic()
            time.sleep(30)
            return stuck, real_monotonic() - started

        with mock.patch.object(_clock, "STUCK_SLEEPS", 3):
            (stuck, after_answer), printed = self.replay_quietly(calls)
        self.assertGreaterEqual(stuck, 0.2)
        self.assertLess(after_answer, 5)
        self.assertIn("[http_replay] sleeping for real at tests/test_clock.py:", printed)
        self.assertEqual(printed.count("sleeping for real"), 1)
        self.assertIn("[http_replay] skipped 4 sleeps (30.3 s), 2 slept for real", printed)

    def test_asyncio_timeouts_keep_the_real_time(self):
        async def main():
            event = asyncio.Event()

            async def waiter():
                async with asyncio.timeout(5):
                    await event.wait()
                return "set in time"

            task = asyncio.create_task(waiter())
            await asyncio.sleep(0)  # the waiter starts its 5 s timeout
            await asyncio.sleep(60)  # skipped: the clocks move on 60 s
            event.set()
            return await task

        result, _ = self.replay_quietly(lambda: asyncio.run(main()))
        self.assertEqual(result, "set in time")

    def test_skipping_can_be_turned_off(self):
        def calls():
            started = real_monotonic()
            time.sleep(0.2)
            return real_monotonic() - started

        with mock.patch.dict(os.environ, {"HTTP_REPLAY_SKIP_SLEEP": "0"}):
            slept, printed = self.replay_quietly(calls)
        self.assertGreaterEqual(slept, 0.2)
        self.assertNotIn("skipped", printed)

    def test_real_functions_are_back_after_replay(self):
        def functions():
            return time.sleep, time.monotonic, time.time, asyncio.sleep, asyncio.base_events.BaseEventLoop.time

        originals = functions()
        self.replay_quietly(lambda: time.sleep(1))
        self.assertEqual(functions(), originals)


@unittest.skipIf(time_machine is None, "time-machine is not installed")
class RecordedClockTest(ReplayTestCase):
    def setUp(self):
        super().setUp()
        self.record_run(lambda: requests.get(f"{self.base}/reply", timeout=10))

    def set_recorded_start(self, start):
        """Replace the recording's clock start (None: remove it)."""
        info_path = self.folder / record.INFO_FILE
        lines = [line for line in info_path.read_text().splitlines() if not line.startswith(_clock.CLOCK_LABEL)]
        if start is not None:
            lines.append(f"{_clock.CLOCK_LABEL}: {start}")
        info_path.write_text("\n".join(lines) + "\n")

    def replay_now(self):
        """datetime.now() and the printed lines during a replay."""
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            now = self.replay_run(lambda: datetime.now(UTC))
        return now, printed.getvalue()

    def test_a_record_run_notes_its_start(self):
        info = record.read_info(self.folder)
        recorded = datetime.fromisoformat(info[_clock.CLOCK_LABEL])
        self.assertLess(abs((datetime.now(UTC) - recorded).total_seconds()), 60)

    def test_replay_sees_the_record_runs_clock(self):
        self.set_recorded_start(RECORDED_START)
        now, _ = self.replay_now()
        self.assertEqual((now.year, now.month, now.day, now.hour, now.minute), (2026, 1, 2, 3, 4))
        self.assertNotEqual(datetime.now(UTC).date(), now.date())  # the real clock is back

    def test_skipped_sleeps_move_the_recorded_clock(self):
        self.set_recorded_start(RECORDED_START)

        def calls():
            time.sleep(120)
            return datetime.now(UTC)

        with contextlib.redirect_stdout(io.StringIO()):
            now = self.replay_run(calls)
        self.assertEqual((now.hour, now.minute), (3, 6))

    def test_the_clock_can_be_turned_off(self):
        self.set_recorded_start(RECORDED_START)
        with mock.patch.dict(os.environ, {"HTTP_REPLAY_CLOCK": "0"}):
            now, _ = self.replay_now()
        self.assertNotEqual((now.year, now.month, now.day), (2026, 1, 2))

    def test_a_recording_without_a_clock_start_uses_the_real_clock(self):
        self.set_recorded_start(None)
        now, printed = self.replay_now()
        self.assertNotEqual((now.year, now.month, now.day), (2026, 1, 2))
        self.assertIn("[http_replay] no clock start in this recording", printed)

    def test_without_time_machine_the_real_clock_is_used(self):
        self.set_recorded_start(RECORDED_START)
        with mock.patch.dict(sys.modules, {"time_machine": None}):
            now, printed = self.replay_now()
        self.assertNotEqual((now.year, now.month, now.day), (2026, 1, 2))
        self.assertIn("the recorded clock needs time-machine", printed)


if __name__ == "__main__":
    unittest.main()
