"""Wall-clock timing per phase of a long job (making a stick, analysing a
playlist), so slow steps are measured rather than guessed."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Callable


@dataclass
class Phase:
    name: str
    seconds: float
    count: int = 0  # files, videos… (``unit`` says which)
    unit: str = ""
    bytes: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        doc = asdict(self)
        doc["seconds"] = round(self.seconds, 3)
        return doc


class PhaseClock:
    """Times consecutive phases: starting one ends the one before.

    ``counts`` (optional) returns the running ``(count, bytes)``; each phase
    records how much they grew while it ran."""

    def __init__(
        self,
        counts: Callable[[], tuple[int, int]] | None = None,
        *,
        unit: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._counts = counts or (lambda: (0, 0))
        self._unit = unit
        self._clock = clock
        self._name: str | None = None
        self._started = 0.0
        self._base = (0, 0)
        self.phases: list[Phase] = []

    @property
    def current(self) -> str | None:
        return self._name

    def start(self, name: str) -> None:
        """End the running phase (if any) and start ``name``.  Starting the
        phase that is already running does nothing."""
        if name == self._name:
            return
        self.stop()
        self._name = name
        self._started = self._clock()
        self._base = self._counts()

    def rebase(self) -> None:
        """The counts jumped (were reset or restored): measure the running
        phase's growth from here."""
        if self._name is not None:
            self._base = self._counts()

    def stop(self) -> None:
        if self._name is None:
            return
        count, size = self._counts()
        self.phases.append(Phase(
            self._name, max(0.0, self._clock() - self._started),
            count=max(0, count - self._base[0]), unit=self._unit,
            bytes=max(0, size - self._base[1]),
        ))
        self._name = None

    def snapshot(self) -> list[Phase]:
        """The phases ended so far (the running one is not included)."""
        return list(self.phases)


def human_seconds(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def _human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover


def describe(phase: Phase) -> str:
    """One readable line: ``copying  38 min 02 s  19,576 files, 2.2 GB (1.0 MB/s)``."""
    parts = []
    if phase.count:
        parts.append(f"{phase.count:,} {phase.unit}".rstrip())
    if phase.bytes:
        rate = (
            f" ({_human_bytes(phase.bytes / phase.seconds)}/s)"
            if phase.seconds >= 0.5 else ""
        )
        parts.append(f"{_human_bytes(phase.bytes)}{rate}")
    if phase.note:
        parts.append(phase.note)
    tail = f"  {', '.join(parts)}" if parts else ""
    return f"{phase.name:<12} {human_seconds(phase.seconds):>12}{tail}"


def total_seconds(phases: list[Phase]) -> float:
    return sum(phase.seconds for phase in phases)


def summary_lines(phases: list[Phase]) -> list[str]:
    if not phases:
        return []
    lines = [describe(phase) for phase in phases]
    lines.append(f"{'total':<12} {human_seconds(total_seconds(phases)):>12}")
    return lines


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile (``fraction`` in 0..1); 0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]
