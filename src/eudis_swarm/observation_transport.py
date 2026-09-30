"""One-hop observation publications over the simulator's current link model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .communication import CommunicationGraph
from .observation_replay import ReceiverObservations, Record
from .validation import (
    validate_nonnegative_real,
    validate_positive_integer,
    validate_timestamp,
)


@dataclass(frozen=True)
class ObservationPublication:
    source_agent_id: int
    published_at: float
    record: Record

    def __post_init__(self) -> None:
        validate_positive_integer(self.source_agent_id, name="observation source agent")
        timestamp = validate_nonnegative_real(
            self.published_at, name="observation publication time"
        )
        object.__setattr__(self, "published_at", round(timestamp, 12))


@dataclass(frozen=True)
class ObservationReceipt:
    source_agent_id: int
    receiver_agent_id: int
    timestamp: float
    delivered: bool
    accepted: bool


class ObservationTransport:
    """No queues, forwarding, or retry: recovery needs a new publication.

    Local sensor ingestion bypasses the radio, but not a failed source endpoint.
    Drops never touch receiver freshness. Source identity is bound by Simulation.
    """

    def __init__(
        self, graph: CommunicationGraph, stores: Mapping[int, ReceiverObservations]
    ) -> None:
        self.graph = graph
        self.stores = dict(stores)

    def deliver(
        self,
        publication: ObservationPublication,
        *,
        now: float,
        participants: Sequence[int],
    ) -> tuple[ObservationReceipt, ...]:
        validate_timestamp(
            now, previous=publication.published_at, name="observation delivery time"
        )
        if publication.source_agent_id not in self.stores:
            raise ValueError("unknown observation source agent")
        active = frozenset(participants)
        if not active <= self.stores.keys():
            raise ValueError("unknown observation receiver")
        receipts = []
        for receiver in sorted(active):
            delivered = publication.source_agent_id in active and (
                receiver == publication.source_agent_id
                or self.graph.can_deliver(publication.source_agent_id, receiver)
            )
            accepted = delivered and self.stores[receiver].receive(
                publication.record, received_at=now
            )
            receipts.append(
                ObservationReceipt(
                    publication.source_agent_id, receiver, now, delivered, accepted
                )
            )
        return tuple(receipts)
