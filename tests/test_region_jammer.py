"""Exercise explicit blocked links and the circular region jammer.

Covers ``RegionJammer`` geometry and timing, ``SimulationConfig`` validation and
canonicalisation of ``blocked_links``, the CLI surface, and a clean balanced
partition driven from the main simulation.
"""

from __future__ import annotations

import math

import pytest

from eudis_swarm.agent import Agent
from eudis_swarm.communication import RegionJammer
from eudis_swarm.config import SimulationConfig
from eudis_swarm.simulation import (
    CommunicationEventKind,
    Simulation,
    SimulationResult,
    _parse_blocked_link,
    _parse_jammer,
    _parser,
    main,
)
from eudis_swarm.task import Task


# four UAVs pinned to two columns; each has one nearby task on its own column so
# the greedy allocator keeps every UAV on its side for the whole run.  Fresh
# instances per run: Agent and Task carry mutable simulation state.
def _split_agents() -> list[Agent]:
    return [
        Agent(agent_id=1, position=(10.0, 10.0), speed=6.0),
        Agent(agent_id=2, position=(10.0, 90.0), speed=6.0),
        Agent(agent_id=3, position=(90.0, 10.0), speed=6.0),
        Agent(agent_id=4, position=(90.0, 90.0), speed=6.0),
    ]


def _split_tasks() -> list[Task]:
    return [
        Task(task_id=1, position=(10.0, 45.0)),
        Task(task_id=2, position=(10.0, 55.0)),
        Task(task_id=3, position=(90.0, 45.0)),
        Task(task_id=4, position=(90.0, 55.0)),
    ]


def _split_config(**overrides: object) -> SimulationConfig:
    base: dict[str, object] = dict(
        agent_count=4,
        task_count=4,
        agent_speed=6.0,
        failure_agent_id=2,
        failure_time=100.0,
        communication_range=130.0,
        max_simulation_time=10.0,
    )
    base.update(overrides)
    return SimulationConfig(**base)  # type: ignore[arg-type]


# --- RegionJammer unit behaviour -------------------------------------------


def test_region_jammer_is_active_only_inside_its_half_open_window() -> None:
    jammer = RegionJammer(0.0, 0.0, 5.0, 2.0, 6.0)
    assert jammer.active_at(1.999) is False
    assert jammer.active_at(2.0) is True
    assert jammer.active_at(5.999) is True
    assert jammer.active_at(6.0) is False


def test_region_jammer_blocks_a_link_by_its_midpoint() -> None:
    jammer = RegionJammer(50.0, 50.0, 10.0, 0.0, 1.0)
    # midpoint (50, 50): squarely inside.
    assert jammer.blocks_link((40.0, 50.0), (60.0, 50.0)) is True
    # midpoint (50, 65): 15 units away, outside radius 10.
    assert jammer.blocks_link((40.0, 65.0), (60.0, 65.0)) is False
    # endpoints inside the disc but midpoint (50, 61) just outside.
    assert jammer.blocks_link((45.0, 61.0), (55.0, 61.0)) is False


@pytest.mark.parametrize(
    "args",
    [
        (0.0, 0.0, 0.0, 0.0, 1.0),  # radius must be positive
        (0.0, 0.0, -1.0, 0.0, 1.0),
        (math.nan, 0.0, 1.0, 0.0, 1.0),  # non-finite centre
        (0.0, math.inf, 1.0, 0.0, 1.0),
        (0.0, 0.0, 1.0, -1.0, 1.0),  # negative start
        (0.0, 0.0, 1.0, 5.0, 5.0),  # end must exceed start
        (0.0, 0.0, 1.0, 5.0, 4.0),
    ],
)
def test_region_jammer_rejects_invalid_parameters(
    args: tuple[float, float, float, float, float],
) -> None:
    with pytest.raises(ValueError):
        RegionJammer(*args)


# --- SimulationConfig plumbing -------------------------------------------------


def test_blocked_links_are_validated_and_canonicalised() -> None:
    config = _split_config(blocked_links=frozenset({(3, 1), (1, 4), (4, 1)}))
    # (3, 1) -> (1, 3); (1, 4) and (4, 1) collapse to one canonical pair.
    assert config.blocked_links == frozenset({(1, 3), (1, 4)})


@pytest.mark.parametrize(
    "blocked_links",
    [
        frozenset({(1, 1)}),  # self-link
        frozenset({(1, 9)}),  # unknown UAV
        frozenset({(0, 2)}),
        frozenset({(1, 2, 3)}),  # not a pair
        {("1", "2")},  # not integers
        42,  # not iterable
    ],
)
def test_invalid_blocked_links_are_rejected(blocked_links: object) -> None:
    with pytest.raises(ValueError):
        _split_config(blocked_links=blocked_links)


def test_region_jammer_must_be_the_right_type() -> None:
    with pytest.raises(ValueError):
        _split_config(region_jammer="middle")  # type: ignore[arg-type]


# --- CLI surface ------------------------------------------------------------


def test_cli_parses_repeatable_blocked_links_and_a_jammer() -> None:
    parsed = _parser().parse_args(
        [
            "--blocked-link",
            "1:3",
            "--blocked-link",
            "2:4",
            "--jammer",
            "50,50,25,2,6",
        ]
    )
    assert parsed.blocked_link == ["1:3", "2:4"]
    assert parsed.jammer == "50,50,25,2,6"

    assert _parse_blocked_link("2:4") == (2, 4)
    jammer = _parse_jammer("50,50,25,2,6")
    assert jammer == RegionJammer(50.0, 50.0, 25.0, 2.0, 6.0)


@pytest.mark.parametrize(
    "spec",
    ["1", "1:2:3", "a:2"],
)
def test_parse_blocked_link_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        _parse_blocked_link(spec)


@pytest.mark.parametrize(
    "spec",
    ["1,2,3", "50,50,25,2", "50,50,x,2,6"],
)
def test_parse_jammer_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        _parse_jammer(spec)


def test_cli_reports_invalid_blocked_link_as_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["--blocked-link", "1:1", "--failure-time", "100"])
    assert "blocked_links" in capsys.readouterr().err


# --- integration: a clean balanced partition from the main simulation --------


def _component_history(result: SimulationResult) -> dict[float, int]:
    assert result.trace is not None
    seen: dict[float, int] = {}
    for frame in result.trace.frames:
        seen.setdefault(round(frame.timestamp, 6), len(frame.components))
    return seen


def test_region_jammer_creates_a_clean_two_plus_two_split() -> None:
    config = _split_config(
        region_jammer=RegionJammer(50.0, 50.0, 35.0, 1.0, 4.0),
    )
    result = Simulation(
        config,
        agents=_split_agents(),
        tasks=_split_tasks(),
        capture_trace=True,
    ).run()

    assert result.trace is not None
    history = _component_history(result)
    during = [count for t, count in history.items() if 1.0 <= t < 4.0]
    before_after = [count for t, count in history.items() if t < 1.0 or t >= 4.0]

    assert during and all(count == 2 for count in during)
    assert before_after and all(count == 1 for count in before_after)

    # the split is exactly {1, 2} | {3, 4}.
    mid_frame = next(
        frame for frame in result.trace.frames if 1.0 <= frame.timestamp < 4.0
    )
    assert {frozenset(component) for component in mid_frame.components} == {
        frozenset({1, 2}),
        frozenset({3, 4}),
    }

    partition_times = [
        event.timestamp
        for event in result.communication_events
        if event.kind is CommunicationEventKind.NETWORK_PARTITIONED
    ]
    reconnect_times = [
        event.timestamp
        for event in result.communication_events
        if event.kind is CommunicationEventKind.NETWORK_RECONNECTED
    ]
    assert partition_times == [1.0]
    assert reconnect_times == [4.0]
    assert result.metrics.mission_completed is True
    assert result.metrics.maximum_connected_component_count == 2


def test_static_blocked_links_partition_the_main_simulation() -> None:
    config = _split_config(
        blocked_links=frozenset({(1, 3), (1, 4), (2, 3), (2, 4)}),
    )
    result = Simulation(
        config,
        agents=_split_agents(),
        tasks=_split_tasks(),
        capture_trace=True,
    ).run()

    assert result.communication_graph is not None
    assert result.communication_graph.blocked_links == frozenset(
        {(1, 3), (1, 4), (2, 3), (2, 4)}
    )
    # a static block holds for the whole run: always two components.
    assert set(_component_history(result).values()) == {2}
    assert result.metrics.maximum_connected_component_count == 2
    assert not any(
        event.kind is CommunicationEventKind.NETWORK_RECONNECTED
        for event in result.communication_events
    )
