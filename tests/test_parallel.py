import threading
import time

import pytest

from ytop.parallel import run_all


def test_runs_everything_concurrently():
    seen: list[int] = []
    active, peak = [0], [0]
    lock = threading.Lock()

    def work(item, stopping):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.01)
        with lock:
            active[0] -= 1
            seen.append(item)

    run_all(range(40), work, workers=4)
    assert sorted(seen) == list(range(40))
    assert 1 < peak[0] <= 4


def test_first_error_stops_the_rest_and_waits_for_running_ones():
    started: list[int] = []
    finished: list[int] = []
    flags: list[bool] = []

    def work(item, stopping):
        started.append(item)
        if item == 3:
            raise ValueError("boom")
        time.sleep(0.02)
        flags.append(stopping())
        finished.append(item)

    with pytest.raises(ValueError, match="boom"):
        run_all(range(100), work, workers=4)
    count = len(finished)
    time.sleep(0.1)
    assert len(finished) == count  # nothing ran on after the raise
    assert len(started) < 100  # queued items never started
    assert any(flags)  # running ones were told to stop


def test_one_worker_runs_in_order():
    seen: list[int] = []
    run_all([3, 1, 2], lambda item, stopping: seen.append(item), workers=1)
    assert seen == [3, 1, 2]
