"""Protect failure detection from silence-only and scheduler regressions.
The tests require distributed evidence before recovery mutates world state."""

from eudis_swarm.agent import Agent, AgentStatus
from eudis_swarm.config import SimulationConfig
from eudis_swarm.failure_manager import FailureManager
from eudis_swarm.metrics import SimulationMetrics
from eudis_swarm.mission import Mission, MissionEventKind
from eudis_swarm.peer_state import PeerStateStore
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import Task, TaskStatus
from eudis_swarm.task_allocator import TaskAllocator


def test_singleton_silence_cannot_self_declare_failure() -> None:
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    task = Task(task_id=1, position=(100.0, 0.0))
    store = PeerStateStore(owner_agent_id=1, peer_agent_ids=(), stale_after=3.0)
    mission = Mission(
        agents=[agent],
        tasks=[task],
        allocator=TaskAllocator(),
        failure_manager=FailureManager(
            heartbeat_timeout=3.0,
            peer_state_stores={1: store},
        ),
        metrics=SimulationMetrics(total_task_count=1, agents_started=1),
    )
    mission.start(5.0)

    assert mission.detect_and_recover(7.999, ()) == []
    assert mission.detect_and_recover(8.0, ()) == []
    assert mission.detect_and_recover(8.001, ()) == []
    assert mission.detect_and_recover(20.0, ()) == []

    assert agent.status is AgentStatus.ACTIVE
    assert agent.available is False
    assert agent.current_task == task.task_id
    assert task.status is TaskStatus.ASSIGNED
    assert task.assigned_agent == agent.agent_id
    assert mission.metrics.failed_agent_count == 0
    assert mission.metrics.orphaned_task_count == 0
    assert (
        sum(event.kind is MissionEventKind.FAILURE_DECLARED for event in mission.events)
        == 0
    )


def test_coarse_movement_step_does_not_skip_scheduled_heartbeats() -> None:
    config = SimulationConfig(
        agent_count=3,
        task_count=3,
        agent_speed=0.1,
        time_step=5.0,
        heartbeat_interval=1.0,
        failure_timeout=2.5,
        failure_agent_id=2,
        failure_time=5.0,
        max_simulation_time=10.0,
        communication_range=500.0,
    )
    agents = [
        Agent(agent_id=1, position=(0.0, 0.0), speed=0.1),
        Agent(agent_id=2, position=(100.0, 0.0), speed=0.1),
        Agent(agent_id=3, position=(200.0, 0.0), speed=0.1),
    ]
    tasks = [
        Task(task_id=1, position=(0.0, 100.0)),
        Task(task_id=2, position=(100.0, 100.0)),
        Task(task_id=3, position=(200.0, 100.0)),
    ]

    result = Simulation(config, agents=agents, tasks=tasks).run()
    failure = result.metrics.failures[2]

    assert failure.injected_at == 5.0
    assert failure.last_heartbeat == 4.0
    assert failure.detected_at == 7.0
    assert result.metrics.failure_detection_latencies[2] == 2.0


def test_time_zero_failure_preserves_the_startup_publication_contract() -> None:
    config = SimulationConfig(
        agent_count=3,
        task_count=3,
        agent_speed=0.1,
        time_step=1.0,
        heartbeat_interval=1.0,
        failure_timeout=1.0,
        failure_agent_id=2,
        failure_time=0.0,
        max_simulation_time=3.0,
        communication_range=500.0,
    )
    agents = [
        Agent(agent_id=1, position=(0.0, 0.0), speed=0.1),
        Agent(agent_id=2, position=(100.0, 0.0), speed=0.1),
        Agent(agent_id=3, position=(200.0, 0.0), speed=0.1),
    ]
    tasks = [
        Task(task_id=1, position=(0.0, 100.0)),
        Task(task_id=2, position=(100.0, 100.0)),
        Task(task_id=3, position=(200.0, 100.0)),
    ]

    result = Simulation(config, agents=agents, tasks=tasks).run()
    assert result.peer_state_stores is not None
    observation = result.peer_state_stores[1].observation_for(2)
    failure = result.metrics.failures[2]

    # startup publishes a baseline snapshot before applying a time-zero fault.
    assert observation is not None
    assert observation.snapshot.timestamp == 0.0
    assert failure.injected_at == 0.0
    assert failure.last_heartbeat == 0.0
    assert failure.detected_at == 2.0
