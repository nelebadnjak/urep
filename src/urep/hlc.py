from __future__ import annotations

import time
from dataclasses import dataclass
from functools import total_ordering


@total_ordering
@dataclass(frozen=True, eq=False)
class HLC:
    """Hybrid Logical Clock: (wall_ms, counter, node_id) total order."""

    wall_ms: int
    counter: int
    node_id: str

    def __str__(self) -> str:
        return f"{self.wall_ms}-{self.counter}-{self.node_id}"

    @classmethod
    def parse(cls, value: str) -> HLC:
        wall, counter, node = value.split("-", 2)
        return cls(int(wall), int(counter), node)

    def _key(self) -> tuple[int, int, str]:
        return (self.wall_ms, self.counter, self.node_id)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, HLC):
            return NotImplemented
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, HLC):
            return NotImplemented
        return self._key() == other._key()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Clock:
    """Generates monotonic HLC values for one node."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self._last: HLC | None = None

    @property
    def last(self) -> HLC | None:
        return self._last

    def tick(self, now: int | None = None) -> HLC:
        current = now_ms() if now is None else now
        if self._last is None:
            value = HLC(current, 0, self.node_id)
        else:
            wall = max(current, self._last.wall_ms)
            counter = self._last.counter + 1 if wall == self._last.wall_ms else 0
            value = HLC(wall, counter, self.node_id)
        self._last = value
        return value

    def observe(self, remote: HLC) -> None:
        """Bump local clock past a received version."""
        if self._last is None or remote > self._last:
            self._last = remote
