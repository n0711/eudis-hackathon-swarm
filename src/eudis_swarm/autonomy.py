"""Pure, receiver-local extended finite-state machines for swarm autonomy.

The machines in this module consume typed events containing only evidence that
one UAV could possess.  They do not import the simulator, communication graph,
mission, or mutable remote agents.  Transition functions are pure; the small
``LocalAutonomyKernel`` applies their results and retains observer-only records.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Generic, Iterable, TypeVar

from .peer_state import PeerStatus
from .task import TaskOwnershipState
from .task_claims import ClaimFreshness, ClaimId, TaskOwnershipView
from .validation import (
    validate_positive_integer,
    validate_positive_real,
    validate_timestamp,
)

MAX_EFSM_COUNTER = 65_535


class MachineName(str, Enum):
    """Stable names used by observer traces and generated documentation."""

    CONTACT = "ContactEFSM"
    PEER_AVAILABILITY = "PeerAvailabilityEFSM"
    TASK_OWNERSHIP = "TaskOwnershipEFSM"
    COORDINATION_MODE = "CoordinationModeEFSM"


class AutonomyEffectKind(str, Enum):
    """Requested effects; transitions never execute these actions directly."""

    CONTACT_ACTIVE = "CONTACT_ACTIVE"
    CONTACT_DEGRADED = "CONTACT_DEGRADED"
    CONTACT_LOST = "CONTACT_LOST"
    CONTACT_RECOVERING = "CONTACT_RECOVERING"
    PEER_HEARD = "PEER_HEARD"
    PEER_SILENT = "PEER_SILENT"
    PEER_DECLARED_UNAVAILABLE = "PEER_DECLARED_UNAVAILABLE"
    REQUEST_TASK_SELECTION = "REQUEST_TASK_SELECTION"
    REQUEST_RECONCILIATION = "REQUEST_RECONCILIATION"
    STAND_DOWN_TASK = "STAND_DOWN_TASK"
    REDUCE_REMOTE_DEPENDENCE = "REDUCE_REMOTE_DEPENDENCE"
    ENTER_LOCAL_AUTONOMY = "ENTER_LOCAL_AUTONOMY"
    RESUME_COOPERATION = "RESUME_COOPERATION"


@dataclass(frozen=True, slots=True)
class RequestedEffect:
    """One declarative action request emitted by a pure transition."""

    kind: AutonomyEffectKind
    peer_agent_id: int | None = None
    task_id: int | None = None


_StateT = TypeVar("_StateT", bound=Enum)
_VariablesT = TypeVar("_VariablesT")
_RecordStateT = TypeVar("_RecordStateT", bound=Enum)
_RecordVariablesT = TypeVar("_RecordVariablesT")


@dataclass(frozen=True, slots=True)
class TransitionResult(Generic[_StateT, _VariablesT]):
    """The deterministic value returned by every transition function."""

    next_state: _StateT
    next_variables: _VariablesT
    effects: tuple[RequestedEffect, ...]
    guard: str
    reason: str


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """Observer-only record of a finite control-state change."""

    timestamp: float
    observer_agent_id: int
    sequence: int
    machine: MachineName
    previous_state: str
    event: str
    next_state: str
    guard: str
    reason: str
    effects: tuple[str, ...]
    peer_agent_id: int | None = None
    task_id: int | None = None


@dataclass(frozen=True, slots=True)
class TransitionRule:
    """Canonical, renderable description of one transition family."""

    source: str
    event: str
    guard: str
    target: str
    effects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MachineDefinition:
    """Finite control vocabulary and canonical transition table."""

    name: MachineName
    states: tuple[str, ...]
    initial_state: str
    rules: tuple[TransitionRule, ...]


class ContactState(str, Enum):
    """One receiver's inference about direct evidence from one peer."""

    UNKNOWN = "UNKNOWN"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    LOST = "LOST"
    RECOVERING = "RECOVERING"


class ContactEventKind(str, Enum):
    """Locally observable contact inputs."""

    PEER_EVIDENCE_RECEIVED = "PEER_EVIDENCE_RECEIVED"
    LOCAL_TIME_ADVANCED = "LOCAL_TIME_ADVANCED"


@dataclass(frozen=True, slots=True)
class ContactPolicy:
    """Receiver-local timing policy for one contact machine."""

    expected_interval: float
    degraded_after: float
    lost_after: float
    recovery_receipts: int = 2

    def __post_init__(self) -> None:
        expected_interval = validate_positive_real(
            self.expected_interval, name="contact expected_interval"
        )
        degraded_after = validate_positive_real(
            self.degraded_after, name="contact degraded_after"
        )
        lost_after = validate_positive_real(self.lost_after, name="contact lost_after")
        recovery_receipts = validate_positive_integer(
            self.recovery_receipts, name="contact recovery_receipts"
        )
        if degraded_after < expected_interval:
            raise ValueError("contact degraded_after must cover an expected interval")
        if lost_after <= degraded_after:
            raise ValueError("contact lost_after must exceed degraded_after")
        if recovery_receipts > MAX_EFSM_COUNTER:
            raise ValueError("contact recovery_receipts exceeds the bounded counter")


@dataclass(frozen=True, slots=True)
class ContactVariables:
    """Bounded typed extended state for one peer contact."""

    initialized_at: float
    last_event_at: float
    last_rx_at: float | None = None
    previous_rx_at: float | None = None
    consecutive_expected_misses: int = 0
    successful_rx_count: int = 0
    recovery_count: int = 0


@dataclass(frozen=True, slots=True)
class ContactConfiguration:
    state: ContactState
    variables: ContactVariables


@dataclass(frozen=True, slots=True)
class ContactEvent:
    kind: ContactEventKind
    timestamp: float
    peer_agent_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ContactEventKind):
            raise ValueError("contact event kind must be a ContactEventKind")
        validate_timestamp(self.timestamp, name="contact event timestamp")
        validate_positive_integer(self.peer_agent_id, name="contact peer_agent_id")


def initial_contact_configuration(timestamp: float = 0.0) -> ContactConfiguration:
    """Return the explicit initial configuration for a contact machine."""

    timestamp = validate_timestamp(timestamp, name="contact initial timestamp")
    return ContactConfiguration(
        state=ContactState.UNKNOWN,
        variables=ContactVariables(
            initialized_at=timestamp,
            last_event_at=timestamp,
        ),
    )


def _bounded_increment(value: int) -> int:
    return min(value + 1, MAX_EFSM_COUNTER)


def _contact_effect(
    state: ContactState, peer_agent_id: int
) -> tuple[RequestedEffect, ...]:
    effects = {
        ContactState.ACTIVE: AutonomyEffectKind.CONTACT_ACTIVE,
        ContactState.DEGRADED: AutonomyEffectKind.CONTACT_DEGRADED,
        ContactState.LOST: AutonomyEffectKind.CONTACT_LOST,
        ContactState.RECOVERING: AutonomyEffectKind.CONTACT_RECOVERING,
    }
    kind = effects.get(state)
    return () if kind is None else (RequestedEffect(kind, peer_agent_id),)


def transition_contact(
    configuration: ContactConfiguration,
    event: ContactEvent,
    policy: ContactPolicy,
) -> TransitionResult[ContactState, ContactVariables]:
    """Interpret receipt and elapsed time without consulting physical link truth."""

    timestamp = validate_timestamp(
        event.timestamp,
        previous=configuration.variables.last_event_at,
        name="contact transition timestamp",
    )
    previous_state = configuration.state
    variables = configuration.variables

    if event.kind is ContactEventKind.PEER_EVIDENCE_RECEIVED:
        next_state = ContactState.ACTIVE
        recovery_count = 0
        distinct_observation = (
            variables.last_rx_at is None or timestamp > variables.last_rx_at
        )
        if previous_state in {
            ContactState.DEGRADED,
            ContactState.LOST,
            ContactState.RECOVERING,
        }:
            previous_recovery_count = (
                variables.recovery_count
                if previous_state is ContactState.RECOVERING
                else 0
            )
            recovery_count = (
                _bounded_increment(previous_recovery_count)
                if distinct_observation
                else previous_recovery_count
            )
            if recovery_count < policy.recovery_receipts:
                next_state = ContactState.RECOVERING
        next_variables = ContactVariables(
            initialized_at=variables.initialized_at,
            last_event_at=timestamp,
            last_rx_at=timestamp,
            previous_rx_at=(
                variables.last_rx_at
                if distinct_observation
                else variables.previous_rx_at
            ),
            consecutive_expected_misses=0,
            successful_rx_count=_bounded_increment(variables.successful_rx_count),
            recovery_count=recovery_count,
        )
        guard = "valid first-hand peer evidence arrived at receiver-local time"
        reason = (
            f"receipt {next_variables.successful_rx_count}; recovery "
            f"{recovery_count}/{policy.recovery_receipts}"
        )
    else:
        reference = (
            variables.initialized_at
            if variables.last_rx_at is None
            else variables.last_rx_at
        )
        age = timestamp - reference
        missed = min(int(age // policy.expected_interval), MAX_EFSM_COUNTER)
        if age > policy.lost_after:
            next_state = ContactState.LOST
            guard = "receiver-local evidence age exceeded lost_after"
        elif age > policy.degraded_after:
            next_state = ContactState.DEGRADED
            guard = "receiver-local evidence age exceeded degraded_after"
        else:
            next_state = previous_state
            guard = "receiver-local evidence age remained within the current guard"
        next_variables = replace(
            variables,
            last_event_at=timestamp,
            consecutive_expected_misses=missed,
            recovery_count=(
                0
                if next_state in {ContactState.DEGRADED, ContactState.LOST}
                else variables.recovery_count
            ),
        )
        reason = (
            f"local evidence age={age:.6g}s; expected misses={missed}; "
            f"degraded>{policy.degraded_after:.6g}s; lost>{policy.lost_after:.6g}s"
        )

    effects = (
        _contact_effect(next_state, event.peer_agent_id)
        if next_state is not previous_state
        else ()
    )
    return TransitionResult(next_state, next_variables, effects, guard, reason)


class PeerAvailabilityEventKind(str, Enum):
    """Inputs that already exist in ``PeerStateStore`` semantics."""

    OBSERVATION_RECEIVED = "OBSERVATION_RECEIVED"
    SILENCE_OBSERVED = "SILENCE_OBSERVED"
    FAILURE_CERTIFICATE_RECEIVED = "FAILURE_CERTIFICATE_RECEIVED"


@dataclass(frozen=True, slots=True)
class PeerAvailabilityVariables:
    last_event_at: float
    last_observation_at: float | None = None
    observation_count: int = 0
    declaration_active: bool = False


@dataclass(frozen=True, slots=True)
class PeerAvailabilityConfiguration:
    state: PeerStatus
    variables: PeerAvailabilityVariables


@dataclass(frozen=True, slots=True)
class PeerAvailabilityEvent:
    kind: PeerAvailabilityEventKind
    timestamp: float
    peer_agent_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PeerAvailabilityEventKind):
            raise ValueError(
                "peer-availability event kind must be a PeerAvailabilityEventKind"
            )
        validate_timestamp(self.timestamp, name="peer-availability event timestamp")
        validate_positive_integer(
            self.peer_agent_id, name="peer-availability peer_agent_id"
        )


def initial_peer_availability_configuration(
    timestamp: float = 0.0,
) -> PeerAvailabilityConfiguration:
    """Preserve the existing never-heard-is-``SILENT`` store semantics."""

    timestamp = validate_timestamp(timestamp, name="peer-availability initial time")
    return PeerAvailabilityConfiguration(
        PeerStatus.SILENT,
        PeerAvailabilityVariables(last_event_at=timestamp),
    )


def transition_peer_availability(
    configuration: PeerAvailabilityConfiguration,
    event: PeerAvailabilityEvent,
) -> TransitionResult[PeerStatus, PeerAvailabilityVariables]:
    """Map the existing peer-status rules into an explicit pure relation."""

    timestamp = validate_timestamp(
        event.timestamp,
        previous=configuration.variables.last_event_at,
        name="peer-availability transition timestamp",
    )
    variables = configuration.variables
    if event.kind is PeerAvailabilityEventKind.OBSERVATION_RECEIVED:
        next_state = PeerStatus.HEARD
        next_variables = replace(
            variables,
            last_event_at=timestamp,
            last_observation_at=timestamp,
            observation_count=_bounded_increment(variables.observation_count),
            declaration_active=False,
        )
        guard = "first-hand peer observation received"
        reason = "first-hand evidence outranks silence or a remote certificate"
    elif event.kind is PeerAvailabilityEventKind.FAILURE_CERTIFICATE_RECEIVED:
        next_state = PeerStatus.DECLARED_FAILED
        next_variables = replace(
            variables,
            last_event_at=timestamp,
            declaration_active=True,
        )
        guard = "validated quorum-backed declaration accepted locally"
        reason = "availability belief changed; physical responsiveness did not"
    else:
        next_state = (
            PeerStatus.DECLARED_FAILED
            if configuration.state is PeerStatus.DECLARED_FAILED
            else PeerStatus.SILENT
        )
        next_variables = replace(variables, last_event_at=timestamp)
        guard = "local silence threshold crossed"
        reason = "silence is ambiguous and cannot establish physical failure"

    effect_kind = {
        PeerStatus.HEARD: AutonomyEffectKind.PEER_HEARD,
        PeerStatus.SILENT: AutonomyEffectKind.PEER_SILENT,
        PeerStatus.DECLARED_FAILED: AutonomyEffectKind.PEER_DECLARED_UNAVAILABLE,
    }[next_state]
    effects = (
        (RequestedEffect(effect_kind, event.peer_agent_id),)
        if next_state is not configuration.state
        else ()
    )
    return TransitionResult(next_state, next_variables, effects, guard, reason)


class TaskOwnershipEventKind(str, Enum):
    """Minimal alphabet derived from one claim store's local evidence view."""

    NO_VALID_CLAIM = "NO_VALID_CLAIM"
    LOCAL_VALID_CLAIM = "LOCAL_VALID_CLAIM"
    FRESH_PEER_VALID_CLAIM = "FRESH_PEER_VALID_CLAIM"
    STALE_PEER_VALID_CLAIM = "STALE_PEER_VALID_CLAIM"
    CONTEST_VISIBLE = "CONTEST_VISIBLE"
    COMPLETION_EVIDENCE = "COMPLETION_EVIDENCE"


@dataclass(frozen=True, slots=True)
class TaskOwnershipVariables:
    last_event_at: float
    known_owner_agent_id: int | None = None
    claim_id: ClaimId | None = None
    claim_freshness: ClaimFreshness | None = None
    contested: bool = False
    completion_known: bool = False


@dataclass(frozen=True, slots=True)
class TaskOwnershipConfiguration:
    state: TaskOwnershipState
    variables: TaskOwnershipVariables


@dataclass(frozen=True, slots=True)
class TaskOwnershipEvent:
    kind: TaskOwnershipEventKind
    timestamp: float
    task_id: int
    known_owner_agent_id: int | None = None
    claim_id: ClaimId | None = None
    claim_freshness: ClaimFreshness | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TaskOwnershipEventKind):
            raise ValueError("task event kind must be a TaskOwnershipEventKind")
        validate_timestamp(self.timestamp, name="task-ownership event timestamp")
        validate_positive_integer(self.task_id, name="task-ownership task_id")
        if self.known_owner_agent_id is not None:
            validate_positive_integer(
                self.known_owner_agent_id,
                name="task-ownership known_owner_agent_id",
            )
        if self.claim_freshness is not None and not isinstance(
            self.claim_freshness, ClaimFreshness
        ):
            raise ValueError("task claim_freshness must be a ClaimFreshness")


def initial_task_ownership_configuration(
    timestamp: float = 0.0,
) -> TaskOwnershipConfiguration:
    timestamp = validate_timestamp(timestamp, name="task-ownership initial time")
    return TaskOwnershipConfiguration(
        TaskOwnershipState.UNCLAIMED,
        TaskOwnershipVariables(last_event_at=timestamp),
    )


def task_ownership_event_from_view(
    observer_agent_id: int,
    view: TaskOwnershipView,
    timestamp: float,
) -> TaskOwnershipEvent:
    """Convert a receiver-local claim view into the formal EFSM alphabet."""

    validate_positive_integer(observer_agent_id, name="task observer_agent_id")
    kind_by_state = {
        TaskOwnershipState.UNCLAIMED: TaskOwnershipEventKind.NO_VALID_CLAIM,
        TaskOwnershipState.OWNED_BY_SELF: TaskOwnershipEventKind.LOCAL_VALID_CLAIM,
        TaskOwnershipState.CLAIMED_BY_PEER_FRESH: (
            TaskOwnershipEventKind.FRESH_PEER_VALID_CLAIM
        ),
        TaskOwnershipState.CLAIMED_BY_PEER_STALE: (
            TaskOwnershipEventKind.STALE_PEER_VALID_CLAIM
        ),
        TaskOwnershipState.CONTESTED: TaskOwnershipEventKind.CONTEST_VISIBLE,
        TaskOwnershipState.COMPLETE: TaskOwnershipEventKind.COMPLETION_EVIDENCE,
    }
    event = TaskOwnershipEvent(
        kind=kind_by_state[view.state],
        timestamp=timestamp,
        task_id=view.task_id,
        known_owner_agent_id=view.known_owner_agent_id,
        claim_id=view.claim_id,
        claim_freshness=view.claim_freshness,
    )
    if (
        event.kind is TaskOwnershipEventKind.LOCAL_VALID_CLAIM
        and event.known_owner_agent_id != observer_agent_id
    ):
        raise ValueError("OWNED_BY_SELF view must name the observing UAV")
    return event


def transition_task_ownership(
    configuration: TaskOwnershipConfiguration,
    event: TaskOwnershipEvent,
    observer_agent_id: int,
) -> TransitionResult[TaskOwnershipState, TaskOwnershipVariables]:
    """Formalize the six existing evidence-derived ownership states."""

    validate_positive_integer(observer_agent_id, name="task observer_agent_id")
    timestamp = validate_timestamp(
        event.timestamp,
        previous=configuration.variables.last_event_at,
        name="task-ownership transition timestamp",
    )
    state_by_event = {
        TaskOwnershipEventKind.NO_VALID_CLAIM: TaskOwnershipState.UNCLAIMED,
        TaskOwnershipEventKind.LOCAL_VALID_CLAIM: TaskOwnershipState.OWNED_BY_SELF,
        TaskOwnershipEventKind.FRESH_PEER_VALID_CLAIM: (
            TaskOwnershipState.CLAIMED_BY_PEER_FRESH
        ),
        TaskOwnershipEventKind.STALE_PEER_VALID_CLAIM: (
            TaskOwnershipState.CLAIMED_BY_PEER_STALE
        ),
        TaskOwnershipEventKind.CONTEST_VISIBLE: TaskOwnershipState.CONTESTED,
        TaskOwnershipEventKind.COMPLETION_EVIDENCE: TaskOwnershipState.COMPLETE,
    }
    observed_state = state_by_event[event.kind]
    if event.kind is TaskOwnershipEventKind.LOCAL_VALID_CLAIM:
        if event.known_owner_agent_id != observer_agent_id:
            raise ValueError("a local claim event must identify the observer")
    elif event.kind in {
        TaskOwnershipEventKind.FRESH_PEER_VALID_CLAIM,
        TaskOwnershipEventKind.STALE_PEER_VALID_CLAIM,
    }:
        if event.known_owner_agent_id in {None, observer_agent_id}:
            raise ValueError("a peer claim event must identify another UAV")

    if configuration.state is TaskOwnershipState.COMPLETE:
        next_state = TaskOwnershipState.COMPLETE
        next_variables = replace(
            configuration.variables,
            last_event_at=timestamp,
            completion_known=True,
        )
        guard = "terminal completion evidence was already accepted"
        reason = "COMPLETE is absorbing under all later local evidence"
    else:
        next_state = observed_state
        next_variables = TaskOwnershipVariables(
            last_event_at=timestamp,
            known_owner_agent_id=event.known_owner_agent_id,
            claim_id=event.claim_id,
            claim_freshness=event.claim_freshness,
            contested=next_state is TaskOwnershipState.CONTESTED,
            completion_known=next_state is TaskOwnershipState.COMPLETE,
        )
        guard = f"receiver-local claim evidence classified as {event.kind.value}"
        reason = "state is a projection of this replica's immutable evidence"

    effects: list[RequestedEffect] = []
    if next_state is TaskOwnershipState.CONTESTED:
        effects.append(
            RequestedEffect(
                AutonomyEffectKind.REQUEST_RECONCILIATION, task_id=event.task_id
            )
        )
    if (
        configuration.state is TaskOwnershipState.OWNED_BY_SELF
        and next_state is not TaskOwnershipState.OWNED_BY_SELF
    ):
        effects.append(
            RequestedEffect(AutonomyEffectKind.STAND_DOWN_TASK, task_id=event.task_id)
        )
    if (
        next_state is TaskOwnershipState.UNCLAIMED
        and configuration.state is not TaskOwnershipState.UNCLAIMED
    ):
        effects.append(
            RequestedEffect(
                AutonomyEffectKind.REQUEST_TASK_SELECTION,
                task_id=event.task_id,
            )
        )
    return TransitionResult(
        next_state,
        next_variables,
        tuple(effects),
        guard,
        reason,
    )


class CoordinationMode(str, Enum):
    """How strongly one UAV may rely on remote coordination evidence."""

    COOPERATIVE = "COOPERATIVE"
    DEGRADED = "DEGRADED"
    LOCAL_AUTONOMY = "LOCAL_AUTONOMY"
    RECONCILING = "RECONCILING"


class CoordinationEventKind(str, Enum):
    LOCAL_EVIDENCE_UPDATED = "LOCAL_EVIDENCE_UPDATED"


@dataclass(frozen=True, slots=True)
class CoordinationPolicy:
    degradation_grace: float
    local_autonomy_grace: float
    recovery_stable_for: float

    def __post_init__(self) -> None:
        validate_positive_real(
            self.degradation_grace, name="coordination degradation_grace"
        )
        validate_positive_real(
            self.local_autonomy_grace,
            name="coordination local_autonomy_grace",
        )
        validate_positive_real(
            self.recovery_stable_for,
            name="coordination recovery_stable_for",
        )


@dataclass(frozen=True, slots=True)
class CoordinationVariables:
    last_event_at: float
    state_entered_at: float
    degraded_since: float | None = None
    unavailable_since: float | None = None
    stable_since: float | None = None
    known_peer_count: int = 0
    active_peer_count: int = 0
    recovering_peer_count: int = 0
    degraded_peer_count: int = 0
    lost_peer_count: int = 0
    unresolved_task_contests: int = 0
    remote_evidence_count: int = 0


@dataclass(frozen=True, slots=True)
class CoordinationConfiguration:
    state: CoordinationMode
    variables: CoordinationVariables


@dataclass(frozen=True, slots=True)
class CoordinationEvidenceEvent:
    kind: CoordinationEventKind
    timestamp: float
    contact_states: tuple[ContactState, ...]
    unresolved_task_contests: int
    new_remote_evidence: bool

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CoordinationEventKind):
            raise ValueError("coordination event kind must be a CoordinationEventKind")
        validate_timestamp(self.timestamp, name="coordination event timestamp")
        if any(not isinstance(state, ContactState) for state in self.contact_states):
            raise ValueError("coordination contacts must contain ContactState values")
        if (
            not isinstance(self.unresolved_task_contests, int)
            or isinstance(self.unresolved_task_contests, bool)
            or self.unresolved_task_contests < 0
        ):
            raise ValueError("unresolved_task_contests must be non-negative")
        if not isinstance(self.new_remote_evidence, bool):
            raise ValueError("new_remote_evidence must be a boolean")


def initial_coordination_configuration(
    timestamp: float = 0.0,
) -> CoordinationConfiguration:
    timestamp = validate_timestamp(timestamp, name="coordination initial time")
    return CoordinationConfiguration(
        CoordinationMode.COOPERATIVE,
        CoordinationVariables(
            last_event_at=timestamp,
            state_entered_at=timestamp,
            stable_since=timestamp,
        ),
    )


def _elapsed(timestamp: float, since: float | None) -> float:
    return 0.0 if since is None else timestamp - since


def _coordination_effect(
    state: CoordinationMode,
) -> tuple[RequestedEffect, ...]:
    kind = {
        CoordinationMode.COOPERATIVE: AutonomyEffectKind.RESUME_COOPERATION,
        CoordinationMode.DEGRADED: AutonomyEffectKind.REDUCE_REMOTE_DEPENDENCE,
        CoordinationMode.LOCAL_AUTONOMY: AutonomyEffectKind.ENTER_LOCAL_AUTONOMY,
        CoordinationMode.RECONCILING: AutonomyEffectKind.REQUEST_RECONCILIATION,
    }[state]
    return (RequestedEffect(kind),)


def transition_coordination_mode(
    configuration: CoordinationConfiguration,
    event: CoordinationEvidenceEvent,
    policy: CoordinationPolicy,
) -> TransitionResult[CoordinationMode, CoordinationVariables]:
    """Update one UAV's epistemic posture from its own composed machine state."""

    timestamp = validate_timestamp(
        event.timestamp,
        previous=configuration.variables.last_event_at,
        name="coordination transition timestamp",
    )
    counts = {state: event.contact_states.count(state) for state in ContactState}
    peer_count = len(event.contact_states)
    active_count = counts[ContactState.ACTIVE]
    recovering_count = counts[ContactState.RECOVERING]
    degraded_count = counts[ContactState.DEGRADED] + counts[ContactState.UNKNOWN]
    lost_count = counts[ContactState.LOST]
    healthy = peer_count == 0 or active_count == peer_count
    severe = peer_count > 0 and (active_count == 0 or lost_count * 2 > peer_count)
    degraded = not healthy

    previous_variables = configuration.variables
    degraded_since = (
        None
        if not degraded
        else previous_variables.degraded_since
        if previous_variables.degraded_since is not None
        else timestamp
    )
    unavailable_since = (
        None
        if not severe
        else previous_variables.unavailable_since
        if previous_variables.unavailable_since is not None
        else timestamp
    )
    stable_since = (
        None
        if not healthy
        else previous_variables.stable_since
        if previous_variables.stable_since is not None
        else timestamp
    )
    next_variables = CoordinationVariables(
        last_event_at=timestamp,
        state_entered_at=previous_variables.state_entered_at,
        degraded_since=degraded_since,
        unavailable_since=unavailable_since,
        stable_since=stable_since,
        known_peer_count=peer_count,
        active_peer_count=active_count,
        recovering_peer_count=recovering_count,
        degraded_peer_count=degraded_count,
        lost_peer_count=lost_count,
        unresolved_task_contests=event.unresolved_task_contests,
        remote_evidence_count=(
            _bounded_increment(previous_variables.remote_evidence_count)
            if event.new_remote_evidence
            else previous_variables.remote_evidence_count
        ),
    )

    previous_state = configuration.state
    next_state = previous_state
    guard = "no coordination-mode guard crossed"
    if event.unresolved_task_contests > 0:
        next_state = CoordinationMode.RECONCILING
        guard = "locally visible task contest requires deterministic reconciliation"
    elif previous_state is CoordinationMode.COOPERATIVE:
        if degraded and _elapsed(timestamp, degraded_since) >= policy.degradation_grace:
            next_state = CoordinationMode.DEGRADED
            guard = "local contact deterioration persisted through grace interval"
    elif previous_state is CoordinationMode.DEGRADED:
        if (
            severe
            and _elapsed(timestamp, unavailable_since) >= policy.local_autonomy_grace
        ):
            next_state = CoordinationMode.LOCAL_AUTONOMY
            guard = "useful direct peer evidence remained locally insufficient"
        elif (
            healthy and _elapsed(timestamp, stable_since) >= policy.recovery_stable_for
        ):
            next_state = CoordinationMode.COOPERATIVE
            guard = "all local contacts remained active through stability interval"
    elif previous_state is CoordinationMode.LOCAL_AUTONOMY:
        if event.new_remote_evidence:
            next_state = CoordinationMode.RECONCILING
            guard = "previously unavailable remote evidence arrived locally"
    else:
        if (
            severe
            and not event.new_remote_evidence
            and _elapsed(
                timestamp,
                max(
                    previous_variables.state_entered_at,
                    unavailable_since if unavailable_since is not None else timestamp,
                ),
            )
            >= policy.local_autonomy_grace
        ):
            next_state = CoordinationMode.LOCAL_AUTONOMY
            guard = "contact deteriorated again during reconciliation"
        elif (
            healthy
            and timestamp > previous_variables.state_entered_at
            and _elapsed(
                timestamp,
                max(
                    previous_variables.state_entered_at,
                    stable_since if stable_since is not None else timestamp,
                ),
            )
            >= policy.recovery_stable_for
        ):
            next_state = CoordinationMode.COOPERATIVE
            guard = "contacts stabilized and no local task contests remain"
        elif (
            degraded
            and not severe
            and _elapsed(
                timestamp,
                max(
                    previous_variables.state_entered_at,
                    degraded_since if degraded_since is not None else timestamp,
                ),
            )
            >= policy.degradation_grace
        ):
            next_state = CoordinationMode.DEGRADED
            guard = "reconciliation settled with partially degraded contact"

    if next_state is not previous_state:
        next_variables = replace(next_variables, state_entered_at=timestamp)

    reason = (
        f"local contacts active={active_count}, recovering={recovering_count}, "
        f"degraded_or_unknown={degraded_count}, lost={lost_count}, "
        f"contests={event.unresolved_task_contests}"
    )
    effects = (
        _coordination_effect(next_state) if next_state is not previous_state else ()
    )
    return TransitionResult(next_state, next_variables, effects, guard, reason)


CONTACT_MACHINE = MachineDefinition(
    name=MachineName.CONTACT,
    states=tuple(state.value for state in ContactState),
    initial_state=ContactState.UNKNOWN.value,
    rules=(
        TransitionRule(
            "UNKNOWN",
            "PEER_EVIDENCE_RECEIVED",
            "receipt",
            "ACTIVE",
            ("CONTACT_ACTIVE",),
        ),
        TransitionRule(
            "ACTIVE",
            "LOCAL_TIME_ADVANCED",
            "age > degraded_after",
            "DEGRADED",
            ("CONTACT_DEGRADED",),
        ),
        TransitionRule(
            "DEGRADED",
            "LOCAL_TIME_ADVANCED",
            "age > lost_after",
            "LOST",
            ("CONTACT_LOST",),
        ),
        TransitionRule(
            "LOST",
            "PEER_EVIDENCE_RECEIVED",
            "first recovery receipt",
            "RECOVERING",
            ("CONTACT_RECOVERING",),
        ),
        TransitionRule(
            "DEGRADED",
            "PEER_EVIDENCE_RECEIVED",
            "first recovery receipt",
            "RECOVERING",
            ("CONTACT_RECOVERING",),
        ),
        TransitionRule(
            "RECOVERING",
            "PEER_EVIDENCE_RECEIVED",
            "recovery_count >= required",
            "ACTIVE",
            ("CONTACT_ACTIVE",),
        ),
        TransitionRule(
            "RECOVERING",
            "LOCAL_TIME_ADVANCED",
            "age > degraded_after",
            "DEGRADED",
            ("CONTACT_DEGRADED",),
        ),
        TransitionRule(
            "*", "LOCAL_TIME_ADVANCED", "age > lost_after", "LOST", ("CONTACT_LOST",)
        ),
    ),
)

PEER_AVAILABILITY_MACHINE = MachineDefinition(
    name=MachineName.PEER_AVAILABILITY,
    states=tuple(state.value for state in PeerStatus),
    initial_state=PeerStatus.SILENT.value,
    rules=(
        TransitionRule(
            "SILENT",
            "OBSERVATION_RECEIVED",
            "first-hand receipt",
            "HEARD",
            ("PEER_HEARD",),
        ),
        TransitionRule(
            "HEARD",
            "SILENCE_OBSERVED",
            "silence threshold crossed",
            "SILENT",
            ("PEER_SILENT",),
        ),
        TransitionRule(
            "*",
            "FAILURE_CERTIFICATE_RECEIVED",
            "valid quorum certificate",
            "DECLARED_FAILED",
            ("PEER_DECLARED_UNAVAILABLE",),
        ),
        TransitionRule(
            "DECLARED_FAILED",
            "OBSERVATION_RECEIVED",
            "new first-hand receipt",
            "HEARD",
            ("PEER_HEARD",),
        ),
    ),
)

TASK_OWNERSHIP_MACHINE = MachineDefinition(
    name=MachineName.TASK_OWNERSHIP,
    states=tuple(state.value for state in TaskOwnershipState),
    initial_state=TaskOwnershipState.UNCLAIMED.value,
    rules=(
        TransitionRule(
            "NONCOMPLETE",
            "NO_VALID_CLAIM",
            "no lease-valid claim",
            "UNCLAIMED",
            ("REQUEST_TASK_SELECTION",),
        ),
        TransitionRule(
            "NONCOMPLETE",
            "LOCAL_VALID_CLAIM",
            "selected owner is self",
            "OWNED_BY_SELF",
        ),
        TransitionRule(
            "NONCOMPLETE",
            "FRESH_PEER_VALID_CLAIM",
            "fresh selected peer claim",
            "CLAIMED_BY_PEER_FRESH",
        ),
        TransitionRule(
            "NONCOMPLETE",
            "STALE_PEER_VALID_CLAIM",
            "stale but lease-valid peer claim",
            "CLAIMED_BY_PEER_STALE",
        ),
        TransitionRule(
            "NONCOMPLETE",
            "CONTEST_VISIBLE",
            "multiple lease-valid owners",
            "CONTESTED",
            ("REQUEST_RECONCILIATION",),
        ),
        TransitionRule(
            "NONCOMPLETE",
            "COMPLETION_EVIDENCE",
            "valid completion evidence",
            "COMPLETE",
        ),
        TransitionRule("COMPLETE", "*", "completion already known", "COMPLETE"),
    ),
)

COORDINATION_MODE_MACHINE = MachineDefinition(
    name=MachineName.COORDINATION_MODE,
    states=tuple(state.value for state in CoordinationMode),
    initial_state=CoordinationMode.COOPERATIVE.value,
    rules=(
        TransitionRule(
            "COOPERATIVE",
            "LOCAL_EVIDENCE_UPDATED",
            "sustained contact deterioration",
            "DEGRADED",
            ("REDUCE_REMOTE_DEPENDENCE",),
        ),
        TransitionRule(
            "DEGRADED",
            "LOCAL_EVIDENCE_UPDATED",
            "sustained insufficient peer evidence",
            "LOCAL_AUTONOMY",
            ("ENTER_LOCAL_AUTONOMY",),
        ),
        TransitionRule(
            "LOCAL_AUTONOMY",
            "LOCAL_EVIDENCE_UPDATED",
            "new remote evidence",
            "RECONCILING",
            ("REQUEST_RECONCILIATION",),
        ),
        TransitionRule(
            "*",
            "LOCAL_EVIDENCE_UPDATED",
            "visible task contest",
            "RECONCILING",
            ("REQUEST_RECONCILIATION",),
        ),
        TransitionRule(
            "RECONCILING",
            "LOCAL_EVIDENCE_UPDATED",
            "stable contact and no contests",
            "COOPERATIVE",
            ("RESUME_COOPERATION",),
        ),
        TransitionRule(
            "RECONCILING",
            "LOCAL_EVIDENCE_UPDATED",
            "contact deteriorates again",
            "LOCAL_AUTONOMY",
            ("ENTER_LOCAL_AUTONOMY",),
        ),
        TransitionRule(
            "DEGRADED",
            "LOCAL_EVIDENCE_UPDATED",
            "stable active contact",
            "COOPERATIVE",
            ("RESUME_COOPERATION",),
        ),
    ),
)


def machine_definitions() -> tuple[MachineDefinition, ...]:
    """Return every canonical machine definition in composition order."""

    return (
        CONTACT_MACHINE,
        PEER_AVAILABILITY_MACHINE,
        TASK_OWNERSHIP_MACHINE,
        COORDINATION_MODE_MACHINE,
    )


def render_transition_tables() -> str:
    """Render canonical definitions as deterministic Markdown tables."""

    sections: list[str] = []
    for definition in machine_definitions():
        sections.extend(
            [
                f"### {definition.name.value}",
                "",
                f"Initial state: `{definition.initial_state}`",
                "",
                "| From | Event | Guard | To | Requested effects |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        sections.extend(
            "| "
            + " | ".join(
                (
                    f"`{rule.source}`",
                    f"`{rule.event}`",
                    rule.guard,
                    f"`{rule.target}`",
                    ", ".join(f"`{effect}`" for effect in rule.effects) or "—",
                )
            )
            + " |"
            for rule in definition.rules
        )
        sections.append("")
    return "\n".join(sections).rstrip() + "\n"


class LocalAutonomyKernel:
    """Compose per-peer, per-task, and per-UAV machines for one receiver."""

    def __init__(
        self,
        owner_agent_id: int,
        peer_agent_ids: Iterable[int],
        task_ids: Iterable[int],
        *,
        contact_policy: ContactPolicy,
        coordination_policy: CoordinationPolicy,
        initial_timestamp: float = 0.0,
    ) -> None:
        self._owner_agent_id = validate_positive_integer(
            owner_agent_id, name="autonomy owner_agent_id"
        )
        peers = tuple(sorted(peer_agent_ids))
        tasks = tuple(sorted(task_ids))
        if len(set(peers)) != len(peers) or owner_agent_id in peers:
            raise ValueError("autonomy peer IDs must be unique and exclude self")
        if len(set(tasks)) != len(tasks) or not tasks:
            raise ValueError("autonomy task IDs must be unique and non-empty")
        for peer_agent_id in peers:
            validate_positive_integer(peer_agent_id, name="autonomy peer_agent_id")
        for task_id in tasks:
            validate_positive_integer(task_id, name="autonomy task_id")
        initial_timestamp = validate_timestamp(
            initial_timestamp, name="autonomy initial timestamp"
        )
        self._peer_agent_ids = peers
        self._task_ids = tasks
        self._contact_policy = contact_policy
        self._coordination_policy = coordination_policy
        self._contacts = {
            peer_agent_id: initial_contact_configuration(initial_timestamp)
            for peer_agent_id in peers
        }
        self._peer_availability = {
            peer_agent_id: initial_peer_availability_configuration(initial_timestamp)
            for peer_agent_id in peers
        }
        self._tasks = {
            task_id: initial_task_ownership_configuration(initial_timestamp)
            for task_id in tasks
        }
        self._coordination = initial_coordination_configuration(initial_timestamp)
        self._transition_records: list[TransitionRecord] = []
        self._new_remote_evidence = False

    @property
    def owner_agent_id(self) -> int:
        return self._owner_agent_id

    @property
    def peer_agent_ids(self) -> tuple[int, ...]:
        return self._peer_agent_ids

    @property
    def task_ids(self) -> tuple[int, ...]:
        return self._task_ids

    @property
    def coordination_mode(self) -> CoordinationMode:
        return self._coordination.state

    @property
    def coordination_configuration(self) -> CoordinationConfiguration:
        return self._coordination

    @property
    def transition_records(self) -> tuple[TransitionRecord, ...]:
        return tuple(self._transition_records)

    def contact_configuration_for(self, peer_agent_id: int) -> ContactConfiguration:
        self._require_peer(peer_agent_id)
        return self._contacts[peer_agent_id]

    def peer_availability_for(self, peer_agent_id: int) -> PeerStatus:
        self._require_peer(peer_agent_id)
        return self._peer_availability[peer_agent_id].state

    def task_configuration_for(self, task_id: int) -> TaskOwnershipConfiguration:
        self._require_task(task_id)
        return self._tasks[task_id]

    def advance_time(self, timestamp: float) -> None:
        for peer_agent_id in self._peer_agent_ids:
            event = ContactEvent(
                ContactEventKind.LOCAL_TIME_ADVANCED,
                timestamp,
                peer_agent_id,
            )
            self._apply_contact(event)

    def receive_peer_evidence(self, peer_agent_id: int, timestamp: float) -> None:
        self._require_peer(peer_agent_id)
        self.receive_forwarded_evidence(peer_agent_id, timestamp)
        self._apply_peer_availability(
            PeerAvailabilityEvent(
                PeerAvailabilityEventKind.OBSERVATION_RECEIVED,
                timestamp,
                peer_agent_id,
            )
        )

    def receive_forwarded_evidence(
        self, forwarder_agent_id: int, timestamp: float
    ) -> None:
        """Observe a successful physical hop without changing heartbeat state.

        The direct forwarder, rather than an immutable message's logical origin,
        is the contact subject.  Only a heartbeat calls ``receive_peer_evidence``
        and updates ``PeerAvailabilityEFSM``.
        """

        self._require_peer(forwarder_agent_id)
        self._apply_contact(
            ContactEvent(
                ContactEventKind.PEER_EVIDENCE_RECEIVED,
                timestamp,
                forwarder_agent_id,
            )
        )
        self._new_remote_evidence = True

    def synchronize_peer_status(
        self,
        peer_agent_id: int,
        status: PeerStatus,
        timestamp: float,
    ) -> None:
        """Apply a typed event only when the existing local store status changes."""

        self._require_peer(peer_agent_id)
        current = self._peer_availability[peer_agent_id].state
        if status is current:
            return
        kind = {
            PeerStatus.HEARD: PeerAvailabilityEventKind.OBSERVATION_RECEIVED,
            PeerStatus.SILENT: PeerAvailabilityEventKind.SILENCE_OBSERVED,
            PeerStatus.DECLARED_FAILED: (
                PeerAvailabilityEventKind.FAILURE_CERTIFICATE_RECEIVED
            ),
        }[status]
        self._apply_peer_availability(
            PeerAvailabilityEvent(kind, timestamp, peer_agent_id)
        )

    def observe_task_view(self, view: TaskOwnershipView, timestamp: float) -> None:
        self._require_task(view.task_id)
        event = task_ownership_event_from_view(
            self._owner_agent_id,
            view,
            timestamp,
        )
        previous = self._tasks[view.task_id]
        result = transition_task_ownership(
            previous,
            event,
            self._owner_agent_id,
        )
        self._tasks[view.task_id] = TaskOwnershipConfiguration(
            result.next_state,
            result.next_variables,
        )
        self._record_change(
            MachineName.TASK_OWNERSHIP,
            previous.state,
            event.kind,
            result,
            timestamp,
            task_id=view.task_id,
        )

    def note_remote_evidence(self) -> None:
        """Record a delivered immutable message for the next local composition step."""

        self._new_remote_evidence = True

    def evaluate_coordination(self, timestamp: float) -> None:
        event = CoordinationEvidenceEvent(
            kind=CoordinationEventKind.LOCAL_EVIDENCE_UPDATED,
            timestamp=timestamp,
            contact_states=tuple(
                self._contacts[peer_agent_id].state
                for peer_agent_id in self._peer_agent_ids
            ),
            unresolved_task_contests=sum(
                configuration.state is TaskOwnershipState.CONTESTED
                for configuration in self._tasks.values()
            ),
            new_remote_evidence=self._new_remote_evidence,
        )
        previous = self._coordination
        result = transition_coordination_mode(
            previous,
            event,
            self._coordination_policy,
        )
        self._coordination = CoordinationConfiguration(
            result.next_state,
            result.next_variables,
        )
        self._record_change(
            MachineName.COORDINATION_MODE,
            previous.state,
            event.kind,
            result,
            timestamp,
        )
        self._new_remote_evidence = False

    def _apply_contact(self, event: ContactEvent) -> None:
        previous = self._contacts[event.peer_agent_id]
        result = transition_contact(previous, event, self._contact_policy)
        self._contacts[event.peer_agent_id] = ContactConfiguration(
            result.next_state,
            result.next_variables,
        )
        self._record_change(
            MachineName.CONTACT,
            previous.state,
            event.kind,
            result,
            event.timestamp,
            peer_agent_id=event.peer_agent_id,
        )

    def _apply_peer_availability(self, event: PeerAvailabilityEvent) -> None:
        previous = self._peer_availability[event.peer_agent_id]
        result = transition_peer_availability(previous, event)
        self._peer_availability[event.peer_agent_id] = PeerAvailabilityConfiguration(
            result.next_state, result.next_variables
        )
        self._record_change(
            MachineName.PEER_AVAILABILITY,
            previous.state,
            event.kind,
            result,
            event.timestamp,
            peer_agent_id=event.peer_agent_id,
        )

    def _record_change(
        self,
        machine: MachineName,
        previous_state: _RecordStateT,
        event_kind: Enum,
        result: TransitionResult[_RecordStateT, _RecordVariablesT],
        timestamp: float,
        *,
        peer_agent_id: int | None = None,
        task_id: int | None = None,
    ) -> None:
        if result.next_state is previous_state:
            return
        self._transition_records.append(
            TransitionRecord(
                timestamp=timestamp,
                observer_agent_id=self._owner_agent_id,
                sequence=len(self._transition_records),
                machine=machine,
                previous_state=str(previous_state.value),
                event=str(event_kind.value),
                next_state=str(result.next_state.value),
                guard=result.guard,
                reason=result.reason,
                effects=tuple(effect.kind.value for effect in result.effects),
                peer_agent_id=peer_agent_id,
                task_id=task_id,
            )
        )

    def _require_peer(self, peer_agent_id: int) -> None:
        if peer_agent_id not in self._contacts:
            raise KeyError(
                f"UAV {peer_agent_id} is not a peer of UAV {self._owner_agent_id}"
            )

    def _require_task(self, task_id: int) -> None:
        if task_id not in self._tasks:
            raise KeyError(f"Task {task_id} is not known to UAV {self._owner_agent_id}")


__all__ = [
    "AutonomyEffectKind",
    "CONTACT_MACHINE",
    "COORDINATION_MODE_MACHINE",
    "ContactConfiguration",
    "ContactEvent",
    "ContactEventKind",
    "ContactPolicy",
    "ContactState",
    "ContactVariables",
    "CoordinationConfiguration",
    "CoordinationEvidenceEvent",
    "CoordinationEventKind",
    "CoordinationMode",
    "CoordinationPolicy",
    "CoordinationVariables",
    "LocalAutonomyKernel",
    "MachineDefinition",
    "MachineName",
    "PEER_AVAILABILITY_MACHINE",
    "PeerAvailabilityConfiguration",
    "PeerAvailabilityEvent",
    "PeerAvailabilityEventKind",
    "PeerAvailabilityVariables",
    "RequestedEffect",
    "TASK_OWNERSHIP_MACHINE",
    "TaskOwnershipConfiguration",
    "TaskOwnershipEvent",
    "TaskOwnershipEventKind",
    "TaskOwnershipVariables",
    "TransitionRecord",
    "TransitionResult",
    "TransitionRule",
    "initial_contact_configuration",
    "initial_coordination_configuration",
    "initial_peer_availability_configuration",
    "initial_task_ownership_configuration",
    "machine_definitions",
    "render_transition_tables",
    "task_ownership_event_from_view",
    "transition_contact",
    "transition_coordination_mode",
    "transition_peer_availability",
    "transition_task_ownership",
]
