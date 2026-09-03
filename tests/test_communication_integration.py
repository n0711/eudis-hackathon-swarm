from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from eudis_swarm.agent import Agent, AgentStatus
from eudis_swarm.config import SimulationConfig
from eudis_swarm.mission import MissionEventKind
from eudis_swarm.simulation import (
    CommunicationEventKind,
    Simulation,
    _parser,
    main,
)
from eudis_swarm.task import Task, TaskStatus


def _mission_signature(
    result: object,
) -> list[tuple[object, float, int | None, int | None]]:
    mission = result.mission  # type: ignore[attr-defined]
    return [
        (event.kind, event.timestamp, event.agent_id, event.task_id)
        for event in mission.events
    ]


def test_jamming_is_misread_as_failure_without_ownership_reconciliation() -> None:
    """A jammed-but-healthy UAV is wrongly declared dead by a peer quorum.

    This is the honest consequence of removing the link oracle.  Nothing local
    distinguishes a jammed peer from a destroyed one, so the three connected
    peers reach quorum on UAV 2 and the world releases its work.  Defending the
    "communication loss != UAV failure" claim is the job of the distributed
    ownership layer, not of privileged knowledge inside the detector.
    """

    config = SimulationConfig(
        failure_time=100.0,
        communication_range=130.0,
        comm_fault_agent_id=2,
        comm_fault_start=4.0,
        comm_fault_end=8.0,
    )

    result = Simulation(config).run()
    metrics = result.metrics
    agent = result.mission.agents[2]

    assert metrics.mission_completed is True
    assert metrics.completed_task_count == 20
    assert metrics.simulation_duration == pytest.approx(10.75)
    # UAV 2 was never physically harmed: no failure was ever injected into it.
    assert agent.failure_injected_at is None

    # ...yet the swarm declared it dead and took its work away, before
    # first-hand contact on reconnection overturned the verdict.
    assert metrics.declaration_retraction_count == 1
    assert metrics.failed_agent_count == 0
    assert metrics.orphaned_task_count == 1
    assert metrics.reassigned_task_count == 1
    assert result.mission.tasks[19].status is TaskStatus.COMPLETED
    assert result.mission.tasks[19].assigned_agent == 2

    # the full physical-recovery path runs on a UAV that never physically failed.
    physical_recovery_kinds = {
        MissionEventKind.FAILURE_DECLARED,
        MissionEventKind.TASK_RELEASED,
        MissionEventKind.TASK_REASSIGNED,
    }
    fired = {event.kind for event in result.mission.events} & physical_recovery_kinds
    assert fired == physical_recovery_kinds

    # UAV 2 finishes the work it could reach before the quorum stopped it.
    agent_completions = [
        (event.task_id, event.timestamp)
        for event in result.mission.events
        if event.kind is MissionEventKind.TASK_COMPLETED and event.agent_id == 2
    ]
    # Tasks 19 and 15 finish during the outage; Task 20 is duplicated while
    # UAV 2 is believed dead, then it rejoins and completes Task 20 for real.
    assert agent_completions == [(19, 4.0), (15, 4.75), (20, 10.5)]

    positions_during_outage = {
        position
        for timestamp, position in result.position_history[2]
        if config.comm_fault_start <= timestamp <= config.comm_fault_end
    }
    assert len(positions_during_outage) > 1

    communication_events = result.communication_events
    assert [
        event.timestamp
        for event in communication_events
        if event.kind is CommunicationEventKind.FAULT_STARTED
    ] == [4.0]
    assert [
        event.timestamp
        for event in communication_events
        if event.kind is CommunicationEventKind.FAULT_ENDED
    ] == [8.0]
    assert any(
        event.kind is CommunicationEventKind.AGENT_UNREACHABLE
        and event.agent_id == 2
        and event.timestamp == 4.0
        for event in communication_events
    )
    assert any(
        event.kind is CommunicationEventKind.AGENT_REACHABLE
        and event.agent_id == 2
        and event.timestamp == 8.0
        for event in communication_events
    )
    assert any(
        event.kind is CommunicationEventKind.NETWORK_PARTITIONED
        and event.timestamp == 4.0
        for event in communication_events
    )
    assert any(
        event.kind is CommunicationEventKind.NETWORK_RECONNECTED
        and event.timestamp == 8.0
        for event in communication_events
    )

    assert metrics.initial_link_count == 6
    assert metrics.minimum_link_count == 3
    assert metrics.maximum_connected_component_count == 2
    assert metrics.isolation_event_count == 1
    assert metrics.link_loss_event_count == 3
    assert metrics.link_restoration_event_count == 3
    assert metrics.total_communication_degraded_duration == pytest.approx(4.0)
    assert metrics.network_ended_connected is True
    assert metrics.healthy_unreachable_event_count == 1
    assert result.communication_graph is not None
    assert result.communication_graph.link_count == 6
    assert result.communication_graph.is_fully_connected is True
    assert result.communication_graph.blocked_agent_ids == frozenset()


def test_off_grid_communication_boundaries_do_not_change_physical_events() -> None:
    baseline = Simulation(SimulationConfig()).run()
    with_fault = Simulation(
        SimulationConfig(
            comm_fault_agent_id=3,
            comm_fault_start=1.125,
            comm_fault_end=1.875,
        )
    ).run()

    assert _mission_signature(with_fault) == _mission_signature(baseline)
    assert with_fault.metrics.completed_task_ids == baseline.metrics.completed_task_ids
    assert (
        with_fault.metrics.failure_detection_latencies
        == baseline.metrics.failure_detection_latencies
    )
    assert (
        with_fault.metrics.task_reassignment_latencies
        == baseline.metrics.task_reassignment_latencies
    )
    assert [
        (event.kind, event.timestamp)
        for event in with_fault.communication_events
        if event.kind
        in {CommunicationEventKind.FAULT_STARTED, CommunicationEventKind.FAULT_ENDED}
    ] == [
        (CommunicationEventKind.FAULT_STARTED, 1.125),
        (CommunicationEventKind.FAULT_ENDED, 1.875),
    ]
    assert with_fault.metrics.total_communication_degraded_duration == pytest.approx(
        0.75
    )


def test_graph_only_boundaries_do_not_round_physical_movement_differently() -> None:
    speed = 0.06779116727398721
    target = (2.557694272740827, 4.510107468035814)
    baseline_config = SimulationConfig(
        agent_count=1,
        task_count=1,
        agent_speed=speed,
        time_step=1.0,
        completion_tolerance=5.117078104563409,
        heartbeat_interval=1.0,
        failure_timeout=2.5,
        failure_agent_id=1,
        failure_time=100.0,
        max_simulation_time=3.0,
        communication_range=1.0,
    )

    baseline = Simulation(
        baseline_config,
        agents=[Agent(agent_id=1, position=(0.0, 0.0), speed=speed)],
        tasks=[Task(task_id=1, position=target)],
    ).run()
    with_fault = Simulation(
        replace(
            baseline_config,
            comm_fault_agent_id=1,
            comm_fault_start=0.113,
            comm_fault_end=0.732,
        ),
        agents=[Agent(agent_id=1, position=(0.0, 0.0), speed=speed)],
        tasks=[Task(task_id=1, position=target)],
    ).run()

    assert _mission_signature(with_fault) == _mission_signature(baseline)
    assert baseline.metrics.simulation_duration == 1.0
    assert with_fault.metrics.simulation_duration == 1.0
    assert baseline.mission.agents[1].position == with_fault.mission.agents[1].position


def test_initial_isolation_is_a_baseline_not_a_transition_event() -> None:
    config = SimulationConfig(
        agent_count=2,
        task_count=2,
        agent_speed=1.0,
        communication_range=1.0,
        failure_agent_id=2,
        failure_time=100.0,
    )
    agents = [
        Agent(agent_id=1, position=(0.0, 0.0), speed=1.0),
        Agent(agent_id=2, position=(100.0, 0.0), speed=1.0),
    ]
    tasks = [
        Task(task_id=1, position=(0.0, 0.0)),
        Task(task_id=2, position=(100.0, 0.0)),
    ]

    result = Simulation(config, agents=agents, tasks=tasks).run()

    assert result.metrics.initial_link_count == 0
    assert result.metrics.maximum_connected_component_count == 2
    assert result.metrics.isolation_event_count == 0
    assert result.metrics.healthy_unreachable_event_count == 0
    assert result.metrics.total_communication_degraded_duration == pytest.approx(0.0)
    assert result.metrics.network_ended_connected is False
    assert not any(
        event.kind is CommunicationEventKind.AGENT_UNREACHABLE
        for event in result.communication_events
    )


def test_isolated_uav_keeps_a_long_running_task_through_the_outage() -> None:
    config = SimulationConfig(
        agent_count=3,
        task_count=3,
        agent_speed=1.0,
        time_step=0.5,
        completion_tolerance=0.0,
        communication_range=20.0,
        failure_agent_id=1,
        failure_time=100.0,
        comm_fault_agent_id=2,
        comm_fault_start=1.0,
        comm_fault_end=3.0,
        max_simulation_time=20.0,
    )
    agents = [
        Agent(agent_id=1, position=(0.0, 0.0), speed=1.0),
        Agent(agent_id=2, position=(10.0, 0.0), speed=1.0),
        Agent(agent_id=3, position=(0.0, 10.0), speed=1.0),
    ]
    tasks = [
        Task(task_id=1, position=(0.0, 5.0)),
        Task(task_id=2, position=(10.0, 5.0)),
        Task(task_id=3, position=(0.0, 15.0)),
    ]

    result = Simulation(config, agents=agents, tasks=tasks).run()

    task_events = [event for event in result.mission.events if event.task_id == 2]
    assert [event.kind for event in task_events] == [
        MissionEventKind.TASK_ASSIGNED,
        MissionEventKind.TASK_COMPLETED,
    ]
    assert task_events[0].agent_id == task_events[1].agent_id == 2
    assert task_events[0].timestamp == 0.0
    assert task_events[1].timestamp == 5.0
    assert result.mission.tasks[2].assigned_agent == 2
    assert result.mission.agents[2].responsive is True
    assert result.mission.agents[2].status is AgentStatus.IDLE
    assert result.metrics.failed_agent_count == 0
    assert result.metrics.orphaned_task_count == 0
    assert (
        len(
            {
                position
                for timestamp, position in result.position_history[2]
                if config.comm_fault_start <= timestamp <= config.comm_fault_end
            }
        )
        > 1
    )


def test_prototype_0_1_physical_recovery_golden_is_unchanged() -> None:
    result = Simulation(SimulationConfig()).run()
    failure = result.metrics.failures[2]
    recovery = result.metrics.recoveries[19]

    assert result.metrics.mission_completed is True
    assert result.metrics.simulation_duration == 17.25
    assert failure.injected_at == 4.0
    assert failure.last_heartbeat == 3.0
    assert failure.detected_at == 5.75
    assert recovery.orphaned_at == 5.75
    assert recovery.reassigned_agent_id == 1
    assert recovery.reassigned_at == 10.75
    assert recovery.completed_at == 17.25
    assert result.mission.agents[2].status is AgentStatus.FAILED


@pytest.mark.parametrize(
    "changes",
    [
        {"communication_range": 0.0},
        {"communication_range": float("nan")},
        {"comm_fault_agent_id": 0},
        {"comm_fault_agent_id": 5},
        {"comm_fault_start": -1.0},
        {"comm_fault_start": float("nan")},
        {"comm_fault_end": float("inf")},
        {"comm_fault_start": 4.0, "comm_fault_end": 4.0},
        {"comm_fault_start": 5.0, "comm_fault_end": 4.0},
    ],
)
def test_communication_configuration_is_validated(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SimulationConfig(**changes)  # type: ignore[arg-type]


def test_communication_cli_arguments_are_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = _parser().parse_args([])
    assert arguments.communication_range == 130.0
    assert arguments.comm_fault_agent is None
    assert arguments.comm_fault_start == 4.0
    assert arguments.comm_fault_end == 8.0
    assert arguments.peer_state_stale_after == 2.5
    assert arguments.allocation_policy == "distance"

    captured: dict[str, SimulationConfig] = {}

    def fake_run(config: SimulationConfig) -> object:
        captured["config"] = config
        return SimpleNamespace(metrics=SimpleNamespace(mission_completed=True))

    monkeypatch.setattr("eudis_swarm.simulation.configure_logging", lambda _level: None)
    monkeypatch.setattr("eudis_swarm.simulation.run_simulation", fake_run)

    exit_code = main(
        [
            "--communication-range",
            "42",
            "--comm-fault-agent",
            "3",
            "--comm-fault-start",
            "1.25",
            "--comm-fault-end",
            "2.75",
            "--peer-state-stale-after",
            "3.5",
            "--allocation-policy",
            "connectivity",
        ]
    )

    assert exit_code == 0
    assert captured["config"].communication_range == 42.0
    assert captured["config"].comm_fault_agent_id == 3
    assert captured["config"].comm_fault_start == 1.25
    assert captured["config"].comm_fault_end == 2.75
    assert captured["config"].peer_state_stale_after == 3.5
    assert captured["config"].allocation_policy == "connectivity"


def test_a_wrongly_declared_uav_keeps_flying_and_duplicates_the_work() -> None:
    """A declaration is belief, and belief does not reach the vehicle.

    UAV 2 is jammed from t=4 to t=8 and never physically harmed.  Its peers
    reach quorum and declare it dead, so the coordinator hands its task to
    somebody else.  UAV 2 hears none of this: it keeps flying, keeps
    transmitting, and finishes the task anyway.  Two UAVs do the same work and
    the swarm pays for it twice.
    """

    result = Simulation(
        SimulationConfig(
            failure_time=100.0,
            communication_range=130.0,
            comm_fault_agent_id=2,
            comm_fault_start=4.0,
            comm_fault_end=8.0,
        )
    ).run()
    metrics = result.metrics
    agent = result.mission.agents[2]

    # the swarm declared UAV 2 dead while it was jammed...
    declared = [
        event
        for event in result.mission.events
        if event.kind is MissionEventKind.FAILURE_DECLARED
    ]
    assert [event.agent_id for event in declared] == [2]

    # ...the vehicle was never touched...
    assert agent.responsive is True
    assert agent.failure_injected_at is None

    # ...and once the link returned, first-hand contact overturned the verdict.
    assert metrics.declaration_retraction_count == 1
    assert metrics.failed_agent_count == 0
    assert agent.wrongly_declared is False

    # the coordinator reassigned work UAV 2 never let go of.
    assert metrics.belief_divergence_event_count == 1
    assert metrics.orphaned_task_count == 1
    assert metrics.reassigned_task_count == 1

    # UAV 2's effort is real but invisible: nobody could hear it report.
    assert metrics.duplicated_task_completion_count == 1
    duplicated = [
        event
        for event in result.mission.events
        if event.kind is MissionEventKind.TASK_DUPLICATED
    ]
    assert [event.agent_id for event in duplicated] == [2]

    # the mission still finishes, having done twenty tasks' work plus one.
    assert metrics.mission_completed is True
    assert metrics.completed_task_count == 20


def test_a_physically_dead_uav_stops_and_releases_its_work() -> None:
    """The genuine fail-stop path is unchanged by the belief/truth split."""

    result = Simulation(SimulationConfig()).run()
    metrics = result.metrics
    agent = result.mission.agents[2]

    assert agent.responsive is False
    assert agent.failure_injected_at == 4.0
    assert agent.wrongly_declared is False
    assert agent.current_task is None

    # a real failure produces no divergence and no duplicated effort.
    assert metrics.belief_divergence_event_count == 0
    assert metrics.duplicated_task_completion_count == 0
    assert metrics.simulation_duration == 17.25
    assert metrics.completed_task_count == 20


def test_a_rejoining_uav_surrenders_work_reassigned_while_it_was_believed_dead() -> (
    None
):
    """Rejoining must not resurrect a claim on work somebody else now owns.

    Here UAV 2 flies too slowly to reach its task before the link returns, so
    unlike the faster scenario above it is still holding the task pointer when
    the declaration is withdrawn. That pointer names work the coordinator has
    since given to a peer, and keeping it would break bidirectional ownership.
    """

    result = Simulation(
        SimulationConfig(
            failure_time=100.0,
            communication_range=130.0,
            comm_fault_agent_id=2,
            comm_fault_start=3.0,
            comm_fault_end=6.0,
            agent_speed=3.0,
            heartbeat_interval=0.5,
        )
    ).run()
    metrics = result.metrics
    agent = result.mission.agents[2]

    assert metrics.declaration_retraction_count == 1
    assert metrics.rejoin_surrendered_task_count == 1

    # the reinstated UAV holds nothing it does not own, and the mission finishes.
    assert agent.wrongly_declared is False
    if agent.current_task is not None:
        assert result.mission.tasks[agent.current_task].assigned_agent == 2
    result.mission.assert_consistent()
    assert metrics.mission_completed is True
    assert metrics.completed_task_count == 20
