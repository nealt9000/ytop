from ytop.timing import (
    Phase,
    PhaseClock,
    describe,
    human_seconds,
    percentile,
    summary_lines,
)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_phases_record_elapsed_time_and_count_growth():
    tick = FakeClock()
    counts = [0, 0]
    clock = PhaseClock(lambda: tuple(counts), unit="files", clock=tick)
    clock.start("copying")
    tick.now += 30
    counts[:] = [10, 4096]
    clock.start("verifying")  # ends copying
    tick.now += 5
    counts[:] = [12, 5000]
    clock.stop()

    assert [p.name for p in clock.phases] == ["copying", "verifying"]
    copying, verifying = clock.phases
    assert (copying.seconds, copying.count, copying.bytes) == (30, 10, 4096)
    assert (verifying.seconds, verifying.count, verifying.bytes) == (5, 2, 904)
    assert copying.unit == "files"
    assert clock.current is None


def test_restarting_the_running_phase_does_not_split_it():
    tick = FakeClock()
    clock = PhaseClock(clock=tick)
    clock.start("a")
    tick.now += 1
    clock.start("a")
    tick.now += 1
    clock.stop()
    assert [(p.name, p.seconds) for p in clock.phases] == [("a", 2)]


def test_rebase_measures_from_jumped_counts():
    counts = [0, 0]
    clock = PhaseClock(lambda: tuple(counts), clock=FakeClock())
    clock.start("playlists")
    counts[:] = [500, 10_000]  # restored running totals, not work done
    clock.rebase()
    counts[:] = [503, 10_300]
    clock.stop()
    assert (clock.phases[0].count, clock.phases[0].bytes) == (3, 300)


def test_snapshot_excludes_the_running_phase():
    clock = PhaseClock(clock=FakeClock())
    clock.start("a")
    clock.start("b")
    assert [p.name for p in clock.snapshot()] == ["a"]


def test_human_seconds():
    assert human_seconds(3.04) == "3.0 s"
    assert human_seconds(42) == "42 s"
    assert human_seconds(125) == "2 min 05 s"
    assert human_seconds(2 * 3600 + 64) == "2 h 01 min"
    assert human_seconds(-3) == "0.0 s"


def test_describe_and_summary():
    phase = Phase("copying", 120, count=19576, unit="files", bytes=120 * 1024 * 1024)
    line = describe(phase)
    assert line.startswith("copying")
    assert "2 min 00 s" in line
    assert "19,576 files" in line
    assert "120.0 MB (1.0 MB/s)" in line
    lines = summary_lines([phase, Phase("verifying", 30)])
    assert lines[-1].split() == ["total", "2", "min", "30", "s"]
    assert summary_lines([]) == []


def test_as_dict_round_trips():
    doc = Phase("videos", 1.23456, count=3, unit="videos", note="x").as_dict()
    assert doc == {"name": "videos", "seconds": 1.235, "count": 3,
                   "unit": "videos", "bytes": 0, "note": "x"}
    assert Phase(**doc).name == "videos"


def test_percentile_nearest_rank():
    values = [float(v) for v in range(1, 11)]
    assert percentile(values, 0.9) == 9
    assert percentile(values, 1.0) == 10
    assert percentile(values, 0.0) == 1
    assert percentile([], 0.9) == 0
