"""Define immutable receiver-local events shared with trace tooling.

The event records describe communication, peer knowledge, and task ownership
without granting any observer data back to an agent decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CommunicationEventKind(str, Enum):
    """Observable communication transitions kept separate from mission events."""

    NETWORK_INITIALIZED = "NETWORK_INITIALIZED"
    FAULT_STARTED = "FAULT_STARTED"
    FAULT_ENDED = "FAULT_ENDED"
    LINK_LOST = "LINK_LOST"
    LINK_RESTORED = "LINK_RESTORED"
    NETWORK_PARTITIONED = "NETWORK_PARTITIONED"
    NETWORK_RECONNECTED = "NETWORK_RECONNECTED"
    AGENT_UNREACHABLE = "AGENT_UNREACHABLE"
    AGENT_REACHABLE = "AGENT_REACHABLE"


@dataclass(frozen=True, slots=True)
class CommunicationEvent:
    kind: CommunicationEventKind
    timestamp: float
    agent_id: int | None = None
    peer_agent_id: int | None = None
    component_count: int | None = None


class PeerStateEventKind(str, Enum):
    """Meaningful receiver-local peer-knowledge transitions."""

    STALE = "STALE"
    REFRESHED = "REFRESHED"


@dataclass(frozen=True, slots=True)
class PeerStateEvent:
    kind: PeerStateEventKind
    timestamp: float
    observer_agent_id: int
    peer_agent_id: int


class TaskClaimEventKind(str, Enum):
    """Observable transitions produced by one receiver-local ownership machine."""

    CLAIM_CREATED = "CLAIM_CREATED"
    CLAIM_RENEWED = "CLAIM_RENEWED"
    CLAIM_RECEIVED = "CLAIM_RECEIVED"
    CLAIM_STALE = "CLAIM_STALE"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    CONTESTED = "CONTESTED"
    RECONCILIATION_SELECTED = "RECONCILIATION_SELECTED"
    CLAIM_RELEASED = "CLAIM_RELEASED"
    RELEASE_RECEIVED = "RELEASE_RECEIVED"
    COMPLETION_CREATED = "COMPLETION_CREATED"
    COMPLETION_RECEIVED = "COMPLETION_RECEIVED"


@dataclass(frozen=True, slots=True)
class TaskClaimEvent:
    """Record one local claim transition with replayable protocol identities."""

    kind: TaskClaimEventKind
    timestamp: float
    observer_agent_id: int
    task_id: int
    owner_agent_id: int | None = None
    source_agent_id: int | None = None
    claim_id: str | None = None
    epoch: int | None = None
    winner_agent_id: int | None = None
    winner_claim_id: str | None = None


__all__ = [
    "CommunicationEvent",
    "CommunicationEventKind",
    "PeerStateEvent",
    "PeerStateEventKind",
    "TaskClaimEvent",
    "TaskClaimEventKind",
]
