"""Deliver receiver-local evidence through one deterministic network model.

Heartbeats, failure evidence, and task-ownership messages reach only receivers
whose current link and local execution state permit delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from .agent import Heartbeat
from .communication import CommunicationGraph
from .failure_manager import FailureDeclaration, FailureManager, FailureVote
from .peer_state import PeerStateStore
from .task_claims import (
    ClaimId,
    TaskClaim,
    TaskClaimRelease,
    TaskClaimStore,
    TaskCompletionEvidence,
)
from .validation import validate_timestamp


@dataclass(frozen=True, slots=True)
class DeliveryBatch:
    """Aggregate results for one deterministic state-publication batch."""

    attempted: int
    delivered: int
    undelivered: int
    refreshed_observations: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if min(self.attempted, self.delivered, self.undelivered) < 0:
            raise ValueError("delivery counts must be non-negative")
        if self.attempted != self.delivered + self.undelivered:
            raise ValueError("attempted deliveries must equal batch outcomes")


@dataclass(frozen=True, slots=True)
class ProtocolDeliveryBatch:
    """Aggregate network outcomes for failure-protocol messages."""

    attempted: int
    delivered: int
    undelivered: int

    def __post_init__(self) -> None:
        if min(self.attempted, self.delivered, self.undelivered) < 0:
            raise ValueError("delivery counts must be non-negative")
        if self.attempted != self.delivered + self.undelivered:
            raise ValueError("attempted deliveries must equal batch outcomes")


class TaskProtocolMessageKind(str, Enum):
    """The immutable ownership evidence carried by one delivery receipt."""

    CLAIM = "CLAIM"
    RELEASE = "RELEASE"
    COMPLETION = "COMPLETION"


@dataclass(frozen=True, slots=True)
class TaskProtocolReceipt:
    """Describe one graph-authorized delivery into one receiver-local store."""

    receiver_agent_id: int
    source_agent_id: int
    task_id: int
    claim_id: ClaimId
    epoch: int
    kind: TaskProtocolMessageKind
    changed: bool


@dataclass(frozen=True, slots=True)
class TaskProtocolDeliveryBatch:
    """Aggregate task-protocol outcomes without merging receiver knowledge."""

    attempted: int
    delivered: int
    undelivered: int
    receipts: tuple[TaskProtocolReceipt, ...]

    def __post_init__(self) -> None:
        if min(self.attempted, self.delivered, self.undelivered) < 0:
            raise ValueError("delivery counts must be non-negative")
        if self.attempted != self.delivered + self.undelivered:
            raise ValueError("attempted deliveries must equal batch outcomes")
        if len(self.receipts) != self.delivered:
            raise ValueError("each delivered task message requires one receipt")


class PeerStateTransport:
    """Deliver immutable snapshots across active direct graph links only."""

    def __init__(
        self,
        graph: CommunicationGraph,
        stores: Mapping[int, PeerStateStore],
    ) -> None:
        if frozenset(stores) != frozenset(graph.agent_ids):
            raise ValueError("peer-state stores must match communication graph UAVs")
        for owner_agent_id, store in stores.items():
            if store.owner_agent_id != owner_agent_id:
                raise ValueError("peer-state store key must match its owner")
            expected_peers = tuple(
                agent_id for agent_id in graph.agent_ids if agent_id != owner_agent_id
            )
            if store.peer_agent_ids != expected_peers:
                raise ValueError("each peer-state store must contain every other UAV")
        self._graph = graph
        self._stores = dict(stores)
        self._agent_ids = frozenset(graph.agent_ids)
        self._last_timestamp: float | None = None
        self._last_protocol_timestamp: float | None = None

    def deliver(
        self,
        snapshots: Iterable[Heartbeat],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> DeliveryBatch:
        """Attempt one directed delivery from each source to every other UAV."""

        if not self._graph.initialized:
            raise RuntimeError(
                "communication graph must be initialized before delivery"
            )
        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="transport timestamp",
        )
        ordered_snapshots = tuple(sorted(snapshots, key=lambda item: item.agent_id))
        source_ids = tuple(snapshot.agent_id for snapshot in ordered_snapshots)
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("a delivery batch may contain one snapshot per source")
        unknown_sources = set(source_ids) - self._agent_ids
        if unknown_sources:
            raise ValueError(f"unknown snapshot source UAVs: {sorted(unknown_sources)}")
        if any(snapshot.timestamp > timestamp for snapshot in ordered_snapshots):
            raise ValueError("snapshot timestamp cannot follow delivery time")
        receivers = self._receivers(receiving_agent_ids)

        attempted = 0
        delivered = 0
        refreshed: list[tuple[int, int]] = []
        for snapshot in ordered_snapshots:
            # one neighbor lookup replaces a validated graph lookup per receiver.
            active_receivers = self._graph.neighbors(snapshot.agent_id) & receivers
            for receiver_agent_id in self._graph.agent_ids:
                if receiver_agent_id == snapshot.agent_id:
                    continue
                attempted += 1
                if receiver_agent_id not in active_receivers:
                    continue
                delivered += 1
                if self._stores[receiver_agent_id].receive(snapshot, timestamp):
                    refreshed.append((receiver_agent_id, snapshot.agent_id))

        self._last_timestamp = timestamp
        return DeliveryBatch(
            attempted=attempted,
            delivered=delivered,
            undelivered=attempted - delivered,
            refreshed_observations=tuple(refreshed),
        )

    def deliver_failure_votes(
        self,
        votes: Iterable[FailureVote],
        failure_manager: FailureManager,
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> ProtocolDeliveryBatch:
        """Broadcast suspicion votes through active links into local mailboxes."""

        timestamp = self._validate_protocol_time(timestamp)
        receivers = self._receivers(receiving_agent_ids)
        ordered_votes = tuple(
            sorted(
                votes,
                key=lambda item: (item.voter_agent_id, item.suspected_agent_id),
            )
        )
        if any(vote.created_at > timestamp for vote in ordered_votes):
            raise ValueError("failure-vote timestamp cannot follow delivery time")
        attempted = 0
        delivered = 0
        for vote in ordered_votes:
            # proposal already stored the creator's vote; transport mutates remotes only.
            for receiver_agent_id in self._graph.agent_ids:
                if receiver_agent_id in {
                    vote.voter_agent_id,
                    vote.suspected_agent_id,
                }:
                    continue
                attempted += 1
                if receiver_agent_id not in receivers or not self._graph.can_deliver(
                    vote.voter_agent_id, receiver_agent_id
                ):
                    continue
                failure_manager.record_vote(receiver_agent_id, vote)
                delivered += 1
        self._last_protocol_timestamp = timestamp
        return ProtocolDeliveryBatch(
            attempted=attempted,
            delivered=delivered,
            undelivered=attempted - delivered,
        )

    def deliver_failure_declarations(
        self,
        declarations: Iterable[FailureDeclaration],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> ProtocolDeliveryBatch:
        """Deliver quorum-backed declarations before world recovery is applied."""

        timestamp = self._validate_protocol_time(timestamp)
        receivers = self._receivers(receiving_agent_ids)
        ordered_declarations = tuple(
            sorted(
                declarations,
                key=lambda item: (item.declarer_agent_id, item.agent_id),
            )
        )
        if any(
            declaration.detected_at > timestamp for declaration in ordered_declarations
        ):
            raise ValueError(
                "failure-declaration timestamp cannot follow delivery time"
            )
        attempted = 0
        delivered = 0
        for declaration in ordered_declarations:
            # detection already updated the declarer; transport mutates remotes only.
            for receiver_agent_id in self._graph.agent_ids:
                if receiver_agent_id in {
                    declaration.declarer_agent_id,
                    declaration.agent_id,
                }:
                    continue
                attempted += 1
                if receiver_agent_id not in receivers or not self._graph.can_deliver(
                    declaration.declarer_agent_id, receiver_agent_id
                ):
                    continue
                self._apply_declaration(receiver_agent_id, declaration)
                delivered += 1
        self._last_protocol_timestamp = timestamp
        return ProtocolDeliveryBatch(
            attempted=attempted,
            delivered=delivered,
            undelivered=attempted - delivered,
        )

    def _apply_declaration(
        self, receiver_agent_id: int, declaration: FailureDeclaration
    ) -> None:
        self._stores[receiver_agent_id].apply_failure_declaration(
            declaration.agent_id,
            voter_agent_ids=declaration.voter_agent_ids,
            required_votes=declaration.required_votes,
        )

    def _receivers(self, supplied_agent_ids: Iterable[int] | None) -> frozenset[int]:
        if supplied_agent_ids is None:
            return self._agent_ids
        receivers = frozenset(supplied_agent_ids)
        unknown_receivers = receivers - self._agent_ids
        if unknown_receivers:
            raise ValueError(f"unknown receiver UAVs: {sorted(unknown_receivers)}")
        return receivers

    def _validate_protocol_time(self, timestamp: float) -> float:
        if not self._graph.initialized:
            raise RuntimeError(
                "communication graph must be initialized before protocol delivery"
            )
        return validate_timestamp(
            timestamp,
            previous=self._last_protocol_timestamp,
            name="failure-protocol transport timestamp",
        )


TaskProtocolMessage = TaskClaim | TaskClaimRelease | TaskCompletionEvidence


class TaskClaimTransport:
    """Deliver claim protocol values through active direct graph links only."""

    def __init__(
        self,
        graph: CommunicationGraph,
        stores: Mapping[int, TaskClaimStore],
    ) -> None:
        graph_agent_ids = frozenset(graph.agent_ids)
        if frozenset(stores) != graph_agent_ids:
            raise ValueError("task-claim stores must match communication graph UAVs")
        expected_agent_ids = tuple(sorted(graph.agent_ids))
        task_ids: tuple[int, ...] | None = None
        freshness_timeout: float | None = None
        lease_timeout: float | None = None
        for owner_agent_id, store in stores.items():
            if store.owner_agent_id != owner_agent_id:
                raise ValueError("task-claim store key must match its owner")
            if store.agent_ids != expected_agent_ids:
                raise ValueError("each task-claim store must contain every graph UAV")
            if task_ids is None:
                task_ids = store.task_ids
                freshness_timeout = store.freshness_timeout
                lease_timeout = store.lease_timeout
                continue
            if store.task_ids != task_ids:
                raise ValueError("task-claim stores must contain the same task IDs")
            if (
                store.freshness_timeout != freshness_timeout
                or store.lease_timeout != lease_timeout
            ):
                raise ValueError("task-claim stores must use one lease policy")
        self._graph = graph
        self._stores = dict(stores)
        self._agent_ids = graph_agent_ids
        self._last_timestamp: float | None = None

    def deliver_claims(
        self,
        claims: Iterable[TaskClaim],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> TaskProtocolDeliveryBatch:
        """Deliver owner publications without refreshing duplicate evidence."""

        return self._deliver(
            tuple(claims),
            timestamp,
            receiving_agent_ids=receiving_agent_ids,
        )

    def deliver_releases(
        self,
        releases: Iterable[TaskClaimRelease],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> TaskProtocolDeliveryBatch:
        """Deliver exact-claim tombstones from their releasing owners."""

        return self._deliver(
            tuple(releases),
            timestamp,
            receiving_agent_ids=receiving_agent_ids,
        )

    def deliver_completions(
        self,
        completions: Iterable[TaskCompletionEvidence],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> TaskProtocolDeliveryBatch:
        """Deliver self-contained terminal evidence from its claim owner."""

        return self._deliver(
            tuple(completions),
            timestamp,
            receiving_agent_ids=receiving_agent_ids,
        )

    def _deliver(
        self,
        messages: tuple[TaskProtocolMessage, ...],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None,
    ) -> TaskProtocolDeliveryBatch:
        if not self._graph.initialized:
            raise RuntimeError(
                "communication graph must be initialized before task delivery"
            )
        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="task-protocol transport timestamp",
        )
        receivers = self._receivers(receiving_agent_ids)
        ordered_messages = tuple(sorted(messages, key=self._message_sort_key))
        for message in ordered_messages:
            source_agent_id = self._source_agent_id(message)
            if source_agent_id not in self._agent_ids:
                raise ValueError(f"unknown task-message source UAV {source_agent_id}")
            if self._message_created_at(message) > timestamp:
                raise ValueError(
                    "task-message creation timestamp cannot follow delivery time"
                )

        attempted = 0
        delivered = 0
        receipts: list[TaskProtocolReceipt] = []
        for message in ordered_messages:
            source_agent_id = self._source_agent_id(message)
            claim = self._identity_claim(message)
            active_receivers = self._graph.neighbors(source_agent_id) & receivers
            for receiver_agent_id in self._graph.agent_ids:
                # local creation already updated the sender, so delivery targets peers.
                if receiver_agent_id == source_agent_id:
                    continue
                attempted += 1
                if receiver_agent_id not in active_receivers:
                    continue
                changed = self._apply(
                    self._stores[receiver_agent_id],
                    message,
                    timestamp,
                )
                delivered += 1
                receipts.append(
                    TaskProtocolReceipt(
                        receiver_agent_id=receiver_agent_id,
                        source_agent_id=source_agent_id,
                        task_id=claim.task_id,
                        claim_id=claim.claim_id,
                        epoch=claim.epoch,
                        kind=self._message_kind(message),
                        changed=changed,
                    )
                )
        self._last_timestamp = timestamp
        return TaskProtocolDeliveryBatch(
            attempted=attempted,
            delivered=delivered,
            undelivered=attempted - delivered,
            receipts=tuple(receipts),
        )

    def _receivers(self, supplied_agent_ids: Iterable[int] | None) -> frozenset[int]:
        if supplied_agent_ids is None:
            return self._agent_ids
        receivers = frozenset(supplied_agent_ids)
        unknown_receivers = receivers - self._agent_ids
        if unknown_receivers:
            raise ValueError(f"unknown receiver UAVs: {sorted(unknown_receivers)}")
        return receivers

    @staticmethod
    def _source_agent_id(message: TaskProtocolMessage) -> int:
        if isinstance(message, TaskClaimRelease):
            return message.releasing_agent_id
        if isinstance(message, TaskCompletionEvidence):
            return message.owner_agent_id
        return message.owner_agent_id

    @staticmethod
    def _identity_claim(message: TaskProtocolMessage) -> TaskClaim:
        if isinstance(message, TaskClaimRelease):
            return message.losing_claim
        if isinstance(message, TaskCompletionEvidence):
            return message.claim
        return message

    @staticmethod
    def _message_created_at(message: TaskProtocolMessage) -> float:
        return message.created_at

    @staticmethod
    def _message_kind(message: TaskProtocolMessage) -> TaskProtocolMessageKind:
        if isinstance(message, TaskClaimRelease):
            return TaskProtocolMessageKind.RELEASE
        if isinstance(message, TaskCompletionEvidence):
            return TaskProtocolMessageKind.COMPLETION
        return TaskProtocolMessageKind.CLAIM

    @classmethod
    def _message_sort_key(
        cls,
        message: TaskProtocolMessage,
    ) -> tuple[int, int, int, str]:
        claim = cls._identity_claim(message)
        return (
            cls._source_agent_id(message),
            claim.task_id,
            claim.epoch,
            cls._message_kind(message).value,
        )

    @staticmethod
    def _apply(
        store: TaskClaimStore,
        message: TaskProtocolMessage,
        timestamp: float,
    ) -> bool:
        if isinstance(message, TaskClaimRelease):
            return store.receive_release(message, timestamp)
        if isinstance(message, TaskCompletionEvidence):
            return store.receive_completion(message, timestamp)
        return store.receive_claim(message, timestamp)


__all__ = [
    "DeliveryBatch",
    "PeerStateTransport",
    "ProtocolDeliveryBatch",
    "TaskClaimTransport",
    "TaskProtocolDeliveryBatch",
    "TaskProtocolMessageKind",
    "TaskProtocolReceipt",
]
