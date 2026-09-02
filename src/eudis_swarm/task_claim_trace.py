"""Capture receiver-local task ownership without observing authoritative mission state.

The immutable trace round-trips through JSON and reads claim stores only through
their pure snapshot boundary, so recording can never participate in a decision.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from math import isclose
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .simulation_events import TaskClaimEvent, TaskClaimEventKind
from .task_claims import ClaimId, TaskClaimStore, TaskOwnershipView
from .validation import (
    validate_nonnegative_real,
    validate_positive_integer,
    validate_positive_real,
    validate_timestamp,
)

TASK_CLAIM_TRACE_SCHEMA_VERSION = 1
TASK_OWNERSHIP_STATES = frozenset(
    {
        "UNCLAIMED",
        "OWNED_BY_SELF",
        "CLAIMED_BY_PEER_FRESH",
        "CLAIMED_BY_PEER_STALE",
        "CONTESTED",
        "COMPLETE",
    }
)
CLAIM_FRESHNESS_STATES = frozenset({"FRESH", "STALE", "EXPIRED"})


def canonical_claim_id(claim_id: ClaimId) -> str:
    """Render a structural claim ID in stable ``task:owner:epoch`` form."""

    task_id, owner_agent_id, epoch = claim_id
    for name, value in (
        ("task", task_id),
        ("owner", owner_agent_id),
        ("epoch", epoch),
    ):
        validate_positive_integer(value, name=f"claim ID {name}")
    return f"{task_id}:{owner_agent_id}:{epoch}"


def _claim_parts(value: str, *, name: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    pieces = value.split(":")
    if len(pieces) != 3 or any(not piece.isdecimal() for piece in pieces):
        raise ValueError(f"{name} must use task:owner:epoch form")
    parts = (int(pieces[0]), int(pieces[1]), int(pieces[2]))
    if any(part < 1 for part in parts) or value != ":".join(map(str, parts)):
        raise ValueError(f"{name} must contain canonical positive integers")
    return parts


@dataclass(frozen=True, slots=True)
class TaskClaimTraceMetadata:
    """Describe one ownership scenario and its fixed local-store policy."""

    scenario: str
    agent_ids: tuple[int, ...]
    task_ids: tuple[int, ...]
    freshness_timeout: float
    lease_timeout: float

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, str) or not self.scenario.strip():
            raise ValueError("trace scenario must be a non-empty string")
        for name, identifiers in (
            ("agent IDs", self.agent_ids),
            ("task IDs", self.task_ids),
        ):
            if not identifiers:
                raise ValueError(f"trace {name} must not be empty")
            for identifier in identifiers:
                validate_positive_integer(identifier, name=f"trace {name}")
            if identifiers != tuple(sorted(set(identifiers))):
                raise ValueError(f"trace {name} must be unique and sorted")
        freshness = validate_positive_real(
            self.freshness_timeout,
            name="trace freshness_timeout",
        )
        lease = validate_positive_real(self.lease_timeout, name="trace lease_timeout")
        if freshness >= lease:
            raise ValueError("trace freshness_timeout must be below lease_timeout")


@dataclass(frozen=True, slots=True)
class TaskClaimTraceLink:
    """Store one canonical undirected active link."""

    source_agent_id: int
    destination_agent_id: int

    def __post_init__(self) -> None:
        validate_positive_integer(self.source_agent_id, name="link source_agent_id")
        validate_positive_integer(
            self.destination_agent_id,
            name="link destination_agent_id",
        )
        if self.source_agent_id >= self.destination_agent_id:
            raise ValueError("trace links must use ascending endpoints")


@dataclass(frozen=True, slots=True)
class TaskClaimTraceKnownClaim:
    """Describe one immutable claim exactly as one receiver currently knows it."""

    claim_id: str
    owner_agent_id: int
    epoch: int
    created_at: float
    received_at: float
    age: float
    freshness: str
    current: bool
    released: bool
    reconciled_loser: bool
    valid: bool

    def __post_init__(self) -> None:
        task_id, owner_id, epoch = _claim_parts(
            self.claim_id,
            name="known-claim claim_id",
        )
        validate_positive_integer(task_id, name="known-claim task_id")
        if (owner_id, epoch) != (self.owner_agent_id, self.epoch):
            raise ValueError("known-claim ID disagrees with its owner or epoch")
        created = validate_timestamp(self.created_at, name="known-claim created_at")
        validate_timestamp(
            self.received_at,
            previous=created,
            name="known-claim received_at",
        )
        validate_nonnegative_real(self.age, name="known-claim age")
        if self.freshness not in CLAIM_FRESHNESS_STATES:
            raise ValueError("known-claim freshness is not recognized")
        flags = (
            self.current,
            self.released,
            self.reconciled_loser,
            self.valid,
        )
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValueError("known-claim flags must be boolean")
        expected_valid = (
            self.current
            and not self.released
            and not self.reconciled_loser
            and self.freshness != "EXPIRED"
        )
        if self.valid != expected_valid:
            raise ValueError("known-claim valid flag disagrees with its evidence")


@dataclass(frozen=True, slots=True)
class TaskClaimTraceCompletion:
    """Identify terminal completion evidence accepted by one receiver."""

    claim_id: str
    owner_agent_id: int
    epoch: int
    created_at: float

    def __post_init__(self) -> None:
        _, owner_id, epoch = _claim_parts(self.claim_id, name="completion claim_id")
        if (owner_id, epoch) != (self.owner_agent_id, self.epoch):
            raise ValueError("completion ID disagrees with its owner or epoch")
        validate_timestamp(self.created_at, name="completion created_at")


@dataclass(frozen=True, slots=True)
class TaskClaimTraceTaskView:
    """Expose one receiver's complete local interpretation of one task."""

    task_id: int
    state: str
    known_owner_agent_id: int | None
    claim_id: str | None
    epoch: int | None
    claim_age: float | None
    claim_freshness: str | None
    contested: bool
    reconciliation_winner_agent_id: int | None
    reconciliation_winner_claim_id: str | None
    released: bool
    released_claim_ids: tuple[str, ...]
    completion: TaskClaimTraceCompletion | None
    complete: bool
    known_claims: tuple[TaskClaimTraceKnownClaim, ...]

    def __post_init__(self) -> None:
        validate_positive_integer(self.task_id, name="task-view task_id")
        if self.state not in TASK_OWNERSHIP_STATES:
            raise ValueError("task-view state is outside the six-state vocabulary")
        if self.contested != (self.state == "CONTESTED"):
            raise ValueError("task-view contested flag disagrees with its state")
        if self.complete != (self.state == "COMPLETE"):
            raise ValueError("task-view complete flag disagrees with its state")
        if (self.completion is None) == self.complete:
            raise ValueError("task-view completion must exist exactly for COMPLETE")

        # stable claim ordering lets consumers compare frames without hash ordering.
        known_order = tuple(
            (claim.owner_agent_id, claim.epoch) for claim in self.known_claims
        )
        if known_order != tuple(sorted(set(known_order))):
            raise ValueError(
                "known claims must be unique and sorted by owner and epoch"
            )
        known_ids = frozenset(claim.claim_id for claim in self.known_claims)
        selected = (
            None
            if self.claim_id is None
            else _claim_parts(
                self.claim_id,
                name="task-view claim_id",
            )
        )
        selected_fields = (
            self.known_owner_agent_id,
            self.epoch,
            self.claim_age,
            self.claim_freshness,
        )
        if selected is None and any(value is not None for value in selected_fields):
            raise ValueError("selected claim fields must be absent together")
        if selected is not None:
            if any(value is None for value in selected_fields):
                raise ValueError("selected claim fields must be present together")
            if selected != (self.task_id, self.known_owner_agent_id, self.epoch):
                raise ValueError("selected claim fields disagree")
            if self.claim_id not in known_ids:
                raise ValueError("selected claim must be locally known")
            claim_age = self.claim_age
            assert claim_age is not None
            validate_nonnegative_real(claim_age, name="task-view claim_age")
            if self.claim_freshness not in CLAIM_FRESHNESS_STATES:
                raise ValueError("selected claim freshness is not recognized")

        released_parts = tuple(
            _claim_parts(claim_id, name="released claim ID")
            for claim_id in self.released_claim_ids
        )
        if any(parts[0] != self.task_id for parts in released_parts):
            raise ValueError("released claims must belong to the task view")
        if released_parts != tuple(sorted(set(released_parts))):
            raise ValueError("released claim IDs must be unique and sorted")
        if self.released != bool(self.released_claim_ids):
            raise ValueError("released flag must match released claim IDs")
        if not set(self.released_claim_ids).issubset(known_ids):
            raise ValueError("released claims must be locally known")

        winner = (
            None
            if self.reconciliation_winner_claim_id is None
            else _claim_parts(
                self.reconciliation_winner_claim_id,
                name="reconciliation winner claim ID",
            )
        )
        if (winner is None) != (self.reconciliation_winner_agent_id is None):
            raise ValueError("reconciliation winner fields must be present together")
        if winner is not None and winner[:2] != (
            self.task_id,
            self.reconciliation_winner_agent_id,
        ):
            raise ValueError("reconciliation winner fields disagree")
        if self.reconciliation_winner_claim_id not in known_ids | {None}:
            raise ValueError("reconciliation winner must be locally known")
        if self.completion is not None:
            completion_parts = _claim_parts(
                self.completion.claim_id,
                name="task-view completion claim ID",
            )
            if (
                completion_parts[0] != self.task_id
                or self.completion.claim_id not in known_ids
            ):
                raise ValueError("completion must reference a locally known task claim")


@dataclass(frozen=True, slots=True)
class TaskClaimTraceAgentView:
    """Group every task interpretation owned by one receiver-local store."""

    agent_id: int
    task_views: tuple[TaskClaimTraceTaskView, ...]


@dataclass(frozen=True, slots=True)
class TaskClaimTraceFrame:
    """Capture one explicit protocol stage at a strictly later logical instant."""

    timestamp: float
    stage: str
    active_links: tuple[TaskClaimTraceLink, ...]
    components: tuple[tuple[int, ...], ...]
    agents: tuple[TaskClaimTraceAgentView, ...]
    events: tuple[TaskClaimEvent, ...]

    def __post_init__(self) -> None:
        validate_timestamp(self.timestamp, name="trace-frame timestamp")
        if not isinstance(self.stage, str) or not self.stage.strip():
            raise ValueError("trace-frame stage must be a non-empty string")
        link_keys = tuple(
            (link.source_agent_id, link.destination_agent_id)
            for link in self.active_links
        )
        if link_keys != tuple(sorted(set(link_keys))):
            raise ValueError("trace-frame links must be unique and sorted")
        if not self.components or self.components != tuple(sorted(self.components)):
            raise ValueError("trace-frame components must be non-empty and sorted")
        if any(
            not component or component != tuple(sorted(set(component)))
            for component in self.components
        ):
            raise ValueError("each component must contain unique sorted UAV IDs")
        agent_order = tuple(agent.agent_id for agent in self.agents)
        if agent_order != tuple(sorted(set(agent_order))):
            raise ValueError("trace-frame agents must be unique and sorted")
        if self.events != tuple(sorted(self.events, key=_event_key)):
            raise ValueError("trace-frame events must use deterministic ordering")


@dataclass(frozen=True, slots=True)
class TaskClaimTrace:
    """Provide the validated standalone ownership-trace document."""

    schema_version: int
    metadata: TaskClaimTraceMetadata
    frames: tuple[TaskClaimTraceFrame, ...]

    def __post_init__(self) -> None:
        if self.schema_version != TASK_CLAIM_TRACE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported task-claim trace schema version: {self.schema_version}"
            )
        if not self.frames:
            raise ValueError("a task-claim trace must contain at least one frame")
        previous: float | None = None
        for frame in self.frames:
            if previous is not None and frame.timestamp <= previous:
                raise ValueError("trace-frame timestamps must be strictly increasing")
            self._validate_frame_membership(frame, previous)
            previous = frame.timestamp

    def _validate_frame_membership(
        self,
        frame: TaskClaimTraceFrame,
        previous: float | None,
    ) -> None:
        agent_ids = frozenset(self.metadata.agent_ids)
        task_ids = frozenset(self.metadata.task_ids)
        flattened = tuple(
            agent_id for component in frame.components for agent_id in component
        )
        if len(flattened) != len(agent_ids) or frozenset(flattened) != agent_ids:
            raise ValueError("trace components must partition the exact UAV membership")
        component_for = {
            agent_id: index
            for index, component in enumerate(frame.components)
            for agent_id in component
        }
        if any(
            link.source_agent_id not in agent_ids
            or link.destination_agent_id not in agent_ids
            or component_for[link.source_agent_id]
            != component_for[link.destination_agent_id]
            for link in frame.active_links
        ):
            raise ValueError("trace links must connect known UAVs in one component")
        if tuple(agent.agent_id for agent in frame.agents) != self.metadata.agent_ids:
            raise ValueError("trace agent views must match metadata membership")
        for agent in frame.agents:
            if (
                tuple(view.task_id for view in agent.task_views)
                != self.metadata.task_ids
            ):
                raise ValueError("each receiver must expose every configured task")
            if any(
                claim.owner_agent_id not in agent_ids
                or not isclose(
                    claim.age,
                    frame.timestamp - claim.received_at,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                for view in agent.task_views
                for claim in view.known_claims
            ):
                raise ValueError("known-claim owner or receiver-local age is invalid")
            if any(view.task_id not in task_ids for view in agent.task_views):
                raise ValueError("task view is outside metadata membership")
        for event in frame.events:
            _validate_event(event, previous, frame.timestamp, agent_ids, task_ids)

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable dictionary."""

        return asdict(self)

    def write_json(self, path: str | Path) -> None:
        """Write the trace without changing a claim store or protocol clock."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read_json(cls, path: str | Path) -> TaskClaimTrace:
        """Read and validate a trace document from disk."""

        with Path(path).open(encoding="utf-8") as source:
            document = json.load(source)
        if not isinstance(document, Mapping):
            raise ValueError("task-claim trace root must be an object")
        return cls.from_dict(document)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TaskClaimTrace:
        """Rebuild immutable records and reject malformed trace documents."""

        version = data["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("task-claim trace schema_version must be an integer")
        metadata = _metadata_from_dict(_object(data["metadata"], "metadata"))
        frames = tuple(
            _frame_from_dict(_object(item, "frame")) for item in data["frames"]
        )
        return cls(schema_version=version, metadata=metadata, frames=frames)


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _metadata_from_dict(data: Mapping[str, Any]) -> TaskClaimTraceMetadata:
    return TaskClaimTraceMetadata(
        scenario=data["scenario"],
        agent_ids=tuple(data["agent_ids"]),
        task_ids=tuple(data["task_ids"]),
        freshness_timeout=data["freshness_timeout"],
        lease_timeout=data["lease_timeout"],
    )


def _known_claim_from_dict(data: Any) -> TaskClaimTraceKnownClaim:
    item = _object(data, "known claim")
    return TaskClaimTraceKnownClaim(**item)


def _task_view_from_dict(data: Any) -> TaskClaimTraceTaskView:
    item = _object(data, "task view")
    completion_data = item["completion"]
    completion = (
        None
        if completion_data is None
        else TaskClaimTraceCompletion(**_object(completion_data, "completion"))
    )
    return TaskClaimTraceTaskView(
        **{
            **item,
            "released_claim_ids": tuple(item["released_claim_ids"]),
            "completion": completion,
            "known_claims": tuple(
                _known_claim_from_dict(claim) for claim in item["known_claims"]
            ),
        }
    )


def _event_from_dict(data: Any) -> TaskClaimEvent:
    item = _object(data, "task-claim event")
    return TaskClaimEvent(**{**item, "kind": TaskClaimEventKind(item["kind"])})


def _frame_from_dict(data: Mapping[str, Any]) -> TaskClaimTraceFrame:
    agents = tuple(
        TaskClaimTraceAgentView(
            agent_id=_object(agent, "agent view")["agent_id"],
            task_views=tuple(
                _task_view_from_dict(view)
                for view in _object(agent, "agent view")["task_views"]
            ),
        )
        for agent in data["agents"]
    )
    return TaskClaimTraceFrame(
        timestamp=data["timestamp"],
        stage=data["stage"],
        active_links=tuple(
            TaskClaimTraceLink(**_object(link, "active link"))
            for link in data["active_links"]
        ),
        components=tuple(tuple(component) for component in data["components"]),
        agents=agents,
        events=tuple(_event_from_dict(event) for event in data["events"]),
    )


def _event_key(event: TaskClaimEvent) -> tuple[float, str, int, int, int, int, str]:
    return (
        event.timestamp,
        event.kind.value,
        event.observer_agent_id,
        event.task_id,
        event.owner_agent_id or 0,
        event.source_agent_id or 0,
        event.claim_id or "",
    )


def _validate_event(
    event: TaskClaimEvent,
    lower: float | None,
    upper: float,
    agent_ids: frozenset[int],
    task_ids: frozenset[int],
) -> None:
    timestamp = validate_timestamp(event.timestamp, name="task-claim event timestamp")
    if timestamp > upper or (lower is not None and timestamp <= lower):
        raise ValueError("task-claim event must belong to its frame delta")
    referenced_agents = tuple(
        agent_id
        for agent_id in (
            event.observer_agent_id,
            event.owner_agent_id,
            event.source_agent_id,
            event.winner_agent_id,
        )
        if agent_id is not None
    )
    if set(referenced_agents) - agent_ids or event.task_id not in task_ids:
        raise ValueError("task-claim event references unknown membership")
    claim_fields = (event.claim_id, event.owner_agent_id, event.epoch)
    if any(value is not None for value in claim_fields):
        if any(value is None for value in claim_fields):
            raise ValueError("task-claim event claim fields must be present together")
        assert event.claim_id is not None
        claim_parts = _claim_parts(event.claim_id, name="event claim ID")
        if claim_parts != (event.task_id, event.owner_agent_id, event.epoch):
            raise ValueError("task-claim event claim fields disagree")

    winner_fields = (event.winner_claim_id, event.winner_agent_id)
    if any(value is not None for value in winner_fields):
        if any(value is None for value in winner_fields):
            raise ValueError("task-claim event winner fields must be present together")
        assert event.winner_claim_id is not None
        winner_parts = _claim_parts(event.winner_claim_id, name="event winner ID")
        if winner_parts[:2] != (event.task_id, event.winner_agent_id):
            raise ValueError("task-claim event winner fields disagree")


def _canonical_links(
    active_links: Iterable[tuple[int, int]],
    agent_ids: frozenset[int],
) -> tuple[TaskClaimTraceLink, ...]:
    keys: set[tuple[int, int]] = set()
    for pair in active_links:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("active_links must contain two-item UAV ID tuples")
        left, right = pair
        if left == right or {left, right} - agent_ids:
            raise ValueError("active_links must connect two different known UAVs")
        source, destination = sorted((left, right))
        keys.add((source, destination))
    return tuple(TaskClaimTraceLink(*key) for key in sorted(keys))


def _trace_task_view(view: TaskOwnershipView) -> TaskClaimTraceTaskView:
    claims = tuple(
        TaskClaimTraceKnownClaim(
            claim_id=canonical_claim_id(observation.claim.claim_id),
            owner_agent_id=observation.claim.owner_agent_id,
            epoch=observation.claim.epoch,
            created_at=observation.claim.created_at,
            received_at=observation.received_at,
            age=observation.age,
            freshness=observation.freshness.value,
            current=observation.current_for_owner,
            released=observation.released,
            reconciled_loser=observation.reconciled_loser,
            valid=(
                observation.current_for_owner
                and not observation.released
                and not observation.reconciled_loser
                and observation.freshness.value != "EXPIRED"
            ),
        )
        for observation in view.known_claim_observations
    )
    completion = view.completion
    trace_completion = (
        None
        if completion is None
        else TaskClaimTraceCompletion(
            claim_id=canonical_claim_id(completion.claim.claim_id),
            owner_agent_id=completion.owner_agent_id,
            epoch=completion.claim.epoch,
            created_at=completion.created_at,
        )
    )
    return TaskClaimTraceTaskView(
        task_id=view.task_id,
        state=view.state.value,
        known_owner_agent_id=view.known_owner_agent_id,
        claim_id=None if view.claim_id is None else canonical_claim_id(view.claim_id),
        epoch=view.epoch,
        claim_age=view.claim_age,
        claim_freshness=(
            None if view.claim_freshness is None else view.claim_freshness.value
        ),
        contested=view.contested,
        reconciliation_winner_agent_id=view.reconciliation_winner_agent_id,
        reconciliation_winner_claim_id=(
            None
            if view.reconciliation_winner_claim_id is None
            else canonical_claim_id(view.reconciliation_winner_claim_id)
        ),
        released=view.released,
        released_claim_ids=tuple(
            canonical_claim_id(claim_id) for claim_id in view.released_claim_ids
        ),
        completion=trace_completion,
        complete=view.complete,
        known_claims=claims,
    )


class TaskClaimTraceRecorder:
    """Read homogeneous local stores and accumulate strictly ordered trace frames."""

    def __init__(self, scenario: str, stores: Mapping[int, TaskClaimStore]) -> None:
        if not stores:
            raise ValueError("task-claim trace recorder requires local stores")
        self._stores = dict(stores)
        agent_ids = tuple(sorted(self._stores))
        first = self._stores[agent_ids[0]]
        for owner_id, store in sorted(self._stores.items()):
            if (
                store.owner_agent_id != owner_id
                or store.agent_ids != agent_ids
                or store.task_ids != first.task_ids
                or store.freshness_timeout != first.freshness_timeout
                or store.lease_timeout != first.lease_timeout
            ):
                raise ValueError(
                    "trace recorder requires homogeneous keyed claim stores"
                )
        self._metadata = TaskClaimTraceMetadata(
            scenario=scenario,
            agent_ids=agent_ids,
            task_ids=first.task_ids,
            freshness_timeout=first.freshness_timeout,
            lease_timeout=first.lease_timeout,
        )
        self._frames: list[TaskClaimTraceFrame] = []
        self._event_count = 0

    @property
    def metadata(self) -> TaskClaimTraceMetadata:
        return self._metadata

    def capture(
        self,
        timestamp: float,
        stage: str,
        *,
        active_links: Iterable[tuple[int, int]],
        components: Iterable[Iterable[int]],
        events: Sequence[TaskClaimEvent],
    ) -> None:
        """Append one boundary using pure local snapshots and observer inputs."""

        previous = None if not self._frames else self._frames[-1].timestamp
        timestamp = validate_timestamp(timestamp, name="task-claim capture timestamp")
        if previous is not None and timestamp <= previous:
            raise ValueError("task-claim capture timestamps must strictly increase")
        if len(events) < self._event_count:
            raise ValueError("cumulative task-claim events must not shrink")

        # snapshot is a pure read, so the recorder cannot advance protocol state.
        agents = tuple(
            TaskClaimTraceAgentView(
                agent_id=owner_id,
                task_views=tuple(
                    _trace_task_view(view)
                    for view in self._stores[owner_id].snapshot(timestamp).task_views
                ),
            )
            for owner_id in self._metadata.agent_ids
        )
        frame = TaskClaimTraceFrame(
            timestamp=timestamp,
            stage=stage,
            active_links=_canonical_links(
                active_links,
                frozenset(self._metadata.agent_ids),
            ),
            components=tuple(
                sorted(tuple(sorted(component)) for component in components)
            ),
            agents=agents,
            events=tuple(sorted(events[self._event_count :], key=_event_key)),
        )
        # validating the candidate document also checks cross-frame event deltas.
        TaskClaimTrace(
            schema_version=TASK_CLAIM_TRACE_SCHEMA_VERSION,
            metadata=self._metadata,
            frames=(*self._frames, frame),
        )
        self._frames.append(frame)
        self._event_count = len(events)

    def finish(self) -> TaskClaimTrace:
        """Freeze and validate every recorded ownership boundary."""

        return TaskClaimTrace(
            schema_version=TASK_CLAIM_TRACE_SCHEMA_VERSION,
            metadata=self._metadata,
            frames=tuple(self._frames),
        )


__all__ = [
    "TASK_CLAIM_TRACE_SCHEMA_VERSION",
    "TaskClaimTrace",
    "TaskClaimTraceAgentView",
    "TaskClaimTraceCompletion",
    "TaskClaimTraceFrame",
    "TaskClaimTraceKnownClaim",
    "TaskClaimTraceLink",
    "TaskClaimTraceMetadata",
    "TaskClaimTraceRecorder",
    "TaskClaimTraceTaskView",
    "canonical_claim_id",
]
