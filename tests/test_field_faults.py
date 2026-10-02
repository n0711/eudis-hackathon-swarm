"""Lab fault injection: drop windows, control files, random loss, fail-stop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eudis_swarm.field_faults import (
    ControlState,
    DropWindow,
    FaultInjector,
    FaultSpecError,
    load_fault_schedule,
    parse_control,
    parse_drop_window,
    partition_links,
    write_control,
)


def test_drop_windows_parse_and_apply() -> None:
    window = parse_drop_window("3,4@5-15")
    assert window == DropWindow(5.0, 15.0, frozenset({3, 4}))
    assert not window.active(4.9) and window.active(5.0) and not window.active(15.0)
    for text in ("3,4", "3@5", "x@1-2", "3@2-1", "@1-2"):
        with pytest.raises(FaultSpecError):
            parse_drop_window(text)


def test_partition_groups_become_cross_group_links() -> None:
    links = partition_links([[1, 2], [3, 4]])
    assert links == {frozenset(pair) for pair in ((1, 3), (1, 4), (2, 3), (2, 4))}
    state = parse_control({"partition": [[1, 2], [3, 4]], "fail_stop": [4]})
    assert state.blocked_peers_of(1) == {3, 4}
    assert state.blocked_peers_of(3) == {1, 2}
    assert state.fail_stop == {4}
    chain = parse_control({"blocked_links": [[1, 3]]})
    assert chain.blocked_peers_of(2) == frozenset()
    assert ControlState().to_json() == {"blocked_links": [], "fail_stop": []}


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"surprise": 1},
        {"partition": "1,2"},
        {"partition": [[1, "2"]]},
        {"blocked_links": "1-2"},
        {"blocked_links": [[1, 1]]},
        {"blocked_links": [[1, 2, 3]]},
        {"fail_stop": [0]},
        {"fail_stop": [True]},
    ],
)
def test_invalid_control_documents_are_refused(raw) -> None:
    with pytest.raises(FaultSpecError):
        parse_control(raw)


def test_injector_combines_windows_and_the_control_file(tmp_path: Path) -> None:
    control = tmp_path / "control.json"
    injector = FaultInjector(
        1, windows=[DropWindow(5.0, 6.0, frozenset({2}))], control_path=control
    )
    assert injector.refresh(0.0) is False  # no file yet
    write_control(control, parse_control({"partition": [[1, 2], [3]]}))
    assert injector.refresh(1.0) is True
    assert injector.blocked == {3}
    assert injector.drops(3) and not injector.drops(2)
    assert injector.blocks(3) and not injector.blocks(2)
    assert injector.refresh(5.5) is True
    assert injector.blocked == {2, 3}
    # a bad edit keeps the previous state rather than silently healing.
    control.write_text("{broken")
    assert injector.refresh(5.6) is False
    assert injector.control_errors == 1
    write_control(control, ControlState())
    assert injector.refresh(7.0) is True
    assert injector.blocked == frozenset()
    assert not injector.fail_stop_due(7.0)
    write_control(control, parse_control({"fail_stop": [1]}))
    injector.refresh(8.0)
    assert injector.fail_stop_due(8.0)


def test_random_loss_is_seeded_and_counted() -> None:
    first = FaultInjector(1, loss=0.3, loss_seed=7)
    second = FaultInjector(1, loss=0.3, loss_seed=7)
    pattern = [first.drops(2) for _ in range(200)]
    assert pattern == [second.drops(2) for _ in range(200)]
    assert 30 < first.random_drops < 90
    with pytest.raises(FaultSpecError):
        FaultInjector(1, loss=1.0)
    with pytest.raises(FaultSpecError):
        FaultInjector(1, fail_stop_at=-1.0)
    timed = FaultInjector(1, fail_stop_at=3.0)
    assert not timed.fail_stop_due(2.9) and timed.fail_stop_due(3.0)


def test_fault_schedules_are_cumulative(tmp_path: Path) -> None:
    path = tmp_path / "faults.json"
    path.write_text(
        json.dumps(
            {
                "events": [
                    {"at": 1.0, "partition": [[1, 2], [3, 4]]},
                    {"at": 2.0, "fail_stop": [4]},
                    {"at": 3.0, "heal": True},
                    {"at": 3.0, "blocked_links": [[1, 2]]},
                ]
            }
        )
    )
    schedule = load_fault_schedule(path)
    assert [event.label for event in schedule] == [
        "partition",
        "fail_stop",
        "heal",
        "partition",
    ]
    assert schedule[1].state.blocked_peers_of(1) == {3, 4}
    assert schedule[1].state.fail_stop == {4}
    assert schedule[2].state.blocked_links == frozenset()
    assert schedule[2].state.fail_stop == {4}
    assert schedule[3].state.blocked_peers_of(1) == {2}


@pytest.mark.parametrize(
    "document",
    [
        "{",
        json.dumps([]),
        json.dumps({"events": [1]}),
        json.dumps({"events": [{"at": 1.0, "boom": True}]}),
        json.dumps({"events": [{"at": -1, "heal": True}]}),
        json.dumps({"events": [{"at": 2, "heal": True}, {"at": 1, "heal": True}]}),
        json.dumps({"events": [{"at": 1.0}]}),
    ],
)
def test_invalid_schedules_are_refused(tmp_path: Path, document: str) -> None:
    path = tmp_path / "faults.json"
    path.write_text(document)
    with pytest.raises(FaultSpecError):
        load_fault_schedule(path)
