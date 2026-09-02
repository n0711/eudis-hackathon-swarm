"""Model receiver-local task claims without consulting authoritative mission state.

Immutable evidence, local lease clocks, and deterministic reconciliation let
replicas diverge during partitions and converge after delivery resumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, TypeAlias

from .task import TaskOwnershipState
from .validation import (
    validate_positive_integer,
    validate_positive_real,
    validate_timestamp,
)

MAX_CLAIM_EPOCH = 2**63 - 1
ClaimId: TypeAlias = tuple[int, int, int]


def _validate_epoch(epoch: int) -> int:
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        raise ValueError("claim epoch must be an integer")
    if not 1 <= epoch <= MAX_CLAIM_EPOCH:
        raise ValueError(f"claim epoch must be between 1 and {MAX_CLAIM_EPOCH}")
    return epoch


@dataclass(frozen=True, slots=True)
class TaskClaim:
    """An immutable owner-scoped publication for one task."""

    task_id: int
    owner_agent_id: int
    epoch: int
    created_at: float
    freshness_timeout: float
    lease_timeout: float

    def __post_init__(self) -> None:
        validate_positive_integer(self.task_id, name="claim task_id")
        validate_positive_integer(self.owner_agent_id, name="claim owner_agent_id")
        _validate_epoch(self.epoch)
        validate_timestamp(
            self.created_at,
            previous=0.0,
            name="claim creation timestamp",
        )
        freshness_timeout = validate_positive_real(
            self.freshness_timeout,
            name="claim freshness_timeout",
        )
        lease_timeout = validate_positive_real(
            self.lease_timeout,
            name="claim lease_timeout",
        )
        if lease_timeout <= freshness_timeout:
            raise ValueError("claim lease_timeout must exceed freshness_timeout")

    @property
    def claim_id(self) -> ClaimId:
        """Return the structural identifier that cannot disagree with the claim."""

        return (self.task_id, self.owner_agent_id, self.epoch)


@dataclass(frozen=True, slots=True)
class TaskClaimRelease:
    """Evidence that an owner released one exact claim, optionally to a winner."""

    losing_claim: TaskClaim
    winning_claim: TaskClaim | None
    created_at: float

    def __post_init__(self) -> None:
        winning_claim = self.winning_claim
        if winning_claim is not None:
            if self.losing_claim.task_id != winning_claim.task_id:
                raise ValueError("release claims must describe the same task")
            if self.losing_claim.owner_agent_id == winning_claim.owner_agent_id:
                raise ValueError("release claims must have different owners")
            if winning_claim.owner_agent_id > self.losing_claim.owner_agent_id:
                raise ValueError("release winner must have the lower owner agent ID")
            if (
                self.losing_claim.freshness_timeout != winning_claim.freshness_timeout
                or self.losing_claim.lease_timeout != winning_claim.lease_timeout
            ):
                raise ValueError("release claims must use the same lease policy")
        validate_timestamp(
            self.created_at,
            # only the releasing owner's timestamps share one clock domain.
            previous=self.losing_claim.created_at,
            name="claim-release timestamp",
        )
        if self.created_at - self.losing_claim.created_at > (
            self.losing_claim.lease_timeout
        ):
            raise ValueError("claim release must be created while its lease is valid")

    @property
    def task_id(self) -> int:
        return self.losing_claim.task_id

    @property
    def releasing_agent_id(self) -> int:
        return self.losing_claim.owner_agent_id


@dataclass(frozen=True, slots=True)
class TaskCompletionEvidence:
    """Self-contained terminal evidence tied to one exact task claim."""

    claim: TaskClaim
    created_at: float

    def __post_init__(self) -> None:
        validate_timestamp(
            self.created_at,
            previous=self.claim.created_at,
            name="task-completion timestamp",
        )
        if self.created_at - self.claim.created_at > self.claim.lease_timeout:
            raise ValueError("task completion must be created while its lease is valid")

    @property
    def task_id(self) -> int:
        return self.claim.task_id

    @property
    def owner_agent_id(self) -> int:
        return self.claim.owner_agent_id


def newest_claims_by_owner(claims: Iterable[TaskClaim]) -> tuple[TaskClaim, ...]:
    """Keep each owner's newest epoch without comparing epochs across owners."""

    newest: dict[int, TaskClaim] = {}
    task_id: int | None = None
    for claim in claims:
        if task_id is None:
            task_id = claim.task_id
        elif claim.task_id != task_id:
            raise ValueError("winner selection requires claims for one task")
        previous = newest.get(claim.owner_agent_id)
        if previous is None or claim.epoch > previous.epoch:
            newest[claim.owner_agent_id] = claim
            continue
        if claim.epoch == previous.epoch and claim != previous:
            raise ValueError("one owner cannot publish conflicting equal-epoch claims")
    return tuple(newest[owner_agent_id] for owner_agent_id in sorted(newest))


def claim_reconciliation_key(claim: TaskClaim) -> int:
    """Return the stable cross-owner key; epochs deliberately do not participate."""

    return claim.owner_agent_id


def select_winning_claim(claims: Iterable[TaskClaim]) -> TaskClaim:
    """Choose the lowest owner ID after owner-local epoch normalization."""

    newest = newest_claims_by_owner(claims)
    if not newest:
        raise ValueError("winner selection requires at least one claim")
    return min(newest, key=claim_reconciliation_key)


class ClaimFreshness(str, Enum):
    """Receiver-local age classification for one immutable claim publication."""

    FRESH = "FRESH"
    STALE = "STALE"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class TaskClaimObservation:
    """One stored claim rendered at a receiver-local logical time."""

    claim: TaskClaim
    received_at: float
    age: float
    freshness: ClaimFreshness
    current_for_owner: bool
    released: bool
    reconciled_loser: bool


@dataclass(frozen=True, slots=True)
class TaskOwnershipView:
    """A trace-ready receiver-local interpretation of one task."""

    task_id: int
    state: TaskOwnershipState
    known_owner_agent_id: int | None
    claim_id: ClaimId | None
    epoch: int | None
    claim_age: float | None
    claim_freshness: ClaimFreshness | None
    contested: bool
    reconciliation_winner_agent_id: int | None
    reconciliation_winner_claim_id: ClaimId | None
    released: bool
    released_claim_ids: tuple[ClaimId, ...]
    completion: TaskCompletionEvidence | None
    known_claim_observations: tuple[TaskClaimObservation, ...]

    @property
    def complete(self) -> bool:
        return self.state is TaskOwnershipState.COMPLETE


@dataclass(frozen=True, slots=True)
class TaskClaimStoreSnapshot:
    """All task views owned by one receiver at one logical instant."""

    owner_agent_id: int
    timestamp: float
    task_views: tuple[TaskOwnershipView, ...]


@dataclass(frozen=True, slots=True)
class TaskReconciliationDecision:
    """The deterministic result of one explicit local reconciliation phase."""

    task_id: int
    winner: TaskClaim
    losers: tuple[TaskClaim, ...]
    created_at: float
    local_release: TaskClaimRelease | None = None


@dataclass(frozen=True, slots=True)
class _StoredClaim:
    claim: TaskClaim
    received_at: float


@dataclass(frozen=True, slots=True)
class _OwnershipContext:
    valid_claims: tuple[TaskClaim, ...]
    selected_claim: TaskClaim | None
    contested: bool
    resolved_claim: TaskClaim | None


class TaskClaimStore:
    """Maintain one UAV's claim evidence and decisions using only delivered data."""

    def __init__(
        self,
        owner_agent_id: int,
        agent_ids: Iterable[int],
        task_ids: Iterable[int],
        lease_duration: float,
        *,
        freshness_timeout: float | None = None,
    ) -> None:
        self._owner_agent_id = validate_positive_integer(
            owner_agent_id,
            name="claim-store owner_agent_id",
        )
        supplied_agent_ids = tuple(agent_ids)
        for agent_id in supplied_agent_ids:
            validate_positive_integer(agent_id, name="claim-store agent_id")
        if len(set(supplied_agent_ids)) != len(supplied_agent_ids):
            raise ValueError("claim-store agent_ids must be unique")
        self._agent_ids = tuple(sorted(supplied_agent_ids))
        self._agent_id_set = frozenset(self._agent_ids)
        if owner_agent_id not in self._agent_id_set:
            raise ValueError("claim-store agent_ids must include its owner")

        supplied_task_ids = tuple(task_ids)
        for task_id in supplied_task_ids:
            validate_positive_integer(task_id, name="claim-store task_id")
        if not supplied_task_ids:
            raise ValueError("claim-store requires at least one task")
        if len(set(supplied_task_ids)) != len(supplied_task_ids):
            raise ValueError("claim-store task_ids must be unique")
        self._task_ids = tuple(sorted(supplied_task_ids))
        self._task_id_set = frozenset(self._task_ids)

        self._lease_timeout = validate_positive_real(
            lease_duration,
            name="claim-store lease_duration",
        )
        chosen_freshness = (
            self._lease_timeout / 2.0
            if freshness_timeout is None
            else validate_positive_real(
                freshness_timeout,
                name="claim-store freshness_timeout",
            )
        )
        if chosen_freshness >= self._lease_timeout:
            raise ValueError("freshness_timeout must be less than lease_duration")
        self._freshness_timeout = chosen_freshness

        self._claims: dict[int, dict[ClaimId, _StoredClaim]] = {
            task_id: {} for task_id in self._task_ids
        }
        self._highwater: dict[int, dict[int, int]] = {
            task_id: {agent_id: 0 for agent_id in self._agent_ids}
            for task_id in self._task_ids
        }
        self._current_claim_ids: dict[int, dict[int, ClaimId]] = {
            task_id: {} for task_id in self._task_ids
        }
        self._released_claim_ids: dict[int, set[ClaimId]] = {
            task_id: set() for task_id in self._task_ids
        }
        self._known_releases: dict[int, dict[ClaimId, TaskClaimRelease]] = {
            task_id: {} for task_id in self._task_ids
        }
        self._known_completions: dict[int, dict[ClaimId, TaskCompletionEvidence]] = {
            task_id: {} for task_id in self._task_ids
        }
        self._resolved_winner_ids: dict[int, ClaimId] = {}
        self._reconciled_claim_ids: dict[int, set[ClaimId]] = {
            task_id: set() for task_id in self._task_ids
        }
        self._outgoing_claims: dict[int, TaskClaim] = {}
        self._outgoing_releases: dict[ClaimId, TaskClaimRelease] = {}
        self._outgoing_completions: dict[int, TaskCompletionEvidence] = {}
        self._last_timestamp: float | None = None

    @property
    def owner_agent_id(self) -> int:
        return self._owner_agent_id

    @property
    def agent_ids(self) -> tuple[int, ...]:
        return self._agent_ids

    @property
    def task_ids(self) -> tuple[int, ...]:
        return self._task_ids

    @property
    def freshness_timeout(self) -> float:
        return self._freshness_timeout

    @property
    def lease_timeout(self) -> float:
        return self._lease_timeout

    @property
    def last_timestamp(self) -> float | None:
        return self._last_timestamp

    def create_claim(self, task_id: int, timestamp: float) -> TaskClaim:
        """Create the next local epoch only when this receiver sees no valid owner."""

        self._require_task(task_id)
        timestamp = self._observe_time(timestamp)
        if self._is_complete(task_id):
            raise ValueError(f"Task {task_id} is already complete locally")
        if self._ownership_context(task_id, timestamp).valid_claims:
            raise ValueError(f"Task {task_id} still has valid local claim evidence")
        claim = self._new_local_claim(task_id, timestamp)
        self._record_claim(claim, timestamp)
        self._outgoing_claims[task_id] = claim
        self._resolved_winner_ids.pop(task_id, None)
        return claim

    def renew_claim(self, task_id: int, timestamp: float) -> TaskClaim:
        """Publish exactly the next owner-local epoch while self remains owner."""

        self._require_task(task_id)
        timestamp = self._observe_time(timestamp)
        view = self._view(task_id, timestamp)
        if view.state is not TaskOwnershipState.OWNED_BY_SELF:
            raise ValueError(f"Task {task_id} is not locally owned by this UAV")
        claim = self._new_local_claim(task_id, timestamp)
        self._record_claim(claim, timestamp)
        self._outgoing_claims[task_id] = claim
        return claim

    def receive_claim(self, claim: TaskClaim, received_at: float) -> bool:
        """Apply one delivered publication and report whether it advanced its owner."""

        received_at = self._observe_time(received_at)
        self._validate_delivered_claim(claim, received_at)
        return self._record_claim(claim, received_at)

    def advance_time(self, timestamp: float) -> tuple[int, ...]:
        """Advance only this receiver's lease clock and report changed task views."""

        previous_timestamp = self._last_timestamp
        prior_states = {
            task_id: self._view(
                task_id,
                0.0 if previous_timestamp is None else previous_timestamp,
            ).state
            for task_id in self._task_ids
        }
        timestamp = self._observe_time(timestamp)
        return tuple(
            task_id
            for task_id in self._task_ids
            if self._view(task_id, timestamp).state is not prior_states[task_id]
        )

    def can_create_claim(self, task_id: int, timestamp: float) -> bool:
        """Return whether all locally known current claims are strictly lease-expired."""

        self._require_task(task_id)
        timestamp = self._observe_time(timestamp)
        return (
            not self._is_complete(task_id)
            and not self._ownership_context(
                task_id,
                timestamp,
            ).valid_claims
        )

    def owns_task(self, task_id: int, timestamp: float) -> bool:
        """Return whether this receiver may currently act as the task owner."""

        return self.view(task_id, timestamp).state is TaskOwnershipState.OWNED_BY_SELF

    def reconcile(
        self,
        task_id: int,
        timestamp: float,
    ) -> TaskReconciliationDecision | None:
        """Resolve an already visible contest and release only a local losing claim."""

        self._require_task(task_id)
        timestamp = self._observe_time(timestamp)
        if self._is_complete(task_id):
            return None
        context = self._ownership_context(task_id, timestamp)
        if not context.contested:
            return None
        winner = select_winning_claim(context.valid_claims)
        losers = tuple(
            claim
            for claim in sorted(
                context.valid_claims,
                key=lambda item: item.owner_agent_id,
            )
            if claim.owner_agent_id != winner.owner_agent_id
        )
        self._resolved_winner_ids[task_id] = winner.claim_id
        # only exact generations in this decision are suppressed afterward.
        self._reconciled_claim_ids[task_id].update(
            claim.claim_id for claim in context.valid_claims
        )

        local_release: TaskClaimRelease | None = None
        local_loser = next(
            (claim for claim in losers if claim.owner_agent_id == self._owner_agent_id),
            None,
        )
        if local_loser is not None:
            local_release = TaskClaimRelease(
                losing_claim=local_loser,
                winning_claim=winner,
                created_at=timestamp,
            )
            self._record_release(local_release, timestamp, locally_created=True)

        return TaskReconciliationDecision(
            task_id=task_id,
            winner=winner,
            losers=losers,
            created_at=timestamp,
            local_release=local_release,
        )

    def reconcile_all(
        self,
        timestamp: float,
    ) -> tuple[TaskReconciliationDecision, ...]:
        """Resolve every task currently contested in stable task-ID order."""

        timestamp = self._observe_time(timestamp)
        decisions: list[TaskReconciliationDecision] = []
        for task_id in self._task_ids:
            decision = self.reconcile(task_id, timestamp)
            if decision is not None:
                decisions.append(decision)
        return tuple(decisions)

    def release_claim(
        self,
        task_id: int,
        timestamp: float,
    ) -> TaskClaimRelease:
        """Voluntarily terminate the exact locally actionable claim."""

        self._require_task(task_id)
        timestamp = self._observe_time(timestamp)
        view = self._view(task_id, timestamp)
        if view.state is not TaskOwnershipState.OWNED_BY_SELF or view.claim_id is None:
            raise ValueError(f"Task {task_id} is not locally actionable")
        release = TaskClaimRelease(
            losing_claim=self._claims[task_id][view.claim_id].claim,
            winning_claim=None,
            created_at=timestamp,
        )
        self._record_release(release, timestamp, locally_created=True)
        return release

    def receive_release(
        self,
        release: TaskClaimRelease,
        received_at: float,
    ) -> bool:
        """Apply exact losing-claim evidence without invalidating any successor."""

        received_at = self._observe_time(received_at)
        self._validate_release(release, received_at)
        if (
            release.releasing_agent_id == self._owner_agent_id
            and self._outgoing_releases.get(release.losing_claim.claim_id) != release
        ):
            raise ValueError("only the local losing owner may originate its release")
        return self._record_release(release, received_at, locally_created=False)

    def create_completion(
        self,
        task_id: int,
        timestamp: float,
    ) -> TaskCompletionEvidence:
        """Create terminal evidence only while the local claim is actionable."""

        self._require_task(task_id)
        timestamp = self._observe_time(timestamp)
        view = self._view(task_id, timestamp)
        if view.state is not TaskOwnershipState.OWNED_BY_SELF or view.claim_id is None:
            raise ValueError(f"Task {task_id} is not locally actionable")
        claim = self._claims[task_id][view.claim_id].claim
        evidence = TaskCompletionEvidence(claim=claim, created_at=timestamp)
        self._record_completion(evidence, timestamp, locally_created=True)
        return evidence

    def receive_completion(
        self,
        evidence: TaskCompletionEvidence,
        received_at: float,
    ) -> bool:
        """Accept self-contained delivered completion as an absorbing local fact."""

        received_at = self._observe_time(received_at)
        self._validate_completion(evidence, received_at)
        return self._record_completion(evidence, received_at, locally_created=False)

    def claims_for_broadcast(self, timestamp: float) -> tuple[TaskClaim, ...]:
        """Return current unexpired local claims for persistent network retry."""

        timestamp = self._observe_time(timestamp)
        claims: list[TaskClaim] = []
        for task_id, claim in sorted(self._outgoing_claims.items()):
            if self._is_complete(task_id):
                continue
            valid = self._valid_claims_by_owner(task_id, timestamp)
            if valid.get(self._owner_agent_id) == claim:
                claims.append(claim)
        return tuple(claims)

    def releases_for_broadcast(self) -> tuple[TaskClaimRelease, ...]:
        """Return persistent locally originated releases in structural ID order."""

        return tuple(
            self._outgoing_releases[claim_id]
            for claim_id in sorted(self._outgoing_releases)
        )

    def completions_for_broadcast(self) -> tuple[TaskCompletionEvidence, ...]:
        """Return persistent locally originated terminal evidence by task ID."""

        return tuple(
            self._outgoing_completions[task_id]
            for task_id in sorted(self._outgoing_completions)
        )

    def view(self, task_id: int, timestamp: float) -> TaskOwnershipView:
        """Render one task using this receiver's evidence and logical clock only."""

        self._require_task(task_id)
        timestamp = self._validate_read_time(timestamp)
        return self._view(task_id, timestamp)

    def snapshot(self, timestamp: float) -> TaskClaimStoreSnapshot:
        """Render every task in deterministic order for tracing or inspection."""

        timestamp = self._validate_read_time(timestamp)
        return TaskClaimStoreSnapshot(
            owner_agent_id=self._owner_agent_id,
            timestamp=timestamp,
            task_views=tuple(
                self._view(task_id, timestamp) for task_id in self._task_ids
            ),
        )

    def _new_local_claim(self, task_id: int, timestamp: float) -> TaskClaim:
        previous_epoch = self._highwater[task_id][self._owner_agent_id]
        if previous_epoch >= MAX_CLAIM_EPOCH:
            raise OverflowError("claim epoch has reached its maximum value")
        return TaskClaim(
            task_id=task_id,
            owner_agent_id=self._owner_agent_id,
            epoch=previous_epoch + 1,
            created_at=timestamp,
            freshness_timeout=self._freshness_timeout,
            lease_timeout=self._lease_timeout,
        )

    def _record_claim(self, claim: TaskClaim, received_at: float) -> bool:
        task_id = claim.task_id
        owner_agent_id = claim.owner_agent_id
        claim_id = claim.claim_id
        known = self._claims[task_id].get(claim_id)
        if known is not None:
            if known.claim != claim:
                raise ValueError(
                    "one owner cannot publish conflicting equal-epoch claims"
                )
            # an immutable duplicate is idempotent and never refreshes support time.
            return False

        highwater = self._highwater[task_id][owner_agent_id]
        if claim.epoch < highwater:
            # obsolete evidence stays auditable without becoming current again.
            self._claims[task_id][claim_id] = _StoredClaim(
                claim=claim,
                received_at=received_at,
            )
            return False
        if claim.epoch == highwater:
            current_claim_id = self._current_claim_ids[task_id].get(owner_agent_id)
            if current_claim_id is not None:
                current = self._claims[task_id][current_claim_id].claim
                if current != claim:
                    raise ValueError(
                        "one owner cannot publish conflicting equal-epoch claims"
                    )
            return False

        self._claims[task_id][claim_id] = _StoredClaim(
            claim=claim,
            received_at=received_at,
        )
        self._highwater[task_id][owner_agent_id] = claim.epoch
        self._current_claim_ids[task_id][owner_agent_id] = claim_id

        resolved_id = self._resolved_winner_ids.get(task_id)
        if resolved_id is not None and resolved_id[1] == owner_agent_id:
            # a newer owner-scoped publication continues the same resolved owner.
            self._resolved_winner_ids[task_id] = claim_id
            self._reconciled_claim_ids[task_id].add(claim_id)
        return True

    def _record_release(
        self,
        release: TaskClaimRelease,
        received_at: float,
        *,
        locally_created: bool,
    ) -> bool:
        task_id = release.task_id
        losing_id = release.losing_claim.claim_id
        previous = self._known_releases[task_id].get(losing_id)
        if previous is not None:
            if previous != release:
                raise ValueError("one exact losing claim cannot name different winners")
            return False

        self._record_claim(release.losing_claim, received_at)
        winning_claim = release.winning_claim
        if winning_claim is not None:
            self._record_claim(winning_claim, received_at)
        self._highwater[task_id][release.releasing_agent_id] = max(
            self._highwater[task_id][release.releasing_agent_id],
            release.losing_claim.epoch,
        )
        self._released_claim_ids[task_id].add(losing_id)
        self._known_releases[task_id][losing_id] = release
        if self._outgoing_claims.get(task_id) == release.losing_claim:
            self._outgoing_claims.pop(task_id)

        if winning_claim is None:
            if self._resolved_winner_ids.get(task_id) == losing_id:
                self._resolved_winner_ids.pop(task_id)
        else:
            resolved_id = self._resolved_winner_ids.get(task_id)
            valid_by_owner = self._valid_claims_by_owner(task_id, received_at)
            resolved_is_valid = (
                resolved_id is not None
                and valid_by_owner.get(resolved_id[1], None) is not None
            )
            if (
                not resolved_is_valid
                or resolved_id == losing_id
                or (
                    resolved_id is not None
                    and resolved_id[1] == winning_claim.owner_agent_id
                )
            ):
                self._resolved_winner_ids[task_id] = winning_claim.claim_id
                self._reconciled_claim_ids[task_id].add(winning_claim.claim_id)
        self._reconciled_claim_ids[task_id].add(losing_id)
        if locally_created:
            self._outgoing_releases[losing_id] = release
        return True

    def _record_completion(
        self,
        evidence: TaskCompletionEvidence,
        received_at: float,
        *,
        locally_created: bool,
    ) -> bool:
        task_id = evidence.task_id
        claim_id = evidence.claim.claim_id
        previous = self._known_completions[task_id].get(claim_id)
        if previous is not None:
            if previous != evidence:
                raise ValueError("one exact claim cannot carry conflicting completions")
            return False
        self._record_claim(evidence.claim, received_at)
        self._known_completions[task_id][claim_id] = evidence
        self._outgoing_claims.pop(task_id, None)
        if locally_created:
            self._outgoing_completions[task_id] = evidence
        return True

    def _ownership_context(self, task_id: int, timestamp: float) -> _OwnershipContext:
        valid_by_owner = self._valid_claims_by_owner(task_id, timestamp)
        resolved_claim: TaskClaim | None = None
        resolved_id = self._resolved_winner_ids.get(task_id)
        if resolved_id is not None:
            resolved_claim = valid_by_owner.get(resolved_id[1])
            if resolved_claim is not None and resolved_claim.claim_id != resolved_id:
                resolved_claim = None

        reconciled_ids = self._reconciled_claim_ids[task_id]
        candidate_claims = tuple(
            claim
            for _, claim in sorted(valid_by_owner.items())
            if claim.claim_id not in reconciled_ids
            or (
                resolved_claim is not None and claim.claim_id == resolved_claim.claim_id
            )
        )
        if not candidate_claims:
            return _OwnershipContext((), None, False, None)
        if len(candidate_claims) > 1:
            return _OwnershipContext(
                candidate_claims,
                None,
                True,
                resolved_claim,
            )
        only_claim = next(iter(candidate_claims))
        return _OwnershipContext(
            candidate_claims,
            only_claim,
            False,
            resolved_claim,
        )

    def _valid_claims_by_owner(
        self,
        task_id: int,
        timestamp: float,
    ) -> dict[int, TaskClaim]:
        valid: dict[int, TaskClaim] = {}
        for owner_agent_id, claim_id in self._current_claim_ids[task_id].items():
            if claim_id in self._released_claim_ids[task_id]:
                continue
            stored = self._claims[task_id][claim_id]
            if self._freshness(stored, timestamp) is ClaimFreshness.EXPIRED:
                continue
            valid[owner_agent_id] = stored.claim
        return valid

    def _view(self, task_id: int, timestamp: float) -> TaskOwnershipView:
        observations = self._observations(task_id, timestamp)
        released_ids = tuple(sorted(self._released_claim_ids[task_id]))
        completion = self._selected_completion(task_id)
        context = self._ownership_context(task_id, timestamp)

        selected = context.selected_claim
        state = TaskOwnershipState.UNCLAIMED
        selected_observation: TaskClaimObservation | None = None
        if completion is not None:
            state = TaskOwnershipState.COMPLETE
            selected = completion.claim
        elif context.contested:
            state = TaskOwnershipState.CONTESTED
        elif selected is not None:
            selected_observation = next(
                item
                for item in observations
                if item.claim.claim_id == selected.claim_id
            )
            if selected.owner_agent_id == self._owner_agent_id:
                state = TaskOwnershipState.OWNED_BY_SELF
            elif selected_observation.freshness is ClaimFreshness.FRESH:
                state = TaskOwnershipState.CLAIMED_BY_PEER_FRESH
            else:
                state = TaskOwnershipState.CLAIMED_BY_PEER_STALE

        if selected is not None and selected_observation is None:
            selected_observation = next(
                (
                    item
                    for item in observations
                    if item.claim.claim_id == selected.claim_id
                ),
                None,
            )
        resolved = context.resolved_claim
        return TaskOwnershipView(
            task_id=task_id,
            state=state,
            known_owner_agent_id=(
                None if selected is None else selected.owner_agent_id
            ),
            claim_id=None if selected is None else selected.claim_id,
            epoch=None if selected is None else selected.epoch,
            claim_age=(
                None if selected_observation is None else selected_observation.age
            ),
            claim_freshness=(
                None if selected_observation is None else selected_observation.freshness
            ),
            contested=context.contested,
            reconciliation_winner_agent_id=(
                None if resolved is None else resolved.owner_agent_id
            ),
            reconciliation_winner_claim_id=(
                None if resolved is None else resolved.claim_id
            ),
            released=bool(released_ids),
            released_claim_ids=released_ids,
            completion=completion,
            known_claim_observations=observations,
        )

    def _observations(
        self,
        task_id: int,
        timestamp: float,
    ) -> tuple[TaskClaimObservation, ...]:
        return tuple(
            TaskClaimObservation(
                claim=stored.claim,
                received_at=stored.received_at,
                age=timestamp - stored.received_at,
                freshness=self._freshness(stored, timestamp),
                current_for_owner=(
                    self._current_claim_ids[task_id].get(stored.claim.owner_agent_id)
                    == stored.claim.claim_id
                ),
                released=(stored.claim.claim_id in self._released_claim_ids[task_id]),
                reconciled_loser=(
                    stored.claim.claim_id in self._reconciled_claim_ids[task_id]
                    and stored.claim.claim_id != self._resolved_winner_ids.get(task_id)
                ),
            )
            for _, stored in sorted(
                self._claims[task_id].items(),
                key=lambda item: (item[0][1], item[0][2]),
            )
        )

    def _freshness(
        self,
        stored: _StoredClaim,
        timestamp: float,
    ) -> ClaimFreshness:
        age = timestamp - stored.received_at
        if age <= stored.claim.freshness_timeout:
            return ClaimFreshness.FRESH
        if age <= stored.claim.lease_timeout:
            return ClaimFreshness.STALE
        return ClaimFreshness.EXPIRED

    def _selected_completion(self, task_id: int) -> TaskCompletionEvidence | None:
        completions = self._known_completions[task_id]
        if not completions:
            return None
        winning_claim = select_winning_claim(
            evidence.claim for evidence in completions.values()
        )
        return completions[winning_claim.claim_id]

    def _is_complete(self, task_id: int) -> bool:
        return bool(self._known_completions[task_id])

    def _validate_delivered_claim(
        self,
        claim: TaskClaim,
        received_at: float,
    ) -> None:
        self._require_task(claim.task_id)
        self._require_agent(claim.owner_agent_id)
        if claim.created_at > received_at:
            raise ValueError("claim creation timestamp cannot follow receipt time")
        if (
            claim.freshness_timeout != self._freshness_timeout
            or claim.lease_timeout != self._lease_timeout
        ):
            raise ValueError("claim lease policy does not match this store")

    def _validate_release(
        self,
        release: TaskClaimRelease,
        received_at: float,
    ) -> None:
        self._validate_delivered_claim(release.losing_claim, received_at)
        if release.winning_claim is not None:
            self._validate_delivered_claim(release.winning_claim, received_at)
        if release.created_at > received_at:
            raise ValueError("claim-release timestamp cannot follow receipt time")

    def _validate_completion(
        self,
        evidence: TaskCompletionEvidence,
        received_at: float,
    ) -> None:
        self._validate_delivered_claim(evidence.claim, received_at)
        if evidence.created_at > received_at:
            raise ValueError("task-completion timestamp cannot follow receipt time")

    def _observe_time(self, timestamp: float) -> float:
        timestamp = validate_timestamp(
            timestamp,
            previous=0.0 if self._last_timestamp is None else self._last_timestamp,
            name="claim-store timestamp",
        )
        self._last_timestamp = timestamp
        return timestamp

    def _validate_read_time(self, timestamp: float) -> float:
        return validate_timestamp(
            timestamp,
            previous=0.0 if self._last_timestamp is None else self._last_timestamp,
            name="claim-store read timestamp",
        )

    def _require_task(self, task_id: int) -> None:
        if (
            not isinstance(task_id, int)
            or isinstance(task_id, bool)
            or task_id not in self._task_id_set
        ):
            raise KeyError(f"unknown task ID {task_id}")

    def _require_agent(self, agent_id: int) -> None:
        if (
            not isinstance(agent_id, int)
            or isinstance(agent_id, bool)
            or agent_id not in self._agent_id_set
        ):
            raise KeyError(f"unknown UAV ID {agent_id}")


__all__ = [
    "ClaimFreshness",
    "ClaimId",
    "MAX_CLAIM_EPOCH",
    "TaskClaim",
    "TaskClaimObservation",
    "TaskClaimRelease",
    "TaskClaimStore",
    "TaskClaimStoreSnapshot",
    "TaskCompletionEvidence",
    "TaskOwnershipView",
    "TaskReconciliationDecision",
    "claim_reconciliation_key",
    "newest_claims_by_owner",
    "select_winning_claim",
]
