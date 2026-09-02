"""Deliver peer evidence through the same deterministic one-hop network model.

Heartbeat snapshots, failure votes, and declarations reach only receivers whose
current link and local execution state permit delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .agent import Heartbeat
from .communication import CommunicationGraph
from .failure_manager import FailureDeclaration, FailureManager, FailureVote
from .peer_state import PeerStateStore
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
        self._last_link_state_timestamp: float | None = None
        self._last_protocol_timestamp: float | None = None

    def synchronize_link_evidence(
        self,
        timestamp: float,
        *,
        observing_agent_ids: Iterable[int] | None = None,
    ) -> None:
        """Expose direct delivery results only to participating local observers."""

        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_link_state_timestamp,
            name="link-evidence timestamp",
        )
        if not self._graph.initialized:
            raise RuntimeError(
                "communication graph must be initialized before link evidence"
            )
        observers = self._receivers(observing_agent_ids)
        for receiver_agent_id, store in sorted(self._stores.items()):
            if receiver_agent_id not in observers:
                continue
            for peer_agent_id in store.peer_agent_ids:
                # this adapter exposes only a local yes/no delivery fact; it
                # never copies positions, components, or other world truth.
                store.observe_link_state(
                    peer_agent_id,
                    reachable=self._graph.can_deliver(peer_agent_id, receiver_agent_id),
                    timestamp=timestamp,
                )
        self._last_link_state_timestamp = timestamp

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


__all__ = ["DeliveryBatch", "PeerStateTransport", "ProtocolDeliveryBatch"]
