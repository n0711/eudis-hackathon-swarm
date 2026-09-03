"""Prove immutable protocol evidence converges over deterministic multi-hop links."""

from __future__ import annotations

from itertools import combinations

from eudis_swarm.agent import AgentStatus, Heartbeat
from eudis_swarm.communication import CommunicationGraph
from eudis_swarm.failure_manager import FailureManager
from eudis_swarm.messaging import PeerStateTransport, TaskClaimTransport
from eudis_swarm.peer_state import PeerStateStore, PeerStatus
from eudis_swarm.task import TaskOwnershipState
from eudis_swarm.task_claims import TaskClaimStore

TASK_ID = 7
FRESHNESS_TIMEOUT = 4.0
LEASE_TIMEOUT = 10.0
FAILURE_TIMEOUT = 2.5


def _canonical_link(left: int, right: int) -> tuple[int, int]:
    return (left, right) if left < right else (right, left)


def _blocked_except(
    agent_ids: tuple[int, ...],
    allowed_links: set[tuple[int, int]],
) -> set[tuple[int, int]]:
    canonical_allowed = {_canonical_link(left, right) for left, right in allowed_links}
    return set(combinations(agent_ids, 2)) - canonical_allowed


def _update_links(
    graph: CommunicationGraph,
    positions: dict[int, tuple[float, float]],
    allowed_links: set[tuple[int, int]],
) -> None:
    graph.update(
        positions,
        blocked_links=_blocked_except(graph.agent_ids, allowed_links),
    )


def _task_network(
    agent_ids: tuple[int, ...],
    allowed_links: set[tuple[int, int]],
) -> tuple[
    dict[int, tuple[float, float]],
    CommunicationGraph,
    dict[int, TaskClaimStore],
    TaskClaimTransport,
]:
    positions = {agent_id: (0.0, 0.0) for agent_id in agent_ids}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    _update_links(graph, positions, allowed_links)
    stores = {
        owner_agent_id: TaskClaimStore(
            owner_agent_id,
            agent_ids,
            (TASK_ID,),
            lease_duration=LEASE_TIMEOUT,
            freshness_timeout=FRESHNESS_TIMEOUT,
        )
        for owner_agent_id in agent_ids
    }
    return positions, graph, stores, TaskClaimTransport(graph, stores)


def _peer_stores(agent_ids: tuple[int, ...]) -> dict[int, PeerStateStore]:
    return {
        owner_agent_id: PeerStateStore(
            owner_agent_id,
            (
                peer_agent_id
                for peer_agent_id in agent_ids
                if peer_agent_id != owner_agent_id
            ),
            stale_after=FAILURE_TIMEOUT,
        )
        for owner_agent_id in agent_ids
    }


def test_claim_crosses_a_three_node_chain_with_origin_preserved() -> None:
    agent_ids = (1, 2, 3)
    _, graph, stores, transport = _task_network(
        agent_ids,
        {(1, 2), (2, 3)},
    )
    claim = stores[1].create_claim(TASK_ID, 0.0)

    batch = transport.deliver_claims((claim,), 0.0)

    assert graph.neighbors(1) == frozenset({2})
    assert stores[3].view(TASK_ID, 0.0).known_owner_agent_id == 1
    final_hop = next(
        receipt for receipt in batch.receipts if receipt.receiver_agent_id == 3
    )
    assert final_hop.origin_agent_id == 1
    assert final_hop.source_agent_id == 1
    assert final_hop.forwarder_agent_id == 2
    assert final_hop.hop_count == 2
    assert batch.forwarded >= 1


def test_completion_crosses_a_three_node_chain_with_origin_preserved() -> None:
    agent_ids = (1, 2, 3)
    _, _, stores, transport = _task_network(
        agent_ids,
        {(1, 2), (2, 3)},
    )
    claim = stores[1].create_claim(TASK_ID, 0.0)
    transport.deliver_claims((claim,), 0.0)
    completion = stores[1].create_completion(TASK_ID, 1.0)

    batch = transport.deliver_completions((completion,), 1.0)

    assert stores[3].view(TASK_ID, 1.0).state is TaskOwnershipState.COMPLETE
    final_hop = next(
        receipt for receipt in batch.receipts if receipt.receiver_agent_id == 3
    )
    assert final_hop.origin_agent_id == 1
    assert final_hop.forwarder_agent_id == 2
    assert final_hop.hop_count == 2


def test_cycle_suppresses_duplicates_and_repeated_delivery_is_quiescent() -> None:
    agent_ids = (1, 2, 3, 4)
    _, _, stores, transport = _task_network(
        agent_ids,
        {(1, 2), (2, 3), (3, 4), (1, 4)},
    )
    claim = stores[1].create_claim(TASK_ID, 0.0)

    first = transport.deliver_claims((claim,), 0.0)

    seen_by_agent = {
        agent_id: transport.seen_message_ids(agent_id) for agent_id in agent_ids
    }
    assert len(seen_by_agent[1]) == 1
    assert all(seen == seen_by_agent[1] for seen in seen_by_agent.values())
    assert sum(receipt.changed for receipt in first.receipts) == 3
    assert first.duplicates_suppressed > 0
    assert first.duplicate_source_publications == 0
    assert first.duplicate_route_suppressions == first.duplicates_suppressed
    assert first.useful_first_deliveries == 3
    assert all(
        len(store.view(TASK_ID, 0.0).known_claim_observations) == 1
        for store in stores.values()
    )

    second = transport.deliver_claims((claim,), 1.0)

    assert (second.attempted, second.delivered, second.undelivered) == (0, 0, 0)
    assert second.receipts == ()
    assert second.duplicate_source_publications == 1
    # One cyclic route remained queued after its receiver learned the message
    # through the other side of the ring; pruning it is not a source duplicate.
    assert second.duplicate_route_suppressions == 1
    assert second.duplicates_suppressed == 2
    assert all(
        transport.seen_message_ids(agent_id) == seen_by_agent[agent_id]
        for agent_id in agent_ids
    )


def test_inactive_receiver_is_uniquely_deferred_without_a_link_attempt() -> None:
    agent_ids = (1, 2, 3)
    _, graph, stores, transport = _task_network(
        agent_ids,
        {(1, 2), (1, 3), (2, 3)},
    )
    claim = stores[1].create_claim(TASK_ID, 0.0)

    first = transport.deliver_claims(
        (claim,),
        0.0,
        receiving_agent_ids=(1, 2),
    )

    assert graph.is_fully_connected is True
    assert first.logical_forwarding_attempts == 1
    assert first.successful_first_deliveries == 1
    assert first.unavailable_link_attempts == 0
    assert first.inactive_endpoint_deferrals == 1
    assert first.useful_first_deliveries == 1
    assert stores[3].view(TASK_ID, 0.0).state is TaskOwnershipState.UNCLAIMED

    unchanged = transport.deliver_claims(
        (claim,),
        1.0,
        receiving_agent_ids=(1, 2),
    )

    assert unchanged.logical_forwarding_attempts == 0
    assert unchanged.inactive_endpoint_deferrals == 0
    assert unchanged.duplicate_source_publications == 1

    rejoined = transport.deliver_claims(
        (),
        2.0,
        receiving_agent_ids=agent_ids,
    )

    assert rejoined.logical_forwarding_attempts == 1
    assert rejoined.successful_first_deliveries == 1
    assert rejoined.unavailable_link_attempts == 0
    assert rejoined.inactive_endpoint_deferrals == 0
    assert rejoined.receipts[0].receiver_agent_id == 3
    assert rejoined.receipts[0].forwarder_agent_id == 1
    assert rejoined.receipts[0].hop_count == 1
    assert stores[3].view(TASK_ID, 2.0).known_owner_agent_id == 1


def test_active_partition_counts_unavailable_links_not_inactive_deferrals() -> None:
    agent_ids = (1, 2, 3)
    _, _, stores, transport = _task_network(agent_ids, {(1, 2)})
    claim = stores[1].create_claim(TASK_ID, 0.0)

    batch = transport.deliver_claims((claim,), 0.0)

    assert batch.logical_forwarding_attempts == 3
    assert batch.successful_first_deliveries == 1
    assert batch.unavailable_link_attempts == 2
    assert batch.inactive_endpoint_deferrals == 0
    assert batch.useful_first_deliveries == 1


def test_first_transport_delivery_can_be_non_useful_domain_evidence() -> None:
    agent_ids = (1, 2)
    _, _, stores, transport = _task_network(agent_ids, {(1, 2)})
    old_claim = stores[1].create_claim(TASK_ID, 0.0)
    new_claim = stores[1].renew_claim(TASK_ID, 0.0)
    transport.deliver_claims((new_claim,), 0.0)

    delayed = transport.deliver_claims((old_claim,), 1.0)

    assert delayed.successful_first_deliveries == 1
    assert delayed.useful_first_deliveries == 0
    assert delayed.receipts[0].changed is False
    assert stores[2].view(TASK_ID, 1.0).claim_id == new_claim.claim_id


def test_pending_claim_crosses_a_new_bridge_after_chain_reconnection() -> None:
    agent_ids = (1, 2, 3, 4)
    positions, graph, stores, transport = _task_network(
        agent_ids,
        {(1, 2), (3, 4)},
    )
    claim = stores[1].create_claim(TASK_ID, 0.0)

    transport.deliver_claims((claim,), 0.0)

    assert stores[2].view(TASK_ID, 0.0).known_owner_agent_id == 1
    assert all(
        stores[agent_id].view(TASK_ID, 0.0).state is TaskOwnershipState.UNCLAIMED
        for agent_id in (3, 4)
    )

    _update_links(graph, positions, {(1, 2), (2, 3), (3, 4)})
    reconnected = transport.deliver_claims((), 1.0)

    assert all(
        store.view(TASK_ID, 1.0).known_owner_agent_id == 1 for store in stores.values()
    )
    bridge_receipt = next(
        receipt for receipt in reconnected.receipts if receipt.receiver_agent_id == 3
    )
    final_receipt = next(
        receipt for receipt in reconnected.receipts if receipt.receiver_agent_id == 4
    )
    assert (bridge_receipt.forwarder_agent_id, bridge_receipt.hop_count) == (2, 2)
    assert (final_receipt.forwarder_agent_id, final_receipt.hop_count) == (3, 3)


def test_split_brain_reconciles_over_a_chain_and_release_converges() -> None:
    agent_ids = (1, 2, 3, 4)
    positions, graph, stores, transport = _task_network(
        agent_ids,
        {(1, 2), (3, 4)},
    )
    left_claim = stores[1].create_claim(TASK_ID, 0.0)
    right_claim = stores[4].create_claim(TASK_ID, 0.0)
    transport.deliver_claims((left_claim, right_claim), 0.0)

    assert stores[1].owns_task(TASK_ID, 0.0) is True
    assert stores[4].owns_task(TASK_ID, 0.0) is True

    _update_links(graph, positions, {(1, 2), (2, 3), (3, 4)})
    transport.deliver_claims((), 1.0)

    assert all(
        store.view(TASK_ID, 1.0).state is TaskOwnershipState.CONTESTED
        for store in stores.values()
    )
    decisions = {
        agent_id: store.reconcile(TASK_ID, 2.0)
        for agent_id, store in sorted(stores.items())
    }
    assert all(decision is not None for decision in decisions.values())
    assert {
        decision.winner.owner_agent_id
        for decision in decisions.values()
        if decision is not None
    } == {1}

    losing_decision = decisions[4]
    assert losing_decision is not None
    release = losing_decision.local_release
    assert release is not None
    release_batch = transport.deliver_releases((release,), 2.0)

    assert all(
        store.view(TASK_ID, 2.0).known_owner_agent_id == 1 for store in stores.values()
    )
    assert sum(store.owns_task(TASK_ID, 2.0) for store in stores.values()) == 1
    final_hop = next(
        receipt for receipt in release_batch.receipts if receipt.receiver_agent_id == 1
    )
    assert final_hop.origin_agent_id == 4
    assert final_hop.forwarder_agent_id == 2
    assert final_hop.hop_count == 3


def test_failure_votes_form_local_quorum_and_declaration_crosses_a_chain() -> None:
    agent_ids = (1, 2, 3, 4, 5)
    participants = (1, 2, 3, 4)
    positions = {agent_id: (0.0, 0.0) for agent_id in agent_ids}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    graph.update(positions)
    stores = _peer_stores(agent_ids)
    transport = PeerStateTransport(graph, stores)
    manager = FailureManager(FAILURE_TIMEOUT, stores)
    target_heartbeat = Heartbeat(
        agent_id=5,
        position=(0.0, 0.0),
        status=AgentStatus.IDLE,
        current_task=None,
        timestamp=0.0,
    )
    transport.deliver((target_heartbeat,), 0.0)

    _update_links(graph, positions, {(1, 2), (2, 3), (3, 4)})
    votes = manager.propose_votes(
        3.0,
        participating_agent_ids=(1, 3, 4),
    )
    assert [(vote.voter_agent_id, vote.suspected_agent_id) for vote in votes] == [
        (1, 5),
        (3, 5),
        (4, 5),
    ]

    vote_batch = transport.deliver_failure_votes(
        votes,
        manager,
        3.0,
        receiving_agent_ids=participants,
    )

    remote_votes_at_declarer = sorted(
        (
            receipt.origin_agent_id,
            receipt.forwarder_agent_id,
            receipt.hop_count,
        )
        for receipt in vote_batch.receipts
        if receipt.receiver_agent_id == 1 and receipt.origin_agent_id != 1
    )
    assert remote_votes_at_declarer == [(3, 2, 2), (4, 2, 3)]
    assert all(
        len(transport.seen_failure_message_ids(agent_id)) == 3
        for agent_id in participants
    )

    declarations = manager.detect_declarations(
        3.0,
        participating_agent_ids=(1,),
    )
    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration.declarer_agent_id == 1
    assert declaration.agent_id == 5
    assert declaration.voter_agent_ids == (1, 3, 4)
    assert declaration.required_votes == 3

    declaration_batch = transport.deliver_failure_declarations(
        (declaration,),
        3.0,
        receiving_agent_ids=participants,
    )

    assert all(
        stores[agent_id].status_for(5) is PeerStatus.DECLARED_FAILED
        for agent_id in participants
    )
    final_hop = next(
        receipt
        for receipt in declaration_batch.receipts
        if receipt.receiver_agent_id == 4
    )
    assert final_hop.origin_agent_id == 1
    assert final_hop.forwarder_agent_id == 3
    assert final_hop.hop_count == 3
