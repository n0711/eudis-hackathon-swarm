"""Deliver receiver-local evidence through one deterministic network model.

Heartbeats remain one-hop observations. Immutable failure and task-ownership
evidence is stored, forwarded, and deduplicated across available one-hop links.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

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

_MessageT = TypeVar("_MessageT")
_MessageIdT = TypeVar("_MessageIdT", bound=Hashable)


@dataclass(frozen=True, slots=True)
class _KnownMessage(Generic[_MessageT]):
    """One transport replica's immutable knowledge and shortest learned hop."""

    message: _MessageT
    origin_agent_id: int
    hop_count: int


@dataclass(frozen=True, slots=True)
class _GossipDelivery(Generic[_MessageT, _MessageIdT]):
    """One first delivery of immutable evidence into a receiver replica."""

    message: _MessageT
    message_id: _MessageIdT
    origin_agent_id: int
    forwarder_agent_id: int
    receiver_agent_id: int
    hop_count: int
    changed: bool


@dataclass(frozen=True, slots=True)
class _GossipBatch(Generic[_MessageT, _MessageIdT]):
    attempted: int
    delivered: int
    undelivered: int
    forwarded: int
    duplicate_source_publications: int
    duplicate_route_suppressions: int
    inactive_endpoint_deferrals: int
    deliveries: tuple[_GossipDelivery[_MessageT, _MessageIdT], ...]

    @property
    def duplicates_suppressed(self) -> int:
        """Return the legacy aggregate of distinct duplicate mechanisms."""

        return self.duplicate_source_publications + self.duplicate_route_suppressions

    @property
    def useful_first_deliveries(self) -> int:
        """Return first deliveries that changed receiver-local domain state."""

        return sum(delivery.changed for delivery in self.deliveries)


class _DeterministicGossip(Generic[_MessageT, _MessageIdT]):
    """Persist and flood immutable evidence without exposing graph truth to stores.

    A route remains pending until its one-hop delivery succeeds or another route
    teaches the receiver the same structural message.  Each receiver therefore
    applies one message ID at most once, while an intermediate that learned it can
    carry it across a bridge that appears later.  The finite node/message/route
    state provides the hop bound; no TTL or random scheduling is required.
    """

    def __init__(
        self,
        agent_ids: Iterable[int],
        *,
        identity: Callable[[_MessageT], _MessageIdT],
        origin: Callable[[_MessageT], int],
        sort_token: Callable[[_MessageT], str],
    ) -> None:
        self._agent_ids = tuple(sorted(agent_ids))
        self._agent_id_set = frozenset(self._agent_ids)
        self._identity = identity
        self._origin = origin
        self._sort_token = sort_token
        self._messages: dict[_MessageIdT, _MessageT] = {}
        self._known_by_agent: dict[int, dict[_MessageIdT, _KnownMessage[_MessageT]]] = {
            agent_id: {} for agent_id in self._agent_ids
        }
        self._pending_routes: set[tuple[int, int, _MessageIdT]] = set()
        self._known_inactive_deferrals: set[tuple[int, _MessageIdT]] = set()

    def seen_message_ids(self, agent_id: int) -> frozenset[_MessageIdT]:
        """Return one transport replica's seen IDs without exposing payloads."""

        if agent_id not in self._agent_id_set:
            raise KeyError(f"unknown gossip receiver UAV {agent_id}")
        return frozenset(self._known_by_agent[agent_id])

    def disseminate(
        self,
        messages: Iterable[_MessageT],
        graph: CommunicationGraph,
        participating_agent_ids: frozenset[int],
        *,
        apply: Callable[[int, _MessageT], bool],
    ) -> _GossipBatch[_MessageT, _MessageIdT]:
        """Seed origin evidence and run stable one-hop rounds to a fixed point.

        ``participating_agent_ids`` names software replicas that may receive or
        relay in this round. An origin explicitly publishing in ``messages`` may
        transmit even when it is not itself an eligible receiver; a previously
        seeded origin must participate or publish again before its own pending
        routes can run.
        """

        ordered_messages = tuple(sorted(messages, key=self._sort_token))
        current_origin_ids: set[int] = set()
        prepared: list[tuple[_MessageIdT, int, _MessageT]] = []
        batch_messages: dict[_MessageIdT, _MessageT] = {}
        for message in ordered_messages:
            message_id = self._identity(message)
            origin_agent_id = self._origin(message)
            if origin_agent_id not in self._agent_id_set:
                raise ValueError(
                    f"unknown protocol-message origin UAV {origin_agent_id}"
                )
            previous = self._messages.get(message_id)
            if previous is not None and previous != message:
                raise ValueError(
                    "one protocol message ID cannot carry different evidence"
                )
            duplicate_in_batch = batch_messages.get(message_id)
            if duplicate_in_batch is not None and duplicate_in_batch != message:
                raise ValueError(
                    "one protocol message ID cannot carry different evidence"
                )
            batch_messages[message_id] = message
            prepared.append((message_id, origin_agent_id, message))

        duplicate_source_publications = 0
        duplicate_route_suppressions = 0
        for message_id, origin_agent_id, message in prepared:
            current_origin_ids.add(origin_agent_id)
            self._messages.setdefault(message_id, message)
            known = self._known_by_agent[origin_agent_id]
            if message_id in known:
                duplicate_source_publications += 1
                continue
            known[message_id] = _KnownMessage(
                message=message,
                origin_agent_id=origin_agent_id,
                hop_count=0,
            )
            duplicate_route_suppressions += self._schedule_from(
                origin_agent_id, message_id
            )

        attempted = 0
        delivered = 0
        undelivered = 0
        forwarded = 0
        deliveries: list[_GossipDelivery[_MessageT, _MessageIdT]] = []
        attempted_this_round: set[tuple[int, int, _MessageIdT]] = set()
        inactive_candidates: set[tuple[int, _MessageIdT]] = set()
        eligible_attempts: set[tuple[int, _MessageIdT]] = set()

        while True:
            candidates = tuple(
                sorted(
                    (
                        route
                        for route in self._pending_routes
                        if route not in attempted_this_round
                    ),
                    key=self._route_sort_key,
                )
            )
            if not candidates:
                break
            for route in candidates:
                forwarder_agent_id, receiver_agent_id, message_id = route
                attempted_this_round.add(route)
                receiver_knowledge = self._known_by_agent[receiver_agent_id]
                if message_id in receiver_knowledge:
                    self._pending_routes.discard(route)
                    duplicate_route_suppressions += 1
                    continue

                forwarder_can_run = (
                    forwarder_agent_id in participating_agent_ids
                    or forwarder_agent_id in current_origin_ids
                )
                if (
                    not forwarder_can_run
                    or receiver_agent_id not in participating_agent_ids
                ):
                    inactive_candidates.add((receiver_agent_id, message_id))
                    continue

                attempted += 1
                eligible_attempts.add((receiver_agent_id, message_id))
                if receiver_agent_id not in graph.neighbors(forwarder_agent_id):
                    undelivered += 1
                    continue

                knowledge = self._known_by_agent[forwarder_agent_id][message_id]
                message = knowledge.message
                changed = apply(receiver_agent_id, message)
                hop_count = knowledge.hop_count + 1
                receiver_knowledge[message_id] = _KnownMessage(
                    message=message,
                    origin_agent_id=knowledge.origin_agent_id,
                    hop_count=hop_count,
                )
                self._pending_routes.discard(route)
                delivered += 1
                if forwarder_agent_id != knowledge.origin_agent_id:
                    forwarded += 1
                deliveries.append(
                    _GossipDelivery(
                        message=message,
                        message_id=message_id,
                        origin_agent_id=knowledge.origin_agent_id,
                        forwarder_agent_id=forwarder_agent_id,
                        receiver_agent_id=receiver_agent_id,
                        hop_count=hop_count,
                        changed=changed,
                    )
                )
                duplicate_route_suppressions += self._schedule_from(
                    receiver_agent_id, message_id
                )

        new_inactive_deferrals = {
            obligation
            for obligation in inactive_candidates - eligible_attempts
            if obligation[1] not in self._known_by_agent[obligation[0]]
            and obligation not in self._known_inactive_deferrals
        }
        self._known_inactive_deferrals.update(new_inactive_deferrals)

        return _GossipBatch(
            attempted=attempted,
            delivered=delivered,
            undelivered=undelivered,
            forwarded=forwarded,
            duplicate_source_publications=duplicate_source_publications,
            duplicate_route_suppressions=duplicate_route_suppressions,
            inactive_endpoint_deferrals=len(new_inactive_deferrals),
            deliveries=tuple(deliveries),
        )

    def _schedule_from(self, forwarder_agent_id: int, message_id: _MessageIdT) -> int:
        duplicates_suppressed = 0
        for receiver_agent_id in self._agent_ids:
            if receiver_agent_id == forwarder_agent_id:
                continue
            if message_id in self._known_by_agent[receiver_agent_id]:
                duplicates_suppressed += 1
                continue
            self._pending_routes.add(
                (forwarder_agent_id, receiver_agent_id, message_id)
            )
        return duplicates_suppressed

    def _route_sort_key(
        self, route: tuple[int, int, _MessageIdT]
    ) -> tuple[int, str, int, int]:
        forwarder_agent_id, receiver_agent_id, message_id = route
        knowledge = self._known_by_agent[forwarder_agent_id][message_id]
        return (
            knowledge.hop_count,
            self._sort_token(knowledge.message),
            forwarder_agent_id,
            receiver_agent_id,
        )


@dataclass(frozen=True, slots=True)
class DeliveryBatch:
    """Aggregate results for one deterministic state-publication batch."""

    attempted: int
    delivered: int
    undelivered: int
    refreshed_observations: tuple[tuple[int, int], ...]
    delivered_observations: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if min(self.attempted, self.delivered, self.undelivered) < 0:
            raise ValueError("delivery counts must be non-negative")
        if self.attempted != self.delivered + self.undelivered:
            raise ValueError("attempted deliveries must equal batch outcomes")
        if (
            len(self.delivered_observations) != self.delivered
            or len(set(self.delivered_observations)) != self.delivered
        ):
            raise ValueError("delivered observations must identify each receipt once")
        if not set(self.refreshed_observations) <= set(self.delivered_observations):
            raise ValueError("refreshed observations must have been delivered")


@dataclass(frozen=True, slots=True)
class ProtocolDeliveryBatch:
    """Aggregate network outcomes for failure-protocol messages."""

    attempted: int
    delivered: int
    undelivered: int
    forwarded: int = 0
    duplicates_suppressed: int = 0
    duplicate_source_publications: int = 0
    duplicate_route_suppressions: int = 0
    inactive_endpoint_deferrals: int = 0
    receipts: tuple[FailureProtocolReceipt, ...] = ()

    def __post_init__(self) -> None:
        if (
            min(
                self.attempted,
                self.delivered,
                self.undelivered,
                self.forwarded,
                self.duplicates_suppressed,
                self.duplicate_source_publications,
                self.duplicate_route_suppressions,
                self.inactive_endpoint_deferrals,
            )
            < 0
        ):
            raise ValueError("delivery counts must be non-negative")
        if self.attempted != self.delivered + self.undelivered:
            raise ValueError("attempted deliveries must equal batch outcomes")
        if self.forwarded > self.delivered:
            raise ValueError("forwarded deliveries cannot exceed delivered messages")
        split_duplicates = (
            self.duplicate_source_publications + self.duplicate_route_suppressions
        )
        if split_duplicates and self.duplicates_suppressed != split_duplicates:
            raise ValueError("duplicate aggregate must equal its classified counts")
        if len(self.receipts) != self.delivered:
            raise ValueError("each delivered failure message requires one receipt")

    @property
    def logical_forwarding_attempts(self) -> int:
        """Return attempts whose sender and receiver were active participants."""

        return self.attempted

    @property
    def successful_first_deliveries(self) -> int:
        """Return first receptions of a structural message ID."""

        return self.delivered

    @property
    def unavailable_link_attempts(self) -> int:
        """Return eligible attempts rejected by the current one-hop graph."""

        return self.undelivered

    @property
    def useful_first_deliveries(self) -> int:
        """Return first deliveries that changed receiver-local domain state."""

        return sum(receipt.changed for receipt in self.receipts)


class FailureProtocolMessageKind(str, Enum):
    """Kinds of immutable evidence used by the failure protocol."""

    VOTE = "VOTE"
    DECLARATION = "DECLARATION"


@dataclass(frozen=True, slots=True)
class FailureProtocolMessageId:
    """Structural identity for one vote generation or declaration certificate."""

    kind: FailureProtocolMessageKind
    origin_agent_id: int
    suspected_agent_id: int
    emitted_at: float
    evidence_timestamp: float
    evidence_received_at: float | None
    task_id: int | None
    voter_agent_ids: tuple[int, ...] = ()
    required_votes: int | None = None


@dataclass(frozen=True, slots=True)
class FailureProtocolReceipt:
    """Record one hop without changing the evidence's logical origin."""

    message_id: FailureProtocolMessageId
    kind: FailureProtocolMessageKind
    origin_agent_id: int
    forwarder_agent_id: int
    receiver_agent_id: int
    suspected_agent_id: int
    hop_count: int
    changed: bool


class TaskProtocolMessageKind(str, Enum):
    """The immutable ownership evidence carried by one delivery receipt."""

    CLAIM = "CLAIM"
    RELEASE = "RELEASE"
    COMPLETION = "COMPLETION"


@dataclass(frozen=True, slots=True)
class TaskProtocolMessageId:
    """Structural identity for one immutable ownership-protocol value."""

    kind: TaskProtocolMessageKind
    claim_id: ClaimId


@dataclass(frozen=True, slots=True)
class TaskProtocolReceipt:
    """Describe one graph-authorized delivery into one receiver-local store."""

    receiver_agent_id: int
    source_agent_id: int
    forwarder_agent_id: int
    hop_count: int
    message_id: TaskProtocolMessageId
    task_id: int
    claim_id: ClaimId
    epoch: int
    kind: TaskProtocolMessageKind
    changed: bool

    @property
    def origin_agent_id(self) -> int:
        """Return the immutable logical source (compatibly named ``source``)."""

        return self.source_agent_id


@dataclass(frozen=True, slots=True)
class TaskProtocolDeliveryBatch:
    """Aggregate task-protocol outcomes without merging receiver knowledge."""

    attempted: int
    delivered: int
    undelivered: int
    receipts: tuple[TaskProtocolReceipt, ...]
    forwarded: int = 0
    duplicates_suppressed: int = 0
    duplicate_source_publications: int = 0
    duplicate_route_suppressions: int = 0
    inactive_endpoint_deferrals: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.attempted,
                self.delivered,
                self.undelivered,
                self.forwarded,
                self.duplicates_suppressed,
                self.duplicate_source_publications,
                self.duplicate_route_suppressions,
                self.inactive_endpoint_deferrals,
            )
            < 0
        ):
            raise ValueError("delivery counts must be non-negative")
        if self.attempted != self.delivered + self.undelivered:
            raise ValueError("attempted deliveries must equal batch outcomes")
        if self.forwarded > self.delivered:
            raise ValueError("forwarded deliveries cannot exceed delivered messages")
        split_duplicates = (
            self.duplicate_source_publications + self.duplicate_route_suppressions
        )
        if split_duplicates and self.duplicates_suppressed != split_duplicates:
            raise ValueError("duplicate aggregate must equal its classified counts")
        if len(self.receipts) != self.delivered:
            raise ValueError("each delivered task message requires one receipt")

    @property
    def logical_forwarding_attempts(self) -> int:
        """Return attempts whose sender and receiver were active participants."""

        return self.attempted

    @property
    def successful_first_deliveries(self) -> int:
        """Return first receptions of a structural message ID."""

        return self.delivered

    @property
    def unavailable_link_attempts(self) -> int:
        """Return eligible attempts rejected by the current one-hop graph."""

        return self.undelivered

    @property
    def useful_first_deliveries(self) -> int:
        """Return first deliveries that changed receiver-local domain state."""

        return sum(receipt.changed for receipt in self.receipts)


class PeerStateTransport:
    """Deliver one-hop heartbeats and gossip immutable failure evidence."""

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
        self._vote_gossip = _DeterministicGossip[FailureVote, FailureProtocolMessageId](
            graph.agent_ids,
            identity=self._vote_message_id,
            origin=lambda vote: vote.voter_agent_id,
            sort_token=lambda vote: repr(self._vote_message_id(vote)),
        )
        self._declaration_gossip = _DeterministicGossip[
            FailureDeclaration, FailureProtocolMessageId
        ](
            graph.agent_ids,
            identity=self._declaration_message_id,
            origin=lambda declaration: declaration.declarer_agent_id,
            sort_token=lambda declaration: repr(
                self._declaration_message_id(declaration)
            ),
        )

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
        receivers = self._receivers(receiving_agent_ids)

        attempted = 0
        delivered = 0
        refreshed: list[tuple[int, int]] = []
        delivered_observations: list[tuple[int, int]] = []
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
                delivered_observations.append((receiver_agent_id, snapshot.agent_id))
                if self._stores[receiver_agent_id].receive(snapshot, timestamp):
                    refreshed.append((receiver_agent_id, snapshot.agent_id))

        self._last_timestamp = timestamp
        return DeliveryBatch(
            attempted=attempted,
            delivered=delivered,
            undelivered=attempted - delivered,
            refreshed_observations=tuple(refreshed),
            delivered_observations=tuple(delivered_observations),
        )

    def deliver_failure_votes(
        self,
        votes: Iterable[FailureVote],
        failure_manager: FailureManager,
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> ProtocolDeliveryBatch:
        """Flood suspicion votes through connected participating UAVs."""

        timestamp = self._validate_protocol_time(timestamp)
        receivers = self._receivers(receiving_agent_ids)
        ordered_votes = tuple(
            sorted(
                votes,
                key=lambda item: (item.voter_agent_id, item.suspected_agent_id),
            )
        )
        if not failure_manager.configured:
            raise RuntimeError("failure manager requires bound peer-state stores")
        for vote in ordered_votes:
            self._validate_failure_vote(vote)
        gossip_batch = self._vote_gossip.disseminate(
            ordered_votes,
            self._graph,
            receivers,
            apply=lambda receiver_agent_id, vote: failure_manager.record_vote(
                receiver_agent_id,
                vote,
                received_at=timestamp,
            ),
        )
        self._last_protocol_timestamp = timestamp
        return ProtocolDeliveryBatch(
            attempted=gossip_batch.attempted,
            delivered=gossip_batch.delivered,
            undelivered=gossip_batch.undelivered,
            forwarded=gossip_batch.forwarded,
            duplicates_suppressed=gossip_batch.duplicates_suppressed,
            duplicate_source_publications=(gossip_batch.duplicate_source_publications),
            duplicate_route_suppressions=gossip_batch.duplicate_route_suppressions,
            inactive_endpoint_deferrals=gossip_batch.inactive_endpoint_deferrals,
            receipts=tuple(
                FailureProtocolReceipt(
                    message_id=delivery.message_id,
                    kind=FailureProtocolMessageKind.VOTE,
                    origin_agent_id=delivery.origin_agent_id,
                    forwarder_agent_id=delivery.forwarder_agent_id,
                    receiver_agent_id=delivery.receiver_agent_id,
                    suspected_agent_id=delivery.message.suspected_agent_id,
                    hop_count=delivery.hop_count,
                    changed=delivery.changed,
                )
                for delivery in gossip_batch.deliveries
            ),
        )

    def deliver_failure_declarations(
        self,
        declarations: Iterable[FailureDeclaration],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> ProtocolDeliveryBatch:
        """Flood quorum-backed declarations before world recovery is applied."""

        timestamp = self._validate_protocol_time(timestamp)
        receivers = self._receivers(receiving_agent_ids)
        ordered_declarations = tuple(
            sorted(
                declarations,
                key=lambda item: (item.declarer_agent_id, item.agent_id),
            )
        )
        for declaration in ordered_declarations:
            self._validate_failure_declaration(declaration)
        gossip_batch = self._declaration_gossip.disseminate(
            ordered_declarations,
            self._graph,
            receivers,
            apply=self._apply_declaration,
        )
        self._last_protocol_timestamp = timestamp
        return ProtocolDeliveryBatch(
            attempted=gossip_batch.attempted,
            delivered=gossip_batch.delivered,
            undelivered=gossip_batch.undelivered,
            forwarded=gossip_batch.forwarded,
            duplicates_suppressed=gossip_batch.duplicates_suppressed,
            duplicate_source_publications=(gossip_batch.duplicate_source_publications),
            duplicate_route_suppressions=gossip_batch.duplicate_route_suppressions,
            inactive_endpoint_deferrals=gossip_batch.inactive_endpoint_deferrals,
            receipts=tuple(
                FailureProtocolReceipt(
                    message_id=delivery.message_id,
                    kind=FailureProtocolMessageKind.DECLARATION,
                    origin_agent_id=delivery.origin_agent_id,
                    forwarder_agent_id=delivery.forwarder_agent_id,
                    receiver_agent_id=delivery.receiver_agent_id,
                    suspected_agent_id=delivery.message.agent_id,
                    hop_count=delivery.hop_count,
                    changed=delivery.changed,
                )
                for delivery in gossip_batch.deliveries
            ),
        )

    def _apply_declaration(
        self, receiver_agent_id: int, declaration: FailureDeclaration
    ) -> bool:
        # A healthy suspected UAV may still relay a certificate about itself,
        # but its store has no self-entry on which to apply that certificate.
        if receiver_agent_id == declaration.agent_id:
            return False
        return self._stores[receiver_agent_id].apply_failure_declaration(
            declaration.agent_id,
            voter_agent_ids=declaration.voter_agent_ids,
            required_votes=declaration.required_votes,
            evidence_last_heartbeat=declaration.last_heartbeat,
        )

    def seen_failure_message_ids(
        self, receiver_agent_id: int
    ) -> frozenset[FailureProtocolMessageId]:
        """Return failure evidence suppressed as duplicates at one receiver."""

        return self._vote_gossip.seen_message_ids(
            receiver_agent_id
        ) | self._declaration_gossip.seen_message_ids(receiver_agent_id)

    def _validate_failure_vote(self, vote: FailureVote) -> None:
        unknown_agent_ids = {
            vote.voter_agent_id,
            vote.suspected_agent_id,
        } - self._agent_ids
        if unknown_agent_ids:
            raise ValueError(
                f"failure vote contains unknown UAVs: {sorted(unknown_agent_ids)}"
            )

    def _validate_failure_declaration(self, declaration: FailureDeclaration) -> None:
        unknown_agent_ids = {
            declaration.agent_id,
            declaration.declarer_agent_id,
            *declaration.voter_agent_ids,
        } - self._agent_ids
        if unknown_agent_ids:
            raise ValueError(
                "failure declaration contains unknown UAVs: "
                f"{sorted(unknown_agent_ids)}"
            )
        expected_votes = max(2, (len(self._agent_ids) - 1) // 2 + 1)
        if declaration.required_votes != expected_votes:
            raise ValueError("failure declaration uses the wrong quorum threshold")

    @staticmethod
    def _vote_message_id(vote: FailureVote) -> FailureProtocolMessageId:
        return FailureProtocolMessageId(
            kind=FailureProtocolMessageKind.VOTE,
            origin_agent_id=vote.voter_agent_id,
            suspected_agent_id=vote.suspected_agent_id,
            emitted_at=vote.created_at,
            evidence_timestamp=vote.last_heartbeat,
            evidence_received_at=vote.last_heard_at,
            task_id=vote.task_id,
        )

    @staticmethod
    def _declaration_message_id(
        declaration: FailureDeclaration,
    ) -> FailureProtocolMessageId:
        return FailureProtocolMessageId(
            kind=FailureProtocolMessageKind.DECLARATION,
            origin_agent_id=declaration.declarer_agent_id,
            suspected_agent_id=declaration.agent_id,
            emitted_at=declaration.detected_at,
            evidence_timestamp=declaration.last_heartbeat,
            evidence_received_at=None,
            task_id=declaration.task_id,
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
    """Store, forward, and deduplicate immutable task-ownership evidence."""

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
        assert task_ids is not None
        assert freshness_timeout is not None
        assert lease_timeout is not None
        self._task_ids = frozenset(task_ids)
        self._freshness_timeout = freshness_timeout
        self._lease_timeout = lease_timeout
        self._last_timestamp: float | None = None
        gossip_arguments = {
            "agent_ids": graph.agent_ids,
            "identity": self._message_id,
            "origin": self._source_agent_id,
            "sort_token": lambda message: repr(self._message_sort_key(message)),
        }
        self._claim_gossip = _DeterministicGossip[
            TaskProtocolMessage, TaskProtocolMessageId
        ](**gossip_arguments)
        self._release_gossip = _DeterministicGossip[
            TaskProtocolMessage, TaskProtocolMessageId
        ](**gossip_arguments)
        self._completion_gossip = _DeterministicGossip[
            TaskProtocolMessage, TaskProtocolMessageId
        ](**gossip_arguments)

    def deliver_claims(
        self,
        claims: Iterable[TaskClaim],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> TaskProtocolDeliveryBatch:
        """Deliver owner publications without refreshing duplicate evidence."""

        messages = tuple(claims)
        if not all(isinstance(message, TaskClaim) for message in messages):
            raise TypeError("deliver_claims accepts only TaskClaim messages")
        return self._deliver(
            messages,
            timestamp,
            receiving_agent_ids=receiving_agent_ids,
            gossip=self._claim_gossip,
        )

    def deliver_releases(
        self,
        releases: Iterable[TaskClaimRelease],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> TaskProtocolDeliveryBatch:
        """Deliver exact-claim tombstones from their releasing owners."""

        messages = tuple(releases)
        if not all(isinstance(message, TaskClaimRelease) for message in messages):
            raise TypeError("deliver_releases accepts only TaskClaimRelease messages")
        return self._deliver(
            messages,
            timestamp,
            receiving_agent_ids=receiving_agent_ids,
            gossip=self._release_gossip,
        )

    def deliver_completions(
        self,
        completions: Iterable[TaskCompletionEvidence],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None = None,
    ) -> TaskProtocolDeliveryBatch:
        """Deliver self-contained terminal evidence from its claim owner."""

        messages = tuple(completions)
        if not all(isinstance(message, TaskCompletionEvidence) for message in messages):
            raise TypeError(
                "deliver_completions accepts only TaskCompletionEvidence messages"
            )
        return self._deliver(
            messages,
            timestamp,
            receiving_agent_ids=receiving_agent_ids,
            gossip=self._completion_gossip,
        )

    def _deliver(
        self,
        messages: tuple[TaskProtocolMessage, ...],
        timestamp: float,
        *,
        receiving_agent_ids: Iterable[int] | None,
        gossip: _DeterministicGossip[TaskProtocolMessage, TaskProtocolMessageId],
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
            self._validate_task_message(message)
        gossip_batch = gossip.disseminate(
            ordered_messages,
            self._graph,
            receivers,
            apply=lambda receiver_agent_id, message: self._apply(
                self._stores[receiver_agent_id],
                message,
                timestamp,
            ),
        )
        self._last_timestamp = timestamp
        return TaskProtocolDeliveryBatch(
            attempted=gossip_batch.attempted,
            delivered=gossip_batch.delivered,
            undelivered=gossip_batch.undelivered,
            receipts=tuple(
                TaskProtocolReceipt(
                    receiver_agent_id=delivery.receiver_agent_id,
                    source_agent_id=delivery.origin_agent_id,
                    forwarder_agent_id=delivery.forwarder_agent_id,
                    hop_count=delivery.hop_count,
                    message_id=delivery.message_id,
                    task_id=self._identity_claim(delivery.message).task_id,
                    claim_id=self._identity_claim(delivery.message).claim_id,
                    epoch=self._identity_claim(delivery.message).epoch,
                    kind=self._message_kind(delivery.message),
                    changed=delivery.changed,
                )
                for delivery in gossip_batch.deliveries
            ),
            forwarded=gossip_batch.forwarded,
            duplicates_suppressed=gossip_batch.duplicates_suppressed,
            duplicate_source_publications=(gossip_batch.duplicate_source_publications),
            duplicate_route_suppressions=gossip_batch.duplicate_route_suppressions,
            inactive_endpoint_deferrals=gossip_batch.inactive_endpoint_deferrals,
        )

    def seen_message_ids(
        self, receiver_agent_id: int
    ) -> frozenset[TaskProtocolMessageId]:
        """Return structural task-message IDs already learned by one UAV."""

        return (
            self._claim_gossip.seen_message_ids(receiver_agent_id)
            | self._release_gossip.seen_message_ids(receiver_agent_id)
            | self._completion_gossip.seen_message_ids(receiver_agent_id)
        )

    def _receivers(self, supplied_agent_ids: Iterable[int] | None) -> frozenset[int]:
        if supplied_agent_ids is None:
            return self._agent_ids
        receivers = frozenset(supplied_agent_ids)
        unknown_receivers = receivers - self._agent_ids
        if unknown_receivers:
            raise ValueError(f"unknown receiver UAVs: {sorted(unknown_receivers)}")
        return receivers

    def _validate_task_message(self, message: TaskProtocolMessage) -> None:
        if isinstance(message, TaskClaimRelease):
            claims = (
                (message.losing_claim,)
                if message.winning_claim is None
                else (message.losing_claim, message.winning_claim)
            )
        elif isinstance(message, TaskCompletionEvidence):
            claims = (message.claim,)
        else:
            claims = (message,)

        for claim in claims:
            if claim.task_id not in self._task_ids:
                raise ValueError(f"unknown task-message task {claim.task_id}")
            if claim.owner_agent_id not in self._agent_ids:
                raise ValueError(
                    f"unknown task-message source UAV {claim.owner_agent_id}"
                )
            if (
                claim.freshness_timeout != self._freshness_timeout
                or claim.lease_timeout != self._lease_timeout
            ):
                raise ValueError("task-message lease policy does not match transport")

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
    def _message_kind(message: TaskProtocolMessage) -> TaskProtocolMessageKind:
        if isinstance(message, TaskClaimRelease):
            return TaskProtocolMessageKind.RELEASE
        if isinstance(message, TaskCompletionEvidence):
            return TaskProtocolMessageKind.COMPLETION
        return TaskProtocolMessageKind.CLAIM

    @classmethod
    def _message_id(cls, message: TaskProtocolMessage) -> TaskProtocolMessageId:
        return TaskProtocolMessageId(
            kind=cls._message_kind(message),
            claim_id=cls._identity_claim(message).claim_id,
        )

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
    "FailureProtocolMessageId",
    "FailureProtocolMessageKind",
    "FailureProtocolReceipt",
    "PeerStateTransport",
    "ProtocolDeliveryBatch",
    "TaskClaimTransport",
    "TaskProtocolDeliveryBatch",
    "TaskProtocolMessageId",
    "TaskProtocolMessageKind",
    "TaskProtocolReceipt",
]
