import pytest

from eudis_swarm.agent import Agent, AgentStatus
from eudis_swarm.config import SimulationConfig
from eudis_swarm.mission import MissionEventKind
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import Task, TaskStatus


def test_failed_uav_task_is_reassigned_and_mission_completes() -> None:
    config = SimulationConfig(
        agent_count=3,
        task_count=3,
        agent_speed=1.0,
        time_step=0.5,
        completion_tolerance=0.01,
        heartbeat_interval=0.5,
        failure_timeout=1.0,
        failure_agent_id=2,
        failure_time=1.0,
        max_simulation_time=30.0,
    )
    agents = [
        Agent(agent_id=1, position=(0.0, 0.0), speed=1.0),
        Agent(agent_id=2, position=(10.0, 0.0), speed=1.0),
        Agent(agent_id=3, position=(20.0, 0.0), speed=1.0),
    ]
    tasks = [
        Task(task_id=1, position=(1.0, 0.0)),
        Task(task_id=2, position=(10.0, 10.0)),
        Task(task_id=3, position=(19.0, 0.0)),
    ]

    result = Simulation(config, agents=agents, tasks=tasks).run()
    metrics = result.metrics
    failed = result.mission.agents[2]
    orphan = metrics.recoveries[2]

    assert metrics.mission_completed is True
    assert metrics.completed_task_count == metrics.total_task_count == 3
    assert metrics.failed_agent_count == 1
    assert metrics.orphaned_task_count == 1
    assert metrics.reassigned_task_count == 1
    assert metrics.recovered_task_count == 1
    assert metrics.human_interventions == 0
    assert failed.status is AgentStatus.FAILED
    assert failed.current_task is None
    assert orphan.failed_agent_id == 2
    assert orphan.reassigned_agent_id in {1, 3}
    assert orphan.reassigned_agent_id != 2
    assert orphan.completed_at is not None
    assert result.mission.tasks[2].status is TaskStatus.COMPLETED
    assert result.mission.tasks[2].assigned_agent == orphan.reassigned_agent_id

    failure = metrics.failures[2]
    assert failure.injected_at == pytest.approx(1.0)
    assert failure.detected_at == pytest.approx(2.0)
    assert failure.last_heartbeat == pytest.approx(0.5)
    assert failure.detected_at is not None
    assert failure.last_heartbeat is not None
    assert failure.injected_at is not None
    assert failure.detected_at - failure.last_heartbeat > config.failure_timeout
    assert orphan.orphaned_at == failure.detected_at
    # the task is not handed on the instant its owner is declared dead: the
    # dead UAV's lease is still live, and no replica may overwrite it until
    # it lapses.  Reassignment follows lease expiry, not the declaration.
    assert orphan.reassigned_at is not None
    assert orphan.reassigned_at > orphan.orphaned_at
    assert orphan.reassigned_at == pytest.approx(3.5)

    frozen_positions = {
        position
        for timestamp, position in result.position_history[2]
        if timestamp >= failure.injected_at
    }
    assert frozen_positions == {(10.0, 1.0)}

    relevant = [
        event
        for event in result.mission.events
        if event.agent_id == 2 or event.task_id == 2
    ]
    kinds = [event.kind for event in relevant]
    expected_order = [
        MissionEventKind.TASK_ASSIGNED,
        MissionEventKind.FAILURE_INJECTED,
        MissionEventKind.HEARTBEAT_TIMEOUT,
        MissionEventKind.FAILURE_DECLARED,
        MissionEventKind.TASK_RELEASED,
        MissionEventKind.TASK_REASSIGNED,
        MissionEventKind.TASK_COMPLETED,
    ]
    cursor = -1
    for kind in expected_order:
        cursor = kinds.index(kind, cursor + 1)


def test_default_scenario_is_reproducible() -> None:
    first = Simulation(SimulationConfig()).run()
    second = Simulation(SimulationConfig()).run()

    first_signature = [
        (event.kind, event.timestamp, event.agent_id, event.task_id)
        for event in first.mission.events
    ]
    second_signature = [
        (event.kind, event.timestamp, event.agent_id, event.task_id)
        for event in second.mission.events
    ]

    assert first_signature == second_signature
    assert first.metrics.simulation_duration == second.metrics.simulation_duration
    assert (
        first.metrics.failure_detection_latencies
        == second.metrics.failure_detection_latencies
    )
    assert (
        first.metrics.task_reassignment_latencies
        == second.metrics.task_reassignment_latencies
    )
    assert first.metrics.mission_completed is True
    assert first.metrics.completed_task_count == 20
    assert first.metrics.failed_agent_count == 1
    assert first.metrics.orphaned_task_count == 1
    assert first.metrics.reassigned_task_count == 1
    assert first.metrics.recovered_task_count == 1
    assert set(first.metrics.recoveries) == {19}
    assert first.metrics.recoveries[19].reassigned_agent_id != 2
    default_kinds = {event.kind for event in first.mission.events}
    assert {
        MissionEventKind.FAILURE_INJECTED,
        MissionEventKind.HEARTBEAT_TIMEOUT,
        MissionEventKind.FAILURE_DECLARED,
        MissionEventKind.TASK_RELEASED,
        MissionEventKind.TASK_REASSIGNED,
        MissionEventKind.MISSION_COMPLETED,
    } <= default_kinds


def test_final_partial_interval_can_complete_the_mission() -> None:
    config = SimulationConfig(
        agent_count=1,
        task_count=1,
        agent_speed=1.0,
        time_step=2.0,
        completion_tolerance=0.0,
        heartbeat_interval=1.0,
        failure_timeout=2.5,
        failure_agent_id=1,
        failure_time=10.0,
        max_simulation_time=1.0,
    )
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    task = Task(task_id=1, position=(1.0, 0.0))

    result = Simulation(config, agents=[agent], tasks=[task]).run()

    assert result.metrics.mission_completed is True
    assert result.metrics.simulation_duration == 1.0
    assert task.status is TaskStatus.COMPLETED
