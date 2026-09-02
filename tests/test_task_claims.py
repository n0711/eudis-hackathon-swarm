"""Verify immutable task claims and receiver-local ownership transitions.

The tests lock lease boundaries, epoch safety, reconciliation, release, and completion.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from itertools import permutations
from typing import Callable

import pytest

from eudis_swarm.task import TaskOwnershipState
from eudis_swarm.task_claims import (
    MAX_CLAIM_EPOCH,
    ClaimFreshness,
    TaskClaim,
    TaskClaimRelease,
    TaskClaimStore,
    TaskCompletionEvidence,
    newest_claims_by_owner,
    select_winning_claim,
)

TASK_ID = 11
FRESHNESS_TIMEOUT = 2.0
LEASE_TIMEOUT = 5.0


def _claim(
    owner_agent_id: int,
    *,
    epoch: int = 1,
    task_id: int = TASK_ID,
    created_at: float = 0.0,
) -> TaskClaim:
    return TaskClaim(
        task_id=task_id,
        owner_agent_id=owner_agent_id,
        epoch=epoch,
        created_at=created_at,
        freshness_timeout=FRESHNESS_TIMEOUT,
        lease_timeout=LEASE_TIMEOUT,
    )


def _store(
    owner_agent_id: int = 7,
    *,
    agent_ids: tuple[int, ...] = (2, 7, 9),
    task_ids: tuple[int, ...] = (TASK_ID,),
) -> TaskClaimStore:
    return TaskClaimStore(
        owner_agent_id,
        agent_ids,
        task_ids,
        lease_duration=LEASE_TIMEOUT,
        freshness_timeout=FRESHNESS_TIMEOUT,
    )


def test_claim_protocol_messages_are_frozen_and_structurally_identified() -> None:
    winner = _claim(2)
    loser = _claim(9, epoch=4)
    release = TaskClaimRelease(
        losing_claim=loser,
        winning_claim=winner,
        created_at=1.0,
    )
    completion = TaskCompletionEvidence(claim=winner, created_at=1.0)

    assert winner.claim_id == (TASK_ID, 2, 1)
    assert release.task_id == TASK_ID
    assert release.releasing_agent_id == 9
    assert completion.task_id == TASK_ID
    assert completion.owner_agent_id == 2
    for message in (winner, release, completion):
        with pytest.raises(FrozenInstanceError):
            setattr(message, "created_at", 99.0)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TaskClaim(0, 2, 1, 0.0, 2.0, 5.0),
        lambda: TaskClaim(11, 0, 1, 0.0, 2.0, 5.0),
        lambda: TaskClaim(11, 2, 0, 0.0, 2.0, 5.0),
        lambda: TaskClaim(11, 2, True, 0.0, 2.0, 5.0),
        lambda: TaskClaim(11, 2, MAX_CLAIM_EPOCH + 1, 0.0, 2.0, 5.0),
        lambda: TaskClaim(11, 2, 1, -1.0, 2.0, 5.0),
        lambda: TaskClaim(11, 2, 1, float("nan"), 2.0, 5.0),
        lambda: TaskClaim(11, 2, 1, 0.0, 0.0, 5.0),
        lambda: TaskClaim(11, 2, 1, 0.0, 2.0, 2.0),
        lambda: TaskClaim(11, 2, 1, 0.0, 3.0, 2.0),
    ],
)
def test_claim_validation_rejects_impossible_identity_time_and_policy(
    factory: Callable[[], TaskClaim],
) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TaskClaimRelease(_claim(9), _claim(2, task_id=12), 1.0),
        lambda: TaskClaimRelease(_claim(9), _claim(9, epoch=2), 1.0),
        lambda: TaskClaimRelease(_claim(2), _claim(9), 1.0),
        lambda: TaskClaimRelease(
            _claim(9, created_at=1.0),
            _claim(2),
            0.5,
        ),
        lambda: TaskClaimRelease(_claim(9), _claim(2), LEASE_TIMEOUT + 0.0001),
        lambda: TaskCompletionEvidence(_claim(2, created_at=1.0), 0.5),
        lambda: TaskCompletionEvidence(_claim(2), LEASE_TIMEOUT + 0.0001),
    ],
)
def test_release_and_completion_evidence_validate_causal_relationships(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TaskClaimStore(7, (2, 9), (11,), 5.0),
        lambda: TaskClaimStore(7, (7, 7), (11,), 5.0),
        lambda: TaskClaimStore(7, (7,), (), 5.0),
        lambda: TaskClaimStore(7, (7,), (11, 11), 5.0),
        lambda: TaskClaimStore(7, (7,), (11,), 0.0),
        lambda: TaskClaimStore(7, (7,), (11,), 5.0, freshness_timeout=5.0),
    ],
)
def test_claim_store_rejects_invalid_membership_and_lease_policy(
    factory: Callable[[], TaskClaimStore],
) -> None:
    with pytest.raises(ValueError):
        factory()


def test_claim_age_boundaries_distinguish_fresh_stale_and_expired() -> None:
    store = _store()
    peer_claim = _claim(9)
    assert store.receive_claim(peer_claim, received_at=1.0) is True

    at_freshness_boundary = store.view(TASK_ID, 3.0)
    assert at_freshness_boundary.state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    assert at_freshness_boundary.claim_age == FRESHNESS_TIMEOUT
    assert at_freshness_boundary.claim_freshness is ClaimFreshness.FRESH
    assert store.can_create_claim(TASK_ID, 3.0) is False

    just_stale = store.view(TASK_ID, 3.0001)
    assert just_stale.state is TaskOwnershipState.CLAIMED_BY_PEER_STALE
    assert just_stale.claim_freshness is ClaimFreshness.STALE
    assert store.can_create_claim(TASK_ID, 3.0001) is False

    at_lease_boundary = store.view(TASK_ID, 6.0)
    assert at_lease_boundary.state is TaskOwnershipState.CLAIMED_BY_PEER_STALE
    assert at_lease_boundary.claim_freshness is ClaimFreshness.STALE
    assert store.can_create_claim(TASK_ID, 6.0) is False
    with pytest.raises(ValueError, match="valid local claim"):
        store.create_claim(TASK_ID, 6.0)

    after_lease = store.view(TASK_ID, 6.0001)
    assert after_lease.state is TaskOwnershipState.UNCLAIMED
    assert after_lease.known_owner_agent_id is None
    assert after_lease.claim_freshness is None
    assert after_lease.known_claim_observations[0].freshness is ClaimFreshness.EXPIRED
    assert store.can_create_claim(TASK_ID, 6.0001) is True
    replacement = store.create_claim(TASK_ID, 6.0001)
    assert replacement.owner_agent_id == store.owner_agent_id
    assert store.view(TASK_ID, 6.0001).state is TaskOwnershipState.OWNED_BY_SELF


def test_expired_self_claim_is_replaced_at_the_next_owner_local_epoch() -> None:
    store = _store(owner_agent_id=7, agent_ids=(7,))
    first = store.create_claim(TASK_ID, 0.0)

    assert store.can_create_claim(TASK_ID, LEASE_TIMEOUT) is False
    assert store.owns_task(TASK_ID, LEASE_TIMEOUT) is True
    assert store.can_create_claim(TASK_ID, LEASE_TIMEOUT + 0.0001) is True
    assert store.owns_task(TASK_ID, LEASE_TIMEOUT + 0.0001) is False

    replacement = store.create_claim(TASK_ID, LEASE_TIMEOUT + 0.0001)
    assert replacement.epoch == first.epoch + 1
    assert replacement.claim_id != first.claim_id
    assert store.owns_task(TASK_ID, LEASE_TIMEOUT + 0.0001) is True


def test_explicit_renewal_increments_exactly_one_epoch_and_refreshes() -> None:
    store = _store(owner_agent_id=7, agent_ids=(7,))
    first = store.create_claim(TASK_ID, 0.0)
    stale_view = store.view(TASK_ID, 3.0)
    assert stale_view.state is TaskOwnershipState.OWNED_BY_SELF
    assert stale_view.claim_freshness is ClaimFreshness.STALE

    renewed = store.renew_claim(TASK_ID, 3.0)

    assert renewed.epoch == first.epoch + 1
    assert renewed.created_at == 3.0
    assert store.claims_for_broadcast(3.0) == (renewed,)
    refreshed_view = store.view(TASK_ID, 3.0)
    assert refreshed_view.claim_id == renewed.claim_id
    assert refreshed_view.claim_age == 0.0
    assert refreshed_view.claim_freshness is ClaimFreshness.FRESH


def test_new_peer_epoch_refreshes_stale_evidence_without_replaying_old_claim() -> None:
    store = _store()
    first = _claim(9, epoch=1)
    store.receive_claim(first, 0.0)
    assert store.view(TASK_ID, 3.0).state is (TaskOwnershipState.CLAIMED_BY_PEER_STALE)

    renewal = _claim(9, epoch=2, created_at=3.0)
    assert store.receive_claim(renewal, 3.0) is True
    refreshed = store.view(TASK_ID, 3.0)
    assert refreshed.state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    assert refreshed.claim_id == renewal.claim_id
    assert refreshed.claim_age == 0.0
    assert store.receive_claim(first, 3.0) is False
    assert store.view(TASK_ID, 3.0).claim_id == renewal.claim_id


def test_duplicate_old_future_and_equivocating_claims_are_safe() -> None:
    store = _store()
    current = _claim(9, epoch=2)
    assert store.receive_claim(current, 0.0) is True

    # duplicate receipt cannot extend the receiver-local evidence lifetime.
    assert store.receive_claim(current, 3.0) is False
    duplicate_view = store.view(TASK_ID, 3.0)
    assert duplicate_view.claim_age == 3.0
    assert duplicate_view.claim_freshness is ClaimFreshness.STALE

    assert store.receive_claim(_claim(9, epoch=1), 3.0) is False
    assert store.view(TASK_ID, 3.0).claim_id == current.claim_id

    equivocation = _claim(9, epoch=2, created_at=1.0)
    with pytest.raises(ValueError, match="conflicting equal-epoch"):
        store.receive_claim(equivocation, 3.0)

    future = _claim(9, epoch=3, created_at=4.0)
    with pytest.raises(ValueError, match="cannot follow receipt"):
        store.receive_claim(future, 3.0)


def test_owner_epochs_are_local_and_never_override_owner_id_priority() -> None:
    low_owner_old_epoch = _claim(2, epoch=1)
    high_owner_new_epoch = _claim(9, epoch=99)

    assert select_winning_claim((high_owner_new_epoch, low_owner_old_epoch)) == (
        low_owner_old_epoch
    )


def test_pure_reconciliation_is_independent_of_message_arrival_order() -> None:
    owner_nine_old = _claim(9, epoch=1)
    owner_nine_new = _claim(9, epoch=8, created_at=1.0)
    owner_two = _claim(2, epoch=1)
    owner_six = _claim(6, epoch=50)
    claims = (owner_nine_old, owner_two, owner_nine_new, owner_six)
    expected_newest = (owner_two, owner_six, owner_nine_new)

    for arrival_order in permutations(claims):
        assert newest_claims_by_owner(arrival_order) == expected_newest
        assert select_winning_claim(arrival_order) == owner_two


def test_contested_replicas_reconcile_and_only_the_local_loser_releases() -> None:
    stores = {agent_id: _store(agent_id) for agent_id in (2, 7, 9)}
    owner_two_claim = stores[2].create_claim(TASK_ID, 0.0)
    owner_nine_claim = stores[9].create_claim(TASK_ID, 0.0)

    for store in stores.values():
        store.receive_claim(owner_nine_claim, 0.0)
        store.receive_claim(owner_two_claim, 0.0)
        assert store.view(TASK_ID, 0.0).state is TaskOwnershipState.CONTESTED

    decisions = {
        agent_id: store.reconcile(TASK_ID, 1.0) for agent_id, store in stores.items()
    }
    assert all(decision is not None for decision in decisions.values())
    assert {
        decision.winner.owner_agent_id
        for decision in decisions.values()
        if decision is not None
    } == {2}
    assert decisions[2] is not None and decisions[2].local_release is None
    assert decisions[7] is not None and decisions[7].local_release is None
    assert decisions[9] is not None and decisions[9].local_release is not None

    views = {agent_id: store.view(TASK_ID, 1.0) for agent_id, store in stores.items()}
    assert views[2].state is TaskOwnershipState.OWNED_BY_SELF
    assert views[7].state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    assert views[9].state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    assert all(not view.contested for view in views.values())
    assert all(view.reconciliation_winner_agent_id == 2 for view in views.values())
    assert sum(store.owns_task(TASK_ID, 1.0) for store in stores.values()) == 1
    assert stores[9].claims_for_broadcast(1.0) == ()
    assert stores[9].releases_for_broadcast() == (decisions[9].local_release,)


def test_reconciliation_preserves_a_stale_but_valid_peer_winner() -> None:
    observer = _store()
    observer.receive_claim(_claim(9), 0.0)
    observer.receive_claim(_claim(2), 0.0)
    assert observer.view(TASK_ID, 3.0).state is TaskOwnershipState.CONTESTED

    decision = observer.reconcile(TASK_ID, 3.0)

    assert decision is not None
    assert decision.winner.owner_agent_id == 2
    resolved = observer.view(TASK_ID, 3.0)
    assert resolved.state is TaskOwnershipState.CLAIMED_BY_PEER_STALE
    assert resolved.claim_freshness is ClaimFreshness.STALE


def test_new_losing_owner_epoch_reopens_a_resolved_contest() -> None:
    observer = _store()
    winner = _claim(2)
    first_loser = _claim(9)
    observer.receive_claim(first_loser, 0.0)
    observer.receive_claim(winner, 0.0)
    first_decision = observer.reconcile(TASK_ID, 1.0)
    assert first_decision is not None
    assert first_decision.winner == winner
    assert observer.view(TASK_ID, 1.0).contested is False

    successor = _claim(9, epoch=2, created_at=2.0)
    assert observer.receive_claim(successor, 2.0) is True
    assert observer.view(TASK_ID, 2.0).state is TaskOwnershipState.CONTESTED

    second_decision = observer.reconcile(TASK_ID, 2.0)
    assert second_decision is not None
    assert second_decision.winner == winner
    assert second_decision.losers == (successor,)


def test_release_tombstone_arriving_first_blocks_delayed_claim_resurrection() -> None:
    winner = _claim(2)
    losing_claim = _claim(9, epoch=4)
    release = TaskClaimRelease(
        losing_claim=losing_claim,
        winning_claim=winner,
        created_at=1.0,
    )
    observer = _store()

    assert observer.receive_release(release, 1.0) is True
    initial = observer.view(TASK_ID, 1.0)
    assert initial.state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    assert initial.known_owner_agent_id == 2
    assert losing_claim.claim_id in initial.released_claim_ids

    assert observer.receive_claim(losing_claim, 2.0) is False
    assert observer.receive_claim(_claim(9, epoch=3), 2.0) is False
    delayed = observer.view(TASK_ID, 2.0)
    assert delayed.known_owner_agent_id == 2
    assert delayed.reconciliation_winner_agent_id == 2
    assert delayed.state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH


def test_voluntary_release_stops_local_ownership_without_erasing_evidence() -> None:
    store = _store(owner_agent_id=7, agent_ids=(7,))
    claim = store.create_claim(TASK_ID, 0.0)

    release = store.release_claim(TASK_ID, 1.0)

    assert release.losing_claim == claim
    assert release.winning_claim is None
    assert store.owns_task(TASK_ID, 1.0) is False
    view = store.view(TASK_ID, 1.0)
    assert view.state is TaskOwnershipState.UNCLAIMED
    assert view.released is True
    assert view.released_claim_ids == (claim.claim_id,)
    assert view.known_claim_observations[0].released is True
    assert store.claims_for_broadcast(1.0) == ()
    assert store.releases_for_broadcast() == (release,)


def test_completion_is_absorbing_after_duplicates_conflicts_and_expiry() -> None:
    owner = _store(owner_agent_id=2, agent_ids=(1, 2, 9))
    winning_claim = owner.create_claim(TASK_ID, 0.0)
    completion = owner.create_completion(TASK_ID, 1.0)
    assert completion.claim == winning_claim
    assert owner.view(TASK_ID, 1.0).state is TaskOwnershipState.COMPLETE
    assert owner.owns_task(TASK_ID, 1.0) is False

    assert owner.receive_completion(completion, 1.0) is False
    lower_conflict = _claim(1, epoch=99, created_at=1.0)
    higher_conflict = _claim(9, epoch=99, created_at=1.0)
    assert owner.receive_claim(lower_conflict, 2.0) is True
    assert owner.receive_claim(higher_conflict, 2.0) is True
    owner.receive_release(
        TaskClaimRelease(
            losing_claim=higher_conflict,
            winning_claim=lower_conflict,
            created_at=2.0,
        ),
        2.0,
    )

    assert owner.view(TASK_ID, 20.0).state is TaskOwnershipState.COMPLETE
    assert owner.can_create_claim(TASK_ID, 20.0) is False
    assert owner.reconcile(TASK_ID, 20.0) is None
    with pytest.raises(ValueError, match="already complete"):
        owner.create_claim(TASK_ID, 20.0)


def test_delayed_completion_retains_its_obsolete_claim_for_auditable_trace() -> None:
    observer = _store()
    current = _claim(9, epoch=2, created_at=1.0)
    completed_claim = _claim(9, epoch=1)
    observer.receive_claim(current, 1.0)

    completion = TaskCompletionEvidence(completed_claim, created_at=1.0)
    assert observer.receive_completion(completion, 2.0) is True

    view = observer.view(TASK_ID, 2.0)
    assert view.state is TaskOwnershipState.COMPLETE
    assert view.claim_id == completed_claim.claim_id
    assert {item.claim.claim_id for item in view.known_claim_observations} == {
        completed_claim.claim_id,
        current.claim_id,
    }
    completed_observation = next(
        item
        for item in view.known_claim_observations
        if item.claim.claim_id == completed_claim.claim_id
    )
    assert completed_observation.current_for_owner is False


def test_six_noncontiguous_replicas_converge_without_four_agent_assumptions() -> None:
    agent_ids = (2, 4, 6, 8, 10, 12)
    stores = {agent_id: _store(agent_id, agent_ids=agent_ids) for agent_id in agent_ids}
    low_owner_claim = stores[2].create_claim(TASK_ID, 0.0)
    high_owner_claim = stores[12].create_claim(TASK_ID, 0.0)

    for index, store in enumerate(stores.values()):
        order = (
            (low_owner_claim, high_owner_claim)
            if index % 2 == 0
            else (high_owner_claim, low_owner_claim)
        )
        for claim in order:
            store.receive_claim(claim, 0.0)
        assert store.view(TASK_ID, 0.0).state is TaskOwnershipState.CONTESTED

    decisions = tuple(store.reconcile(TASK_ID, 1.0) for store in stores.values())
    assert all(decision is not None for decision in decisions)
    assert {
        decision.winner.owner_agent_id for decision in decisions if decision is not None
    } == {2}
    assert sum(store.owns_task(TASK_ID, 1.0) for store in stores.values()) == 1
    assert stores[12].releases_for_broadcast()[0].releasing_agent_id == 12
    assert all(
        store.view(TASK_ID, 1.0).reconciliation_winner_agent_id == 2
        for store in stores.values()
    )
