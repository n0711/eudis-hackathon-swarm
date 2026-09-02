"""Exercise task-claim delivery across deterministic communication topology.

The tests prove partitioned receiver beliefs converge without authoritative shortcuts.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from eudis_swarm.agent import Agent
from eudis_swarm.communication import CommunicationGraph
from eudis_swarm.messaging import (
    TaskClaimTransport,
    TaskProtocolMessageKind,
)
from eudis_swarm.task import Task, TaskOwnershipState, TaskStatus
from eudis_swarm.task_claims import (
    ClaimFreshness,
    TaskClaim,
    TaskClaimStore,
)

TASK_ID = 7
FRESHNESS_TIMEOUT = 2.0
LEASE_TIMEOUT = 5.0
FOUR_AGENTS = (1, 2, 3, 4)
CROSS_COMPONENT_LINKS = frozenset({(1, 3), (1, 4), (2, 3), (2, 4)})


def _stores(
    agent_ids: tuple[int, ...],
    *,
    task_ids: tuple[int, ...] = (TASK_ID,),
    freshness_timeout: float = FRESHNESS_TIMEOUT,
    lease_timeout: float = LEASE_TIMEOUT,
) -> dict[int, TaskClaimStore]:
    return {
        owner_agent_id: TaskClaimStore(
            owner_agent_id,
            agent_ids,
            task_ids,
            lease_duration=lease_timeout,
            freshness_timeout=freshness_timeout,
        )
        for owner_agent_id in agent_ids
    }


def _network(
    agent_ids: tuple[int, ...] = FOUR_AGENTS,
    *,
    blocked_links: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[
    dict[int, tuple[float, float]],
    CommunicationGraph,
    dict[int, TaskClaimStore],
    TaskClaimTransport,
]:
    positions = {agent_id: (0.0, 0.0) for agent_id in agent_ids}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    graph.update(positions, blocked_links=blocked_links)
    stores = _stores(agent_ids)
    return positions, graph, stores, TaskClaimTransport(graph, stores)


def _partitioned_split_brain() -> tuple[
    dict[int, tuple[float, float]],
    CommunicationGraph,
    dict[int, TaskClaimStore],
    TaskClaimTransport,
    TaskClaim,
    TaskClaim,
]:
    positions, graph, stores, transport = _network(blocked_links=CROSS_COMPONENT_LINKS)
    left_claim = stores[1].create_claim(TASK_ID, 0.0)
    right_claim = stores[3].create_claim(TASK_ID, 0.0)
    transport.deliver_claims((left_claim, right_claim), 0.0)
    return positions, graph, stores, transport, left_claim, right_claim


def _reconnected_contest() -> tuple[
    dict[int, tuple[float, float]],
    CommunicationGraph,
    dict[int, TaskClaimStore],
    TaskClaimTransport,
    TaskClaim,
    TaskClaim,
]:
    positions, graph, stores, transport, left_claim, right_claim = (
        _partitioned_split_brain()
    )
    graph.update(positions)
    transport.deliver_claims((left_claim, right_claim), 1.0)
    return positions, graph, stores, transport, left_claim, right_claim


def test_connected_claim_propagates_only_through_graph_delivery() -> None:
    _, _, stores, transport = _network()
    claim = stores[1].create_claim(TASK_ID, 0.0)

    assert stores[1].owns_task(TASK_ID, 0.0) is True
    assert all(
        stores[agent_id].view(TASK_ID, 0.0).state is TaskOwnershipState.UNCLAIMED
        for agent_id in (2, 3, 4)
    )

    batch = transport.deliver_claims((claim,), 0.0)

    assert (batch.attempted, batch.delivered, batch.undelivered) == (3, 3, 0)
    assert [receipt.receiver_agent_id for receipt in batch.receipts] == [2, 3, 4]
    assert all(receipt.source_agent_id == 1 for receipt in batch.receipts)
    assert all(receipt.task_id == TASK_ID for receipt in batch.receipts)
    assert all(receipt.claim_id == claim.claim_id for receipt in batch.receipts)
    assert all(
        receipt.kind is TaskProtocolMessageKind.CLAIM for receipt in batch.receipts
    )
    assert all(receipt.changed for receipt in batch.receipts)
    for agent_id in (2, 3, 4):
        view = stores[agent_id].view(TASK_ID, 0.0)
        assert view.state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
        assert view.known_owner_agent_id == 1
        assert view.claim_freshness is ClaimFreshness.FRESH


def test_cut_claim_delivery_ages_fresh_evidence_to_stale_without_freeing_task() -> None:
    positions, graph, stores, transport = _network((1, 2))
    claim = stores[1].create_claim(TASK_ID, 0.0)
    initial = transport.deliver_claims((claim,), 0.0)
    assert initial.receipts[0].receiver_agent_id == 2

    graph.update(positions, blocked_links={(1, 2)})
    dropped = transport.deliver_claims((claim,), 1.0)
    assert (dropped.attempted, dropped.delivered, dropped.undelivered) == (1, 0, 1)
    assert dropped.receipts == ()

    assert stores[2].advance_time(FRESHNESS_TIMEOUT) == ()
    at_boundary = stores[2].view(TASK_ID, FRESHNESS_TIMEOUT)
    assert at_boundary.state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    assert at_boundary.claim_freshness is ClaimFreshness.FRESH

    assert stores[2].advance_time(FRESHNESS_TIMEOUT + 0.0001) == (TASK_ID,)
    stale = stores[2].view(TASK_ID, FRESHNESS_TIMEOUT + 0.0001)
    assert stale.state is TaskOwnershipState.CLAIMED_BY_PEER_STALE
    assert stale.claim_freshness is ClaimFreshness.STALE
    assert stale.claim_id == claim.claim_id
    assert stale.known_claim_observations[0].current_for_owner is True
    assert stores[2].can_create_claim(TASK_ID, LEASE_TIMEOUT) is False


def test_balanced_partition_delivers_within_components_and_drops_cross_cut() -> None:
    _, graph, stores, transport = _network(blocked_links=CROSS_COMPONENT_LINKS)
    left_claim = stores[1].create_claim(TASK_ID, 0.0)
    right_claim = stores[3].create_claim(TASK_ID, 0.0)

    batch = transport.deliver_claims((left_claim, right_claim), 0.0)

    assert graph.connected_components == (frozenset({1, 2}), frozenset({3, 4}))
    assert (batch.attempted, batch.delivered, batch.undelivered) == (6, 2, 4)
    assert [
        (receipt.source_agent_id, receipt.receiver_agent_id)
        for receipt in batch.receipts
    ] == [(1, 2), (3, 4)]
    assert stores[2].view(TASK_ID, 0.0).known_owner_agent_id == 1
    assert stores[4].view(TASK_ID, 0.0).known_owner_agent_id == 3
    known_owners = {
        agent_id: {
            observation.claim.owner_agent_id
            for observation in store.view(TASK_ID, 0.0).known_claim_observations
        }
        for agent_id, store in stores.items()
    }
    assert known_owners == {1: {1}, 2: {1}, 3: {3}, 4: {3}}


def test_partition_permits_legitimate_receiver_local_split_brain() -> None:
    _, _, stores, _, left_claim, right_claim = _partitioned_split_brain()

    assert left_claim.task_id == right_claim.task_id == TASK_ID
    assert left_claim.owner_agent_id == 1
    assert right_claim.owner_agent_id == 3
    assert stores[1].owns_task(TASK_ID, 0.0) is True
    assert stores[3].owns_task(TASK_ID, 0.0) is True
    assert stores[2].view(TASK_ID, 0.0).known_owner_agent_id == 1
    assert stores[4].view(TASK_ID, 0.0).known_owner_agent_id == 3
    assert all(
        store.view(TASK_ID, 0.0).state is not TaskOwnershipState.CONTESTED
        for store in stores.values()
    )


def test_reconnect_makes_all_replicas_contested_before_reconciliation() -> None:
    positions, graph, stores, transport, left_claim, right_claim = (
        _partitioned_split_brain()
    )

    graph.update(positions)
    batch = transport.deliver_claims((left_claim, right_claim), 1.0)

    assert graph.is_fully_connected is True
    assert (batch.attempted, batch.delivered, batch.undelivered) == (6, 6, 0)
    assert sum(receipt.changed for receipt in batch.receipts) == 4
    for store in stores.values():
        view = store.view(TASK_ID, 1.0)
        assert view.state is TaskOwnershipState.CONTESTED
        assert view.contested is True
        assert view.known_owner_agent_id is None
        assert view.reconciliation_winner_agent_id is None


def test_opposite_transport_arrival_orders_reconcile_to_the_same_winner() -> None:
    _, _, first_stores, first_transport = _network()
    first_left = first_stores[1].create_claim(TASK_ID, 0.0)
    first_right = first_stores[3].create_claim(TASK_ID, 0.0)
    first_sources = [
        first_transport.deliver_claims(
            (claim,),
            0.0,
            receiving_agent_ids=(2,),
        )
        .receipts[0]
        .source_agent_id
        for claim in (first_right, first_left)
    ]

    _, _, second_stores, second_transport = _network()
    second_left = second_stores[1].create_claim(TASK_ID, 0.0)
    second_right = second_stores[3].create_claim(TASK_ID, 0.0)
    second_sources = [
        second_transport.deliver_claims(
            (claim,),
            0.0,
            receiving_agent_ids=(4,),
        )
        .receipts[0]
        .source_agent_id
        for claim in (second_left, second_right)
    ]

    assert first_sources == [3, 1]
    assert second_sources == [1, 3]
    assert first_stores[2].view(TASK_ID, 0.0).state is TaskOwnershipState.CONTESTED
    assert second_stores[4].view(TASK_ID, 0.0).state is TaskOwnershipState.CONTESTED

    first_decision = first_stores[2].reconcile(TASK_ID, 1.0)
    second_decision = second_stores[4].reconcile(TASK_ID, 1.0)
    assert first_decision is not None
    assert second_decision is not None
    assert first_decision.winner.claim_id == second_decision.winner.claim_id
    assert first_decision.winner.owner_agent_id == 1
    assert first_stores[2].view(TASK_ID, 1.0).known_owner_agent_id == 1
    assert second_stores[4].view(TASK_ID, 1.0).known_owner_agent_id == 1


def test_loser_release_is_graph_gated_and_converges_after_retry() -> None:
    positions, graph, stores, transport, _, _ = _reconnected_contest()
    losing_decision = stores[3].reconcile(TASK_ID, 2.0)
    assert losing_decision is not None
    release = losing_decision.local_release
    assert release is not None
    assert release.releasing_agent_id == 3
    assert release.winning_claim is not None
    assert release.winning_claim.owner_agent_id == 1
    assert stores[3].owns_task(TASK_ID, 2.0) is False

    graph.update(positions, blocked_links={(3, 4)})
    partial = transport.deliver_releases((release,), 2.0)
    assert (partial.attempted, partial.delivered, partial.undelivered) == (3, 2, 1)
    assert [receipt.receiver_agent_id for receipt in partial.receipts] == [1, 2]
    assert all(
        receipt.kind is TaskProtocolMessageKind.RELEASE for receipt in partial.receipts
    )
    assert stores[4].view(TASK_ID, 2.0).state is TaskOwnershipState.CONTESTED

    graph.update(positions)
    retry = transport.deliver_releases((release,), 3.0)
    assert (retry.attempted, retry.delivered, retry.undelivered) == (3, 3, 0)
    assert [(item.receiver_agent_id, item.changed) for item in retry.receipts] == [
        (1, False),
        (2, False),
        (4, True),
    ]
    assert sum(store.owns_task(TASK_ID, 3.0) for store in stores.values()) == 1
    assert stores[1].owns_task(TASK_ID, 3.0) is True
    assert all(
        store.view(TASK_ID, 3.0).known_owner_agent_id == 1 for store in stores.values()
    )


def test_older_and_duplicate_claims_cannot_resurrect_released_ownership() -> None:
    _, _, stores, transport = _network()
    winner = stores[1].create_claim(TASK_ID, 0.0)
    old_loser = stores[3].create_claim(TASK_ID, 0.0)
    current_loser = stores[3].renew_claim(TASK_ID, 0.0)
    transport.deliver_claims((winner, current_loser), 0.0)
    losing_decision = stores[3].reconcile(TASK_ID, 1.0)
    assert losing_decision is not None and losing_decision.local_release is not None
    release = losing_decision.local_release
    transport.deliver_releases((release,), 1.0)

    delayed = transport.deliver_claims((old_loser, current_loser), 2.0)
    duplicate_release = transport.deliver_releases((release,), 2.0)

    assert (delayed.attempted, delayed.delivered, delayed.undelivered) == (6, 6, 0)
    assert all(receipt.changed is False for receipt in delayed.receipts)
    assert all(receipt.changed is False for receipt in duplicate_release.receipts)
    assert stores[3].owns_task(TASK_ID, 2.0) is False
    assert all(
        store.view(TASK_ID, 2.0).known_owner_agent_id == 1 for store in stores.values()
    )


def test_delivered_completion_remains_absorbing_after_conflicting_claims() -> None:
    positions, graph, stores, transport = _network(blocked_links=CROSS_COMPONENT_LINKS)
    left_claim = stores[1].create_claim(TASK_ID, 0.0)
    right_old = stores[3].create_claim(TASK_ID, 0.0)
    transport.deliver_claims((left_claim, right_old), 0.0)

    completion = stores[1].create_completion(TASK_ID, 1.0)
    partition_delivery = transport.deliver_completions((completion,), 1.0)
    assert (
        partition_delivery.attempted,
        partition_delivery.delivered,
        partition_delivery.undelivered,
    ) == (3, 1, 2)
    right_new = stores[3].renew_claim(TASK_ID, 1.0)
    transport.deliver_claims((right_new,), 1.0)

    graph.update(positions)
    completion_delivery = transport.deliver_completions((completion,), 2.0)
    assert (
        completion_delivery.attempted,
        completion_delivery.delivered,
        completion_delivery.undelivered,
    ) == (3, 3, 0)
    assert [receipt.changed for receipt in completion_delivery.receipts] == [
        False,
        True,
        True,
    ]
    assert all(
        receipt.kind is TaskProtocolMessageKind.COMPLETION
        for receipt in completion_delivery.receipts
    )

    transport.deliver_claims((right_new,), 2.0)
    transport.deliver_claims((right_old,), 3.0)
    transport.deliver_completions((completion,), 3.0)
    for store in stores.values():
        view = store.view(TASK_ID, 20.0)
        assert view.state is TaskOwnershipState.COMPLETE
        assert view.complete is True
        assert store.can_create_claim(TASK_ID, 20.0) is False


def test_transport_rejects_future_sources_receivers_and_uninitialized_graph() -> None:
    agent_ids = (1, 2)
    positions = {agent_id: (0.0, 0.0) for agent_id in agent_ids}
    graph = CommunicationGraph(agent_ids, communication_range=1.0)
    stores = _stores(agent_ids)
    transport = TaskClaimTransport(graph, stores)
    claim = stores[1].create_claim(TASK_ID, 0.0)

    with pytest.raises(RuntimeError, match="graph must be initialized"):
        transport.deliver_claims((claim,), 0.0)

    graph.update(positions)
    with pytest.raises(ValueError, match="unknown receiver"):
        transport.deliver_claims((claim,), 0.0, receiving_agent_ids=(99,))
    with pytest.raises(ValueError, match="unknown task-message source"):
        transport.deliver_claims(
            (
                TaskClaim(
                    task_id=TASK_ID,
                    owner_agent_id=99,
                    epoch=1,
                    created_at=0.0,
                    freshness_timeout=FRESHNESS_TIMEOUT,
                    lease_timeout=LEASE_TIMEOUT,
                ),
            ),
            0.0,
        )
    with pytest.raises(ValueError, match="creation timestamp cannot follow"):
        transport.deliver_claims(
            (
                TaskClaim(
                    task_id=TASK_ID,
                    owner_agent_id=1,
                    epoch=2,
                    created_at=1.0,
                    freshness_timeout=FRESHNESS_TIMEOUT,
                    lease_timeout=LEASE_TIMEOUT,
                ),
            ),
            0.5,
        )


def test_transport_constructor_rejects_mismatched_receiver_stores() -> None:
    graph = CommunicationGraph((1, 2), communication_range=1.0)
    graph.update({1: (0.0, 0.0), 2: (0.0, 0.0)})
    matching = _stores((1, 2))

    with pytest.raises(ValueError, match="must match communication graph"):
        TaskClaimTransport(graph, {1: matching[1]})

    wrong_owner: Mapping[int, TaskClaimStore] = {
        1: matching[2],
        2: matching[1],
    }
    with pytest.raises(ValueError, match="key must match its owner"):
        TaskClaimTransport(graph, wrong_owner)

    mismatched_tasks = {
        1: TaskClaimStore(1, (1, 2), (7,), 5.0, freshness_timeout=2.0),
        2: TaskClaimStore(2, (1, 2), (8,), 5.0, freshness_timeout=2.0),
    }
    with pytest.raises(ValueError, match="same task IDs"):
        TaskClaimTransport(graph, mismatched_tasks)

    mismatched_policy = {
        1: TaskClaimStore(1, (1, 2), (7,), 5.0, freshness_timeout=2.0),
        2: TaskClaimStore(2, (1, 2), (7,), 6.0, freshness_timeout=2.0),
    }
    with pytest.raises(ValueError, match="one lease policy"):
        TaskClaimTransport(graph, mismatched_policy)


def test_authoritative_agent_and_task_mutation_cannot_change_local_belief() -> None:
    _, _, stores, transport = _network((1, 2))
    claim = stores[1].create_claim(TASK_ID, 0.0)
    transport.deliver_claims((claim,), 0.0)
    before = stores[2].snapshot(0.0)

    authoritative_agent = Agent(1, position=(100.0, 100.0), speed=10.0)
    authoritative_task = Task(TASK_ID, position=(200.0, 200.0))
    authoritative_agent.assign_task(TASK_ID)
    authoritative_task.assign(authoritative_agent.agent_id)
    authoritative_agent.complete_task(TASK_ID)
    authoritative_task.complete(authoritative_agent.agent_id)

    assert authoritative_agent.current_task is None
    assert authoritative_task.status is TaskStatus.COMPLETED
    assert stores[2].snapshot(0.0) == before
    local_view = stores[2].view(TASK_ID, 0.0)
    assert local_view.state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    assert local_view.complete is False


def test_six_noncontiguous_uavs_receive_one_claim_without_scale_assumptions() -> None:
    agent_ids = (2, 4, 6, 8, 10, 12)
    _, graph, stores, transport = _network(agent_ids)
    claim = stores[12].create_claim(TASK_ID, 0.0)

    batch = transport.deliver_claims((claim,), 0.0)

    assert graph.is_fully_connected is True
    assert (batch.attempted, batch.delivered, batch.undelivered) == (5, 5, 0)
    assert [receipt.receiver_agent_id for receipt in batch.receipts] == [
        2,
        4,
        6,
        8,
        10,
    ]
    assert stores[12].owns_task(TASK_ID, 0.0) is True
    assert all(
        stores[agent_id].view(TASK_ID, 0.0).known_owner_agent_id == 12
        for agent_id in agent_ids
        if agent_id != 12
    )
