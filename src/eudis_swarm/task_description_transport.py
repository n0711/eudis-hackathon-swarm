"""Bounded store-and-forward gossip of accepted stationary task descriptions.

A round snapshots the senders first. A receiver can forward new knowledge only
in the next round, so no hop bypasses the current graph. Failed endpoints never
participate. Successful routes are acknowledged and suppressed permanently for
these immutable, mission-lifetime descriptions; blocked routes can retry later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .communication import CommunicationGraph
from .task_discovery import ReceiverTaskDiscovery
from .validation import validate_nonnegative_real, validate_timestamp


@dataclass(frozen=True)
class TaskDescriptionReceipt:
    forwarder_agent_id: int
    receiver_agent_id: int
    task_id: int
    timestamp: float
    delivered: bool
    accepted: bool


class TaskDescriptionTransport:
    def __init__(
        self, graph: CommunicationGraph, stores: Mapping[int, ReceiverTaskDiscovery]
    ) -> None:
        self.graph = graph
        self.stores = dict(stores)
        self._acknowledged: set[tuple[int, int, int]] = set()
        self._last_timestamp = 0.0

    def exchange(
        self, *, now: float, participants: Sequence[int]
    ) -> tuple[TaskDescriptionReceipt, ...]:
        now = validate_nonnegative_real(now, name="description exchange time")
        validate_timestamp(
            now, previous=self._last_timestamp, name="description exchange time"
        )
        self._last_timestamp = now
        active = tuple(sorted(set(participants)))
        if not set(active) <= self.stores.keys():
            raise ValueError("unknown task description endpoint")
        outgoing = tuple(
            (sender, task.description)
            for sender in active
            for task in self.stores[sender].tasks
        )
        receipts = []
        for sender, description in outgoing:
            for receiver in active:
                route = (sender, receiver, description.objective.task_id)
                if sender == receiver or route in self._acknowledged:
                    continue
                delivered = self.graph.can_deliver(sender, receiver)
                accepted = False
                if delivered:
                    accepted = self.stores[receiver].receive_description(
                        description, received_at=now, forwarder_agent_id=sender
                    )
                    self._acknowledged.add(route)
                receipts.append(
                    TaskDescriptionReceipt(
                        sender,
                        receiver,
                        description.objective.task_id,
                        now,
                        delivered,
                        accepted,
                    )
                )
        return tuple(receipts)
