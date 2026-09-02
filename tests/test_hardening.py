"""Exercise validation and lifecycle boundaries across the simulation core.

These tests keep malformed state and regressing logical time out of every layer.
"""

from __future__ import annotations

from math import inf, nan

import pytest

from eudis_swarm.agent import Agent, AgentStatus, Heartbeat
from eudis_swarm.communication import CommunicationGraph
from eudis_swarm.config import SimulationConfig
from eudis_swarm.failure_manager import FailureManager
from eudis_swarm.metrics import SimulationMetrics
from eudis_swarm.mission import Mission, MissionEventKind, MissionState
from eudis_swarm.peer_state import PeerStateStore
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import Task, TaskStatus
from eudis_swarm.task_allocator import TaskAllocator


def _mission() -> Mission:
    return Mission(
        agents=[Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)],
        tasks=[Task(task_id=1, position=(10.0, 0.0))],
        allocator=TaskAllocator(),
        failure_manager=FailureManager(heartbeat_timeout=2.0),
        metrics=SimulationMetrics(total_task_count=1, agents_started=1),
    )


def test_initially_satisfied_tasks_complete_at_zero_time() -> None:
    config = SimulationConfig(
        agent_count=1,
        task_count=2,
        agent_speed=1.0,
        completion_tolerance=0.0,
        failure_agent_id=1,
        failure_time=100.0,
        communication_range=1.0,
    )
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    tasks = [
        Task(task_id=1, position=(0.0, 0.0)),
        Task(task_id=2, position=(0.0, 0.0)),
    ]

    result = Simulation(config, agents=[agent], tasks=tasks).run()

    completion_events = [
        event
        for event in result.mission.events
        if event.kind is MissionEventKind.TASK_COMPLETED
    ]
    assert result.metrics.simulation_duration == 0.0
    assert [event.timestamp for event in completion_events] == [0.0, 0.0]
    assert result.mission.state is MissionState.COMPLETED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_count", 1.5),
        ("agent_count", True),
        ("agent_count", 0),
        ("task_count", 0),
        ("task_count", False),
        ("random_seed", 1.5),
        ("random_seed", True),
        ("area_width", nan),
        ("area_height", inf),
        ("agent_speed", -inf),
        ("time_step", 0.0),
        ("heartbeat_interval", False),
        ("failure_timeout", nan),
        ("peer_state_stale_after", nan),
        ("peer_state_stale_after", inf),
        ("peer_state_stale_after", 0.0),
        ("peer_state_stale_after", False),
        ("max_simulation_time", -1.0),
        ("communication_range", inf),
        ("completion_tolerance", -1.0),
        ("failure_time", nan),
        ("comm_fault_start", -1.0),
        ("comm_fault_end", inf),
        ("failure_agent_id", True),
        ("comm_fault_agent_id", False),
    ],
)
def test_invalid_configuration_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        SimulationConfig(**{field: value})  # type: ignore[arg-type]


def test_invalid_allocation_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="allocation_policy"):
        SimulationConfig(allocation_policy="unknown")


def test_lifecycle_rejects_operations_outside_running_state() -> None:
    mission = _mission()

    with pytest.raises(RuntimeError):
        mission.allocate_tasks(0.0)
    with pytest.raises(RuntimeError):
        mission.complete_task(1, 1, 0.0)
    with pytest.raises(RuntimeError):
        mission.inject_failure(1, 0.0)

    mission.start(1.0)
    assert mission.state is MissionState.RUNNING
    with pytest.raises(RuntimeError):
        mission.start(1.0)
    with pytest.raises(ValueError, match="backwards"):
        mission.exchange_heartbeats(0.5)

    mission.finish(2.0, completed=False)
    assert mission.state is MissionState.TIMED_OUT
    with pytest.raises(RuntimeError):
        mission.allocate_tasks(2.0)
    with pytest.raises(RuntimeError):
        mission.finish(2.0, completed=False)


def test_communication_update_timestamps_cannot_regress() -> None:
    config = SimulationConfig(failure_time=100.0)
    simulation = Simulation(config)

    simulation._update_communication_graph(1.0)
    with pytest.raises(ValueError, match="backwards"):
        simulation._update_communication_graph(0.5)


def test_heartbeat_and_metrics_timestamps_cannot_regress() -> None:
    store = PeerStateStore(2, (1,), stale_after=2.0)
    store.receive(Heartbeat(1, (0.0, 0.0), AgentStatus.IDLE, None, 2.0), 2.0)
    with pytest.raises(ValueError, match="backwards"):
        store.receive(
            Heartbeat(1, (0.0, 0.0), AgentStatus.IDLE, None, 1.0),
            3.0,
        )

    metrics = SimulationMetrics(total_task_count=1, agents_started=1)
    metrics.record_task_completion(1, 2.0)
    with pytest.raises(ValueError, match="backwards"):
        metrics.record_failure_injection(1, 1.0, (0.0, 0.0))


def test_assignment_and_graph_invariants_hold_after_recovery() -> None:
    active_mission = _mission()
    active_mission.start(0.0)
    active_agent = active_mission.agents[1]
    active_task = active_mission.tasks[1]
    assert active_agent.current_task == active_task.task_id
    assert active_task.assigned_agent == active_agent.agent_id

    result = Simulation(SimulationConfig()).run()

    current_task_ids = [
        agent.current_task
        for agent in result.mission.agents.values()
        if agent.current_task is not None
    ]
    assert len(current_task_ids) == len(set(current_task_ids))
    for task in result.mission.tasks.values():
        if task.status is TaskStatus.ASSIGNED:
            assert task.assigned_agent is not None
            owner = result.mission.agents[task.assigned_agent]
            assert owner.current_task == task.task_id
            assert owner.status is AgentStatus.ACTIVE
    assert all(
        agent.current_task is None
        for agent in result.mission.agents.values()
        if agent.status is AgentStatus.FAILED
    )

    graph = CommunicationGraph(agent_ids=[3, 1, 2], communication_range=2.0)
    graph.update({1: (0.0, 0.0), 2: (1.0, 0.0), 3: (0.0, 1.0)})
    keys = [link.key for link in graph.links]
    assert keys == [(1, 2), (1, 3), (2, 3)]
    assert len(keys) == len(set(keys))
    for agent_id in graph.agent_ids:
        for neighbor_id in graph.neighbors(agent_id):
            assert agent_id in graph.neighbors(neighbor_id)
