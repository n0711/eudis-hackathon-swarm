"""Verify receiver-local peer evidence transitions and delivery boundaries.
The tests keep freshness, reachability, silence, and declared failure distinct."""

from __future__ import annotations

import pytest

from eudis_swarm.agent import Agent, AgentStatus, Heartbeat
from eudis_swarm.communication import CommunicationGraph
from eudis_swarm.messaging import PeerStateTransport
from eudis_swarm.peer_state import PeerKnowledgeState, PeerStateStore, PeerStatus


def _stores(
    agent_ids: tuple[int, ...], stale_after: float = 2.5
) -> dict[int, PeerStateStore]:
    return {
        owner_id: PeerStateStore(
            owner_id,
            (peer_id for peer_id in agent_ids if peer_id != owner_id),
            stale_after,
        )
        for owner_id in agent_ids
    }


def _snapshot(agent_id: int, timestamp: float, position=(0.0, 0.0)) -> Heartbeat:
    return Heartbeat(
        agent_id=agent_id,
        position=position,
        status=AgentStatus.IDLE,
        current_task=None,
        timestamp=timestamp,
    )


def test_active_link_delivers_snapshot_to_receiver() -> None:
    graph = CommunicationGraph((1, 2), communication_range=2.0)
    graph.update({1: (0.0, 0.0), 2: (1.0, 0.0)})
    stores = _stores((1, 2))
    transport = PeerStateTransport(graph, stores)

    batch = transport.deliver((_snapshot(1, 0.0),), 0.0)

    observation = stores[2].observation_for(1)
    assert batch.attempted == batch.delivered == 1
    assert batch.undelivered == 0
    assert observation is not None
    assert observation.snapshot.agent_id == 1
    assert stores[2].state_for(1) is PeerKnowledgeState.FRESH


def test_missing_link_does_not_populate_peer_state() -> None:
    graph = CommunicationGraph((1, 2), communication_range=1.0)
    graph.update({1: (0.0, 0.0), 2: (10.0, 0.0)})
    stores = _stores((1, 2))
    transport = PeerStateTransport(graph, stores)
    source = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    snapshot = source.send_heartbeat(0.0)
    assert snapshot is not None

    batch = transport.deliver((snapshot,), 0.0)
    source.move_toward((5.0, 0.0), 1.0)

    assert batch.attempted == batch.undelivered == 1
    assert batch.delivered == 0
    assert stores[2].observation_for(1) is None
    assert stores[2].state_for(1) is PeerKnowledgeState.UNKNOWN
    assert source.position == (1.0, 0.0)


def test_delivered_snapshot_is_immutable_and_not_a_live_agent_view() -> None:
    graph = CommunicationGraph((1, 2), communication_range=10.0)
    graph.update({1: (0.0, 0.0), 2: (1.0, 0.0)})
    stores = _stores((1, 2))
    transport = PeerStateTransport(graph, stores)
    source = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    original = source.send_heartbeat(0.0)
    assert original is not None

    transport.deliver((original,), 0.0)
    source.assign_task(7)
    source.move_toward((5.0, 0.0), 1.0)

    observation = stores[2].observation_for(1)
    assert observation is not None
    assert observation.snapshot.position == (0.0, 0.0)
    assert observation.snapshot.current_task is None
    assert source.position == (1.0, 0.0)
    assert source.current_task == 7


def test_receivers_keep_independent_versions_of_the_same_peer() -> None:
    positions = {1: (0.0, 0.0), 2: (1.0, 0.0), 3: (0.0, 1.0)}
    graph = CommunicationGraph((1, 2, 3), communication_range=10.0)
    stores = _stores((1, 2, 3))
    transport = PeerStateTransport(graph, stores)

    graph.update(positions, blocked_agent_ids={3})
    transport.deliver((_snapshot(2, 0.0, (1.0, 0.0)),), 0.0)
    graph.update(positions, blocked_agent_ids={1})
    transport.deliver((_snapshot(2, 1.0, (2.0, 0.0)),), 1.0)

    first_view = stores[1].observation_for(2)
    third_view = stores[3].observation_for(2)
    assert first_view is not None
    assert third_view is not None
    assert first_view.snapshot.timestamp == 0.0
    assert first_view.snapshot.position == (1.0, 0.0)
    assert third_view.snapshot.timestamp == 1.0
    assert third_view.snapshot.position == (2.0, 0.0)


def test_peer_freshness_uses_strict_timeout_and_refreshes() -> None:
    store = PeerStateStore(1, (2,), stale_after=2.5)
    assert store.state_for(2) is PeerKnowledgeState.UNKNOWN

    assert store.receive(_snapshot(2, 0.0), 0.0) is False
    assert store.advance_time(2.5) == ()
    assert store.state_for(2) is PeerKnowledgeState.FRESH

    assert store.advance_time(2.5001) == (2,)
    assert store.state_for(2) is PeerKnowledgeState.STALE
    assert store.receive(_snapshot(2, 3.0), 3.0) is True
    assert store.state_for(2) is PeerKnowledgeState.FRESH


def test_silence_and_insufficient_quorum_never_declare_failure() -> None:
    store = PeerStateStore(1, (2, 3), stale_after=2.5)
    store.receive(_snapshot(2, 0.0), 0.0)

    assert store.status_for(2) is PeerStatus.HEARD
    assert store.advance_time(2.5001) == (2,)
    assert store.status_for(2) is PeerStatus.SILENT
    assert store.declared_failed_peer_ids == frozenset()

    with pytest.raises(ValueError, match="valid quorum"):
        store.apply_failure_declaration(
            2,
            voter_agent_ids=(1,),
            required_votes=2,
        )

    assert store.status_for(2) is PeerStatus.SILENT
    assert store.declared_failed_peer_ids == frozenset()


@pytest.mark.parametrize("value", [0.0, -1.0, True, float("nan"), float("inf")])
def test_stale_threshold_is_validated(value: object) -> None:
    with pytest.raises(ValueError):
        PeerStateStore(1, (2,), value)  # type: ignore[arg-type]
