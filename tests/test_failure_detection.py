"""Protect failure detection from silence-only and scheduler regressions.
The tests require distributed evidence before recovery mutates world state."""

from eudis_swarm.agent import Agent, AgentStatus, Heartbeat
from eudis_swarm.config import SimulationConfig
from eudis_swarm.failure_manager import (
    FailureDeclaration,
    FailureManager,
    FailureVote,
)
from eudis_swarm.metrics import SimulationMetrics
from eudis_swarm.mission import Mission, MissionEventKind
from eudis_swarm.peer_state import PeerStateStore
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import Task, TaskStatus
from eudis_swarm.task_allocator import TaskAllocator


def _failure_stores(agent_ids: tuple[int, ...]) -> dict[int, PeerStateStore]:
    return {
        owner_agent_id: PeerStateStore(
            owner_agent_id,
            (
                peer_agent_id
                for peer_agent_id in agent_ids
                if peer_agent_id != owner_agent_id
            ),
            stale_after=2.0,
        )
        for owner_agent_id in agent_ids
    }


def _heartbeat(agent_id: int, timestamp: float) -> Heartbeat:
    return Heartbeat(
        agent_id=agent_id,
        position=(0.0, 0.0),
        status=AgentStatus.IDLE,
        current_task=None,
        timestamp=timestamp,
    )


def test_failure_evidence_does_not_order_independent_source_clocks() -> None:
    vote = FailureVote(
        voter_agent_id=1,
        suspected_agent_id=3,
        created_at=5.0,
        last_heartbeat=100.0,
        last_heard_at=4.0,
        task_id=None,
    )
    declaration = FailureDeclaration(
        agent_id=3,
        declarer_agent_id=1,
        detected_at=6.0,
        last_heartbeat=100.0,
        task_id=None,
        voter_agent_ids=(1, 2),
        required_votes=2,
    )

    assert vote.last_heartbeat > vote.created_at
    assert declaration.last_heartbeat > declaration.detected_at

    metrics = SimulationMetrics(total_task_count=1, agents_started=3)
    metrics.record_failure_detection(
        agent_id=3,
        timestamp=declaration.detected_at,
        last_heartbeat=declaration.last_heartbeat,
    )
    assert metrics.heartbeat_detection_delays == {3: -94.0}


def test_vote_freshness_uses_first_local_receipt_not_source_or_duplicate_time() -> None:
    stores = _failure_stores((1, 2, 3))
    stores[1].receive(_heartbeat(3, 100.0), received_at=1.0)
    manager = FailureManager(heartbeat_timeout=2.0, peer_state_stores=stores)

    assert len(manager.propose_votes(4.0, participating_agent_ids=(1,))) == 1
    remote_vote = FailureVote(
        voter_agent_id=2,
        suspected_agent_id=3,
        created_at=1_000.0,
        last_heartbeat=100.0,
        last_heard_at=999.0,
        task_id=None,
    )
    assert manager.record_vote(1, remote_vote, received_at=4.0) is True

    # A later copy is network activity, not a new evidence generation, and must
    # not extend the original receiver-local freshness window.
    assert manager.record_vote(1, remote_vote, received_at=5.5) is False
    manager.propose_votes(5.5, participating_agent_ids=(1,))

    assert manager.detect_declarations(6.1, participating_agent_ids=(1,)) == ()


def test_delayed_obsolete_vote_is_ignored_without_replacing_newer_evidence() -> None:
    stores = _failure_stores((1, 2, 3))
    stores[1].receive(_heartbeat(3, 101.0), received_at=1.0)
    manager = FailureManager(heartbeat_timeout=2.0, peer_state_stores=stores)
    manager.propose_votes(4.0, participating_agent_ids=(1,))
    newer = FailureVote(
        voter_agent_id=2,
        suspected_agent_id=3,
        created_at=5.0,
        last_heartbeat=101.0,
        last_heard_at=4.0,
        task_id=None,
    )
    obsolete = FailureVote(
        voter_agent_id=2,
        suspected_agent_id=3,
        created_at=6.0,
        last_heartbeat=100.0,
        last_heard_at=4.0,
        task_id=None,
    )

    assert manager.record_vote(1, newer, received_at=4.0) is True
    assert manager.record_vote(1, obsolete, received_at=4.1) is False

    declarations = manager.detect_declarations(4.1, participating_agent_ids=(1,))
    assert len(declarations) == 1
    assert declarations[0].voter_agent_ids == (1, 2)


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
