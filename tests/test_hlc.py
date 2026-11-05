from __future__ import annotations

from urep.hlc import HLC, Clock


def test_parse_roundtrip_with_hyphenated_node() -> None:
    value = HLC(1762359723412, 3, "bg-3f9a1c2b-7d4e-4a8f")
    assert str(value) == "1762359723412-3-bg-3f9a1c2b-7d4e-4a8f"
    assert HLC.parse(str(value)) == value


def test_total_order() -> None:
    a = HLC(100, 0, "a")
    b = HLC(100, 1, "a")
    c = HLC(101, 0, "a")
    d = HLC(101, 0, "b")
    assert a < b < c < d
    assert sorted([d, b, a, c]) == [a, b, c, d]


def test_clock_is_monotonic_when_wall_goes_backwards() -> None:
    clock = Clock("node")
    first = clock.tick(1000)
    second = clock.tick(900)
    third = clock.tick(1000)
    assert first.wall_ms == 1000 and first.counter == 0
    assert second.wall_ms == 1000 and second.counter == 1
    assert third.wall_ms == 1000 and third.counter == 2
    assert first < second < third


def test_clock_advances_with_wall_time() -> None:
    clock = Clock("node")
    first = clock.tick(1000)
    second = clock.tick(1001)
    assert first.counter == 0
    assert second.counter == 0
    assert second.wall_ms == 1001


def test_observe_moves_clock_past_remote() -> None:
    clock = Clock("node")
    remote = HLC(5000, 7, "other")
    clock.observe(remote)
    assert clock.last == remote
    nxt = clock.tick(4000)
    assert nxt.wall_ms == 5000
    assert nxt.counter == 8
