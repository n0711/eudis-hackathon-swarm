"""Verify deterministic allocation and receiver-local connectivity evidence.

The tests ensure live authoritative peer movement cannot bypass delivered status.
"""

from __future__ import annotations

from eudis_swarm.agent import Agent
from eudis_swarm.peer_state import PeerKnowledgeState, PeerStateStore, PeerStatus
from eudis_swarm.task import Task
from eudis_swarm.task_allocator import (
    Allocation,
    CommunicationAwareTaskAllocator,
    TaskAllocator,
)


def _store(owner_id: int, peer_ids: tuple[int, ...]) -> PeerStateStore:
    return PeerStateStore(owner_id, peer_ids, stale_after=2.5)


def test_unknown_knowledge_falls_back_to_distance_ordering() -> None:
    agents = [
        Agent(agent_id=2, position=(10.0, 0.0), speed=1.0),
        Agent(agent_id=1, position=(0.0, 0.0), speed=1.0),
    ]
    tasks = [
        Task(task_id=2, position=(9.0, 0.0)),
        Task(task_id=1, position=(1.0, 0.0)),
    ]
    stores = {1: _store(1, (2,)), 2: _store(2, (1,))}

    baseline = TaskAllocator().allocate(agents, tasks)
    connectivity = CommunicationAwareTaskAllocator(stores, 3.0).allocate(agents, tasks)

    assert [(item.agent_id, item.task_id) for item in connectivity] == [
        (item.agent_id, item.task_id) for item in baseline
    ]
    assert all(item.predicted_peer_degree == 0 for item in connectivity)
    assert all(item.predicted_isolation is True for item in connectivity)


def test_fresh_peer_connectivity_changes_the_distance_only_choice() -> None:
    observer = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    peer = Agent(agent_id=2, position=(10.0, 0.0), speed=1.0)
    snapshot = peer.send_heartbeat(0.0)
    assert snapshot is not None
    store = _store(1, (2,))
    store.receive(snapshot, 0.0)
    near = Task(task_id=1, position=(1.0, 0.0))
    linked = Task(task_id=2, position=(7.0, 0.0))

    baseline = TaskAllocator().allocate((observer,), (near, linked))[0]
    allocator = CommunicationAwareTaskAllocator({1: store}, 3.0)
    near_evaluation = allocator.evaluate_candidate(observer, near)
    linked_evaluation = allocator.evaluate_candidate(observer, linked)
    connectivity = allocator.allocate((observer,), (near, linked))[0]

    assert (baseline.task_id, baseline.distance) == (1, 1.0)
    assert baseline.predicted_peer_degree is None
    assert near_evaluation.predicted_peer_degree == 0
    assert near_evaluation.predicted_isolation is True
    assert linked_evaluation.predicted_peer_degree == 1
    assert linked_evaluation.predicted_isolation is False
    assert connectivity.task_id == 2
    assert connectivity.distance == 7.0
    assert connectivity.predicted_peer_degree == 1
    assert connectivity.predicted_isolation is False


def test_stale_peer_is_excluded_and_does_not_make_agent_unavailable() -> None:
    observer = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    peer = Agent(agent_id=2, position=(10.0, 0.0), speed=1.0)
    snapshot = peer.send_heartbeat(0.0)
    assert snapshot is not None
    store = _store(1, (2,))
    store.receive(snapshot, 0.0)
    assert store.advance_time(2.5001) == (2,)
    assert store.state_for(2) is PeerKnowledgeState.STALE

    allocation = CommunicationAwareTaskAllocator({1: store}, 3.0).allocate(
        (observer,),
        (Task(task_id=1, position=(1.0, 0.0)), Task(task_id=2, position=(7.0, 0.0))),
    )[0]

    assert observer.available is True
    assert allocation.task_id == 1
    assert allocation.predicted_peer_degree == 0
    assert allocation.predicted_isolation is True


def test_silent_peer_is_excluded_even_while_raw_snapshot_is_fresh() -> None:
    observer = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    peer = Agent(agent_id=2, position=(10.0, 0.0), speed=1.0)
    snapshot = peer.send_heartbeat(0.0)
    assert snapshot is not None
    store = PeerStateStore(1, (2,), stale_after=10.0)
    store.receive(snapshot, 0.0)
    allocator = CommunicationAwareTaskAllocator({1: store}, 3.0)
    task = Task(task_id=1, position=(7.0, 0.0))

    assert allocator.evaluate_candidate(observer, task).predicted_peer_degree == 1
    assert store.observe_silence(2, timestamp=2.5001, silent_after=2.5) is True
    assert store.state_for(2) is PeerKnowledgeState.FRESH
    assert store.status_for(2) is PeerStatus.SILENT

    # raw freshness remains available for diagnostics, but decisions require heard.
    assert allocator.evaluate_candidate(observer, task).predicted_peer_degree == 0


def test_prediction_uses_last_delivery_until_refreshed() -> None:
    observer = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    peer = Agent(agent_id=2, position=(10.0, 0.0), speed=20.0)
    snapshot = peer.send_heartbeat(0.0)
    assert snapshot is not None
    store = _store(1, (2,))
    store.receive(snapshot, 0.0)

    peer.assign_task(99)
    peer.move_toward((-10.0, 0.0), 1.0)
    assert peer.position == (-10.0, 0.0)
    tasks = (
        Task(task_id=1, position=(7.0, 0.0)),
        Task(task_id=2, position=(-7.0, 0.0)),
    )

    first_choice = CommunicationAwareTaskAllocator(
        {1: store, 2: _store(2, (1,))}, 3.0
    ).allocate((observer, peer), tasks)[0]

    assert first_choice.agent_id == 1
    assert first_choice.task_id == 1
    assert first_choice.predicted_peer_degree == 1

    refreshed_snapshot = peer.send_heartbeat(1.0)
    assert refreshed_snapshot is not None
    store.receive(refreshed_snapshot, 1.0)
    second_choice = CommunicationAwareTaskAllocator({1: store}, 3.0).allocate(
        (observer,), tasks
    )[0]

    assert second_choice.task_id == 2
    assert second_choice.predicted_peer_degree == 1


def test_connectivity_ties_resolve_by_agent_then_task_id() -> None:
    agents = (
        Agent(agent_id=2, position=(0.0, 0.0), speed=1.0),
        Agent(agent_id=1, position=(0.0, 0.0), speed=1.0),
    )
    stores = {1: _store(1, (2,)), 2: _store(2, (1,))}
    tasks = (
        Task(task_id=2, position=(1.0, 0.0)),
        Task(task_id=1, position=(1.0, 0.0)),
    )

    allocations = CommunicationAwareTaskAllocator(stores, 3.0).allocate(agents, tasks)

    assert [(item.agent_id, item.task_id) for item in allocations] == [(1, 1), (2, 2)]


def test_connectivity_allocator_excludes_failed_agents() -> None:
    healthy = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    failed = Agent(agent_id=2, position=(10.0, 0.0), speed=1.0)
    failed.declare_failed()
    stores = {1: _store(1, (2,)), 2: _store(2, (1,))}

    allocation = CommunicationAwareTaskAllocator(stores, 3.0).allocate(
        (healthy, failed), (Task(task_id=1, position=(10.0, 0.0)),)
    )[0]

    assert allocation.agent_id == 1
    assert failed.current_task is None


def test_ranked_batch_matches_repeated_minimum_semantics() -> None:
    agents = (
        Agent(agent_id=1, position=(0.0, 0.0), speed=1.0),
        Agent(agent_id=2, position=(10.0, 0.0), speed=1.0),
        Agent(agent_id=3, position=(5.0, 8.0), speed=1.0),
    )
    stores = {
        agent.agent_id: _store(
            agent.agent_id,
            tuple(peer.agent_id for peer in agents if peer.agent_id != agent.agent_id),
        )
        for agent in agents
    }
    snapshots = {agent.agent_id: agent.send_heartbeat(0.0) for agent in agents}
    assert all(snapshot is not None for snapshot in snapshots.values())
    for owner_id, store in stores.items():
        for peer_id, snapshot in snapshots.items():
            if peer_id != owner_id:
                assert snapshot is not None
                store.receive(snapshot, 0.0)

    tasks = (
        Task(task_id=1, position=(2.0, 1.0)),
        Task(task_id=2, position=(8.0, 1.0)),
        Task(task_id=3, position=(5.0, 6.0)),
        Task(task_id=4, position=(20.0, 20.0)),
    )
    allocator = CommunicationAwareTaskAllocator(stores, 6.0)
    remaining_agents = {agent.agent_id: agent for agent in agents}
    remaining_tasks = {task.task_id: task for task in tasks}
    repeated_minimum = []
    while remaining_agents and remaining_tasks:
        evaluations = (
            allocator.evaluate_candidate(agent, task)
            for agent in remaining_agents.values()
            for task in remaining_tasks.values()
        )

        def score(item: Allocation) -> tuple[int, int, float, int, int]:
            degree = item.predicted_peer_degree
            isolation = item.predicted_isolation
            assert degree is not None and isolation is not None
            return (
                int(isolation),
                -degree,
                item.distance,
                item.agent_id,
                item.task_id,
            )

        selected = min(evaluations, key=score)
        repeated_minimum.append(selected)
        del remaining_agents[selected.agent_id]
        del remaining_tasks[selected.task_id]

    assert allocator.allocate(agents, tasks) == repeated_minimum
