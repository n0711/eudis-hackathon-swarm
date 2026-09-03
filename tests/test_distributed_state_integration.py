"""Exercise milestone invariants across topology, transport, and local evidence.
These scenarios prove partitions and recovery without authoritative peer state."""

from __future__ import annotations

from eudis_swarm.agent import Agent, AgentStatus, Heartbeat
from eudis_swarm.communication import CommunicationGraph
from eudis_swarm.failure_manager import (
    FailureDeclaration,
    FailureManager,
    FailureVote,
)
from eudis_swarm.messaging import PeerStateTransport
from eudis_swarm.peer_state import PeerKnowledgeState, PeerStateStore, PeerStatus


def _stores(
    agent_ids: tuple[int, ...], stale_after: float = 2.5
) -> dict[int, PeerStateStore]:
    return {
        owner_agent_id: PeerStateStore(
            owner_agent_id,
            (
                peer_agent_id
                for peer_agent_id in agent_ids
                if peer_agent_id != owner_agent_id
            ),
            stale_after,
        )
        for owner_agent_id in agent_ids
    }


def _heartbeat(agent: Agent, timestamp: float) -> Heartbeat:
    heartbeat = agent.send_heartbeat(timestamp)
    assert heartbeat is not None
    return heartbeat


def test_link_cut_is_not_failure_and_reconnect_refreshes_the_same_store() -> None:
    source = Agent(agent_id=1, position=(0.0, 0.0), speed=1.0)
    observer = Agent(agent_id=2, position=(0.0, 0.0), speed=1.0)
    positions = {1: source.position, 2: observer.position}
    graph = CommunicationGraph((1, 2), communication_range=1.0)
    stores = _stores((1, 2))
    observer_store = stores[2]
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)

    graph.update(positions)
    transport.deliver((_heartbeat(source, 0.0),), 0.0)
    assert observer_store.status_for(1) is PeerStatus.HEARD

    # cutting the link tells the observer nothing; only elapsed silence does.
    graph.update(positions, blocked_links={(1, 2)})
    assert observer_store.status_for(1) is PeerStatus.HEARD

    observer_store.advance_time(3.0)
    assert observer_store.state_for(1) is PeerKnowledgeState.STALE
    assert observer_store.status_for(1) is PeerStatus.SILENT
    # the observer now genuinely suspects the peer it can no longer hear...
    proposed = failure_manager.propose_votes(3.0, participating_agent_ids=(2,))
    assert [vote.suspected_agent_id for vote in proposed] == [1]

    # ...but a lone observer is never a quorum, so nothing is declared.
    assert (
        failure_manager.detect_declarations(
            3.0,
            participating_agent_ids=(2,),
        )
        == ()
    )
    assert failure_manager.declared_agent_ids == frozenset()
    assert observer_store.declared_failed_peer_ids == frozenset()
    assert source.status is AgentStatus.IDLE

    graph.update(positions)
    assert stores[2] is observer_store
    assert observer_store.status_for(1) is PeerStatus.SILENT

    refreshed = transport.deliver(
        (_heartbeat(source, 4.0),),
        4.0,
        receiving_agent_ids=(2,),
    )

    assert refreshed.attempted == refreshed.delivered == 1
    assert refreshed.undelivered == 0
    assert observer_store.state_for(1) is PeerKnowledgeState.FRESH
    assert observer_store.status_for(1) is PeerStatus.HEARD
    observation = observer_store.observation_for(1)
    assert observation is not None
    assert observation.snapshot.timestamp == 4.0
    assert (
        failure_manager.detect_declarations(
            4.0,
            participating_agent_ids=(2,),
        )
        == ()
    )


def test_one_connected_observer_cannot_turn_silence_into_consensus() -> None:
    agent_ids = (1, 2)
    agents = {
        agent_id: Agent(agent_id, position=(0.0, 0.0), speed=1.0)
        for agent_id in agent_ids
    }
    positions = {agent_id: agent.position for agent_id, agent in agents.items()}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)

    graph.update(positions)
    transport.deliver(
        tuple(_heartbeat(agents[agent_id], 0.0) for agent_id in agent_ids),
        0.0,
    )

    graph.update(positions)
    votes = failure_manager.propose_votes(3.0, participating_agent_ids=(1,))

    assert len(votes) == 1
    assert votes[0].suspected_agent_id == 2
    assert failure_manager.required_votes == 2
    assert failure_manager.detect_declarations(3.0, participating_agent_ids=(1,)) == ()
    assert stores[1].status_for(2) is PeerStatus.SILENT
    assert stores[1].declared_failed_peer_ids == frozenset()


def test_reconnection_without_a_heartbeat_does_not_clear_suspicion() -> None:
    agent_ids = (1, 2, 3)
    agents = {
        agent_id: Agent(agent_id, position=(0.0, 0.0), speed=1.0)
        for agent_id in agent_ids
    }
    positions = {agent_id: agent.position for agent_id, agent in agents.items()}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)

    graph.update(positions)
    transport.deliver(
        tuple(_heartbeat(agents[agent_id], 0.0) for agent_id in agent_ids),
        0.0,
    )

    # the healthy target is unreachable long enough for its old snapshot to age.
    graph.update(positions, blocked_links={(1, 2), (2, 3)})
    for store in stores.values():
        store.advance_time(4.1)
    assert stores[1].status_for(2) is PeerStatus.SILENT
    assert stores[3].status_for(2) is PeerStatus.SILENT

    # restoration happens between heartbeats.  The link is back, but neither
    # observer has heard from UAV 2, and no local fact reveals the difference.
    graph.update(positions)
    transport.deliver(
        (_heartbeat(agents[1], 4.1), _heartbeat(agents[3], 4.1)),
        4.1,
        receiving_agent_ids=(1, 3),
    )
    assert stores[1].status_for(2) is PeerStatus.SILENT
    assert stores[3].status_for(2) is PeerStatus.SILENT

    proposed = failure_manager.propose_votes(4.25, participating_agent_ids=(1, 3))
    assert {vote.suspected_agent_id for vote in proposed} == {2}
    assert {vote.voter_agent_id for vote in proposed} == {1, 3}

    # suspicion is still not consensus: no vote was delivered, so nobody declares.
    assert (
        failure_manager.detect_declarations(
            4.25,
            participating_agent_ids=(1, 3),
        )
        == ()
    )

    transport.deliver(
        (_heartbeat(agents[2], 5.0),),
        5.0,
        receiving_agent_ids=(1, 3),
    )
    assert stores[1].status_for(2) is PeerStatus.HEARD
    assert stores[3].status_for(2) is PeerStatus.HEARD
    assert failure_manager.declared_agent_ids == frozenset()


def test_fixed_two_by_two_partition_routes_only_within_components() -> None:
    agent_ids = (1, 2, 3, 4)
    agents = {
        agent_id: Agent(agent_id, position=(0.0, 0.0), speed=1.0)
        for agent_id in agent_ids
    }
    positions = {agent_id: agent.position for agent_id, agent in agents.items()}
    cross_component_links = {(1, 3), (1, 4), (2, 3), (2, 4)}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = PeerStateTransport(graph, stores)

    # all positions stay fixed, so explicit link policy is the only partition cause.
    graph.update(positions, blocked_links=cross_component_links)
    batch = transport.deliver(
        tuple(_heartbeat(agents[agent_id], 0.0) for agent_id in agent_ids),
        0.0,
    )

    assert graph.connected_components == (frozenset({1, 2}), frozenset({3, 4}))
    assert batch.attempted == 12
    assert batch.delivered == 4
    assert batch.undelivered == 8
    expected_observed_peers = {1: {2}, 2: {1}, 3: {4}, 4: {3}}
    for observer_agent_id, expected_peers in expected_observed_peers.items():
        store = stores[observer_agent_id]
        observed_peers = {
            peer_agent_id
            for peer_agent_id in store.peer_agent_ids
            if store.observation_for(peer_agent_id) is not None
        }
        assert observed_peers == expected_peers
        for peer_agent_id in expected_peers:
            assert store.status_for(peer_agent_id) is PeerStatus.HEARD
        for peer_agent_id in set(agent_ids) - expected_peers - {observer_agent_id}:
            assert store.status_for(peer_agent_id) is PeerStatus.SILENT


def test_graph_delivered_failure_votes_form_a_quorum() -> None:
    agent_ids = (1, 2, 3, 4)
    agents = {
        agent_id: Agent(agent_id, position=(0.0, 0.0), speed=1.0)
        for agent_id in agent_ids
    }
    positions = {agent_id: agent.position for agent_id, agent in agents.items()}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)

    graph.update(positions)
    transport.deliver(
        tuple(_heartbeat(agents[agent_id], 0.0) for agent_id in agent_ids),
        0.0,
    )
    # stop the target locally; detector replicas receive no authoritative failure flag.
    assert agents[4].inject_failure(1.0) is True

    graph.update(positions)
    survivors = (1, 2, 3)
    transport.deliver(
        tuple(_heartbeat(agents[agent_id], 3.0) for agent_id in survivors),
        3.0,
        receiving_agent_ids=survivors,
    )
    votes = failure_manager.propose_votes(
        3.0,
        participating_agent_ids=survivors,
    )

    assert [(vote.voter_agent_id, vote.suspected_agent_id) for vote in votes] == [
        (1, 4),
        (2, 4),
        (3, 4),
    ]
    vote_delivery = transport.deliver_failure_votes(
        votes,
        failure_manager,
        3.0,
        receiving_agent_ids=survivors,
    )
    assert vote_delivery.delivered == 6
    assert vote_delivery.attempted == (
        vote_delivery.delivered + vote_delivery.undelivered
    )
    assert vote_delivery.duplicates_suppressed > 0

    declarations = failure_manager.detect_declarations(
        3.0,
        participating_agent_ids=survivors,
    )
    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration.agent_id == 4
    assert declaration.declarer_agent_id == 1
    assert declaration.voter_agent_ids == survivors
    assert declaration.required_votes == 2

    outgoing_declarations = failure_manager.declarations_for_broadcast(
        participating_agent_ids=survivors
    )
    assert [item.declarer_agent_id for item in outgoing_declarations] == [1, 2, 3]
    declaration_delivery = transport.deliver_failure_declarations(
        outgoing_declarations,
        3.0,
        receiving_agent_ids=survivors,
    )
    assert declaration_delivery.delivered == 6
    assert declaration_delivery.attempted == (
        declaration_delivery.delivered + declaration_delivery.undelivered
    )
    assert declaration_delivery.duplicates_suppressed > 0
    assert failure_manager.declared_agent_ids == frozenset({4})
    for survivor_agent_id in survivors:
        assert stores[survivor_agent_id].status_for(4) is PeerStatus.DECLARED_FAILED


def test_declaration_certificates_retry_after_partition_reconnects() -> None:
    agent_ids = (1, 2, 3, 4, 5)
    agents = {
        agent_id: Agent(agent_id, position=(0.0, 0.0), speed=1.0)
        for agent_id in agent_ids
    }
    positions = {agent_id: agent.position for agent_id, agent in agents.items()}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)

    graph.update(positions)
    transport.deliver(
        tuple(_heartbeat(agents[agent_id], 0.0) for agent_id in agent_ids),
        0.0,
    )

    # observer four is isolated while three peers corroborate target five's silence.
    isolated_observer_links = {(1, 4), (2, 4), (3, 4), (4, 5)}
    graph.update(positions, blocked_links=isolated_observer_links)
    survivors = (1, 2, 3, 4)
    transport.deliver(
        tuple(_heartbeat(agents[agent_id], 3.0) for agent_id in survivors),
        3.0,
        receiving_agent_ids=survivors,
    )
    votes = failure_manager.propose_votes(
        3.0,
        participating_agent_ids=survivors,
    )
    transport.deliver_failure_votes(
        votes,
        failure_manager,
        3.0,
        receiving_agent_ids=survivors,
    )
    detected = failure_manager.detect_declarations(
        3.0,
        participating_agent_ids=survivors,
    )
    # UAV 1 declares both the silent target and the isolated observer: from
    # inside the majority the two silences are indistinguishable.
    assert sorted((item.declarer_agent_id, item.agent_id) for item in detected) == [
        (1, 4),
        (1, 5),
    ]

    certificates = failure_manager.declarations_for_broadcast(
        participating_agent_ids=survivors
    )
    # each of the three connected UAVs certifies both silences.
    assert sorted((item.declarer_agent_id, item.agent_id) for item in certificates) == [
        (1, 4),
        (1, 5),
        (2, 4),
        (2, 5),
        (3, 4),
        (3, 5),
    ]
    transport.deliver_failure_declarations(
        certificates,
        3.0,
        receiving_agent_ids=survivors,
    )
    assert stores[4].status_for(5) is PeerStatus.SILENT

    # reconnecting does not reset stores; persistent certificates retry in place.
    graph.update(positions)
    retry_certificates = failure_manager.declarations_for_broadcast(
        participating_agent_ids=survivors
    )
    transport.deliver_failure_declarations(
        retry_certificates,
        3.25,
        receiving_agent_ids=survivors,
    )
    assert stores[4].status_for(5) is PeerStatus.DECLARED_FAILED


def test_protocol_transport_treats_source_times_as_clock_skewed_metadata() -> None:
    agent_ids = (1, 2, 3)
    positions = {agent_id: (0.0, 0.0) for agent_id in agent_ids}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)
    graph.update(positions)

    future_vote = FailureVote(
        voter_agent_id=1,
        suspected_agent_id=3,
        created_at=1.0,
        last_heartbeat=0.0,
        last_heard_at=0.0,
        task_id=None,
    )
    vote_batch = transport.deliver_failure_votes(
        (future_vote,),
        failure_manager,
        0.5,
    )
    assert vote_batch.delivered == 2
    assert all(receipt.origin_agent_id == 1 for receipt in vote_batch.receipts)

    future_declaration = FailureDeclaration(
        agent_id=3,
        declarer_agent_id=1,
        detected_at=1.0,
        last_heartbeat=0.0,
        task_id=None,
        voter_agent_ids=(1, 2),
        required_votes=2,
    )
    declaration_batch = transport.deliver_failure_declarations(
        (future_declaration,), 0.5
    )
    assert declaration_batch.delivered == 2
    assert all(
        receipt.message_id.emitted_at == 1.0 for receipt in declaration_batch.receipts
    )


def test_quorum_without_the_local_declarer_is_ignored_safely() -> None:
    agent_ids = (1, 2, 3, 4)
    agents = {
        agent_id: Agent(agent_id, position=(0.0, 0.0), speed=1.0)
        for agent_id in agent_ids
    }
    positions = {agent_id: agent.position for agent_id, agent in agents.items()}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)

    graph.update(positions)
    transport.deliver(
        tuple(_heartbeat(agents[agent_id], 0.0) for agent_id in agent_ids),
        0.0,
    )
    graph.update(positions)

    # receiver one has two remote votes but contributed no local evidence itself.
    for voter_agent_id in (2, 3):
        failure_manager.record_vote(
            1,
            FailureVote(
                voter_agent_id=voter_agent_id,
                suspected_agent_id=4,
                created_at=3.0,
                last_heartbeat=0.0,
                last_heard_at=0.0,
                task_id=None,
            ),
        )

    assert failure_manager.detect_declarations(3.0, participating_agent_ids=(1,)) == ()
    assert stores[1].status_for(4) is PeerStatus.SILENT


def test_failure_votes_expire_and_unchanged_evidence_is_retried() -> None:
    agent_ids = (1, 2, 3)
    agents = {
        agent_id: Agent(agent_id, position=(0.0, 0.0), speed=1.0)
        for agent_id in agent_ids
    }
    positions = {agent_id: agent.position for agent_id, agent in agents.items()}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = PeerStateTransport(graph, stores)
    failure_manager = FailureManager(2.5, stores)

    graph.update(positions)
    transport.deliver(
        tuple(_heartbeat(agents[agent_id], 0.0) for agent_id in agent_ids),
        0.0,
    )

    # only the two live observers publish at the timeout boundary.
    graph.update(positions)
    transport.deliver(
        (_heartbeat(agents[1], 3.0), _heartbeat(agents[2], 3.0)),
        3.0,
        receiving_agent_ids=(1, 2),
    )
    first_votes = failure_manager.propose_votes(
        3.0,
        participating_agent_ids=(1, 2),
    )
    assert [(vote.voter_agent_id, vote.suspected_agent_id) for vote in first_votes] == [
        (1, 3),
        (2, 3),
    ]

    # deliver one cross-vote, then let every vote in that local quorum expire.
    transport.deliver_failure_votes(
        (first_votes[1],),
        failure_manager,
        3.0,
        receiving_agent_ids=(1,),
    )
    assert failure_manager.detect_declarations(5.6, participating_agent_ids=(1,)) == ()

    # the same heartbeat evidence is retransmitted after the earlier delivery loss.
    transport.deliver(
        (_heartbeat(agents[1], 5.7), _heartbeat(agents[2], 5.7)),
        5.7,
        receiving_agent_ids=(1, 2),
    )
    retry_votes = failure_manager.propose_votes(
        5.7,
        participating_agent_ids=(1, 2),
    )
    assert [(vote.voter_agent_id, vote.suspected_agent_id) for vote in retry_votes] == [
        (1, 3),
        (2, 3),
    ]

    transport.deliver_failure_votes(
        retry_votes,
        failure_manager,
        5.7,
        receiving_agent_ids=(1, 2),
    )
    declarations = failure_manager.detect_declarations(
        5.7,
        participating_agent_ids=(1, 2),
    )
    assert len(declarations) == 1
    assert declarations[0].agent_id == 3
