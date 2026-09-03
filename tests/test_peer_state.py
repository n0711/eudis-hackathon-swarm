"""Verify receiver-local peer evidence transitions and delivery boundaries.
The tests keep freshness, reachability, silence, and declared failure distinct."""

from __future__ import annotations

import pytest

from eudis_swarm.agent import Agent, AgentStatus, Heartbeat
from eudis_swarm.communication import CommunicationGraph, CommunicationState
from eudis_swarm.failure_manager import FailureManager
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


def test_partition_exposes_no_graph_oracle_to_peer_state_or_failure_logic() -> None:
    agent_ids = (1, 2, 3)
    positions = {agent_id: (0.0, 0.0) for agent_id in agent_ids}
    source = Agent(agent_id=1, position=positions[1], speed=1.0)
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    receiver_store = stores[2]
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)

    graph.update(positions)
    initial = source.send_heartbeat(0.0)
    assert initial is not None
    transport.deliver((initial,), 0.0, receiving_agent_ids=(2,))
    assert receiver_store.status_for(1) is PeerStatus.HEARD

    # The world knows UAV 1 is isolated.  A failed packet provides no matching
    # oracle fact to UAV 2, so its last delivered evidence remains HEARD.
    graph.update(positions, blocked_links={(1, 2), (1, 3)})
    assert graph.communication_state(1) is CommunicationState.UNREACHABLE
    undelivered = source.send_heartbeat(1.0)
    assert undelivered is not None
    transport.deliver((undelivered,), 1.0, receiving_agent_ids=(2,))
    assert receiver_store.status_for(1) is PeerStatus.HEARD
    retained = receiver_store.observation_for(1)
    assert retained is not None
    assert retained.snapshot == initial

    # Only receiver-local elapsed time changes the belief to SILENT.  Failure
    # logic may create a suspicion vote, but one local timeout is not failure.
    assert receiver_store.advance_time(2.5001) == (1,)
    assert receiver_store.status_for(1) is PeerStatus.SILENT
    votes = failure_manager.propose_votes(
        2.5001,
        participating_agent_ids=(2,),
    )
    assert [(vote.voter_agent_id, vote.suspected_agent_id) for vote in votes] == [
        (2, 1)
    ]
    assert (
        failure_manager.detect_declarations(
            2.5001,
            participating_agent_ids=(2,),
        )
        == ()
    )
    assert receiver_store.status_for(1) is PeerStatus.SILENT
    assert receiver_store.declared_failed_peer_ids == frozenset()

    # Physical reconnection is equally invisible until a packet is received.
    graph.update(positions)
    assert graph.communication_state(1) is CommunicationState.REACHABLE
    assert receiver_store.status_for(1) is PeerStatus.SILENT
    refreshed = source.send_heartbeat(3.0)
    assert refreshed is not None
    transport.deliver((refreshed,), 3.0, receiving_agent_ids=(2,))
    assert receiver_store.status_for(1) is PeerStatus.HEARD
    assert source.responsive is True
    assert source.status is AgentStatus.IDLE


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


def test_source_clock_may_lead_receiver_clock_but_freshness_stays_local() -> None:
    store = PeerStateStore(1, (2,), stale_after=2.5)

    assert store.receive(_snapshot(2, 100.0), received_at=5.0) is False
    observation = store.observation_for(2)
    assert observation is not None
    assert observation.snapshot.timestamp == 100.0
    assert observation.received_at == 5.0

    assert store.advance_time(7.5) == ()
    assert store.state_for(2) is PeerKnowledgeState.FRESH
    assert store.advance_time(7.5001) == (2,)
    assert store.state_for(2) is PeerKnowledgeState.STALE

    with pytest.raises(ValueError, match="receipt timestamp must not move backwards"):
        store.receive(_snapshot(2, 101.0), received_at=7.0)


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
            evidence_last_heartbeat=0.0,
        )

    assert store.status_for(2) is PeerStatus.SILENT
    assert store.declared_failed_peer_ids == frozenset()


def test_a_replayed_certificate_cannot_undo_a_retraction() -> None:
    """First-hand contact outranks a certificate built from older evidence.

    Declaration certificates are retransmitted, so one issued before the link
    healed keeps arriving afterwards. Accepting it would silently re-kill a UAV
    the swarm has already heard from.
    """

    store = PeerStateStore(1, (2, 3), stale_after=2.5)
    quorum = {"voter_agent_ids": (1, 3), "required_votes": 2}
    store.receive(_snapshot(2, 0.0), 0.0)
    assert store.apply_failure_declaration(2, **quorum, evidence_last_heartbeat=0.0)
    assert store.status_for(2) is PeerStatus.DECLARED_FAILED

    # the link heals and UAV 2 is plainly transmitting again
    store.receive(_snapshot(2, 1.0), 1.0)
    assert store.declared_failed_peer_ids == frozenset()
    assert store.retracted_peer_ids == frozenset({2})

    # the old certificate is retransmitted and must be ignored
    assert not store.apply_failure_declaration(2, **quorum, evidence_last_heartbeat=0.0)
    assert store.declared_failed_peer_ids == frozenset()
    assert store.retracted_peer_ids == frozenset({2})

    # a declaration built from the newer silence is still allowed through, and
    # it invalidates the stale retraction witness so it must be earned again.
    assert store.apply_failure_declaration(2, **quorum, evidence_last_heartbeat=1.0)
    assert store.declared_failed_peer_ids == frozenset({2})
    assert store.retracted_peer_ids == frozenset()


@pytest.mark.parametrize("value", [0.0, -1.0, True, float("nan"), float("inf")])
def test_stale_threshold_is_validated(value: object) -> None:
    with pytest.raises(ValueError):
        PeerStateStore(1, (2,), value)  # type: ignore[arg-type]
