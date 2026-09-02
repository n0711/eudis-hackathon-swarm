"""Store the evidence that one UAV has actually received about its peers.

Freshness, link reachability, and protocol-backed failure are kept separate so
that silence can never silently become physical failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .agent import Heartbeat
from .validation import (
    validate_positive_integer,
    validate_positive_real,
    validate_timestamp,
)


class PeerKnowledgeState(str, Enum):
    """Freshness of one receiver's last successfully delivered peer snapshot."""

    UNKNOWN = "UNKNOWN"
    FRESH = "FRESH"
    STALE = "STALE"


class PeerStatus(str, Enum):
    """Receiver-local interpretation of the available peer evidence."""

    HEARD = "HEARD"
    SILENT = "SILENT"
    UNREACHABLE = "UNREACHABLE"
    DECLARED_FAILED = "DECLARED_FAILED"


@dataclass(frozen=True, slots=True)
class PeerObservation:
    """An immutable delivered snapshot and its receiver-local arrival time."""

    snapshot: Heartbeat
    received_at: float

    def __post_init__(self) -> None:
        received_at = validate_timestamp(self.received_at, name="received_at")
        if received_at < self.snapshot.timestamp:
            raise ValueError("received_at cannot precede the snapshot timestamp")


class PeerStateStore:
    """Keep one UAV's observations, link evidence, and received declarations."""

    def __init__(
        self,
        owner_agent_id: int,
        peer_agent_ids: Iterable[int],
        stale_after: float,
    ) -> None:
        self._owner_agent_id = validate_positive_integer(
            owner_agent_id, name="owner_agent_id"
        )
        supplied_peer_ids = tuple(peer_agent_ids)
        for peer_agent_id in supplied_peer_ids:
            validate_positive_integer(peer_agent_id, name="peer_agent_id")
        if len(set(supplied_peer_ids)) != len(supplied_peer_ids):
            raise ValueError("peer_agent_ids must be unique")
        if owner_agent_id in supplied_peer_ids:
            raise ValueError("a peer-state store must not contain self-state")
        self._peer_agent_ids = tuple(sorted(supplied_peer_ids))
        self._peer_agent_id_set = frozenset(self._peer_agent_ids)
        self._stale_after = validate_positive_real(stale_after, name="stale_after")
        self._observations: dict[int, PeerObservation] = {}
        self._states = {
            peer_agent_id: PeerKnowledgeState.UNKNOWN
            for peer_agent_id in self._peer_agent_ids
        }
        self._link_reachable: dict[int, bool | None] = {
            peer_agent_id: None for peer_agent_id in self._peer_agent_ids
        }
        self._reachable_since: dict[int, float | None] = {
            peer_agent_id: None for peer_agent_id in self._peer_agent_ids
        }
        self._silent_peer_ids: set[int] = set()
        self._declared_failed_peer_ids: set[int] = set()
        self._last_timestamp: float | None = None

    @property
    def owner_agent_id(self) -> int:
        return self._owner_agent_id

    @property
    def peer_agent_ids(self) -> tuple[int, ...]:
        return self._peer_agent_ids

    @property
    def stale_after(self) -> float:
        return self._stale_after

    @property
    def stale_peer_ids(self) -> frozenset[int]:
        return frozenset(
            peer_agent_id
            for peer_agent_id, state in self._states.items()
            if state is PeerKnowledgeState.STALE
        )

    @property
    def silent_peer_ids(self) -> frozenset[int]:
        return frozenset(
            peer_agent_id
            for peer_agent_id in self._peer_agent_ids
            if self.status_for(peer_agent_id) is PeerStatus.SILENT
        )

    @property
    def unreachable_peer_ids(self) -> frozenset[int]:
        return frozenset(
            peer_agent_id
            for peer_agent_id in self._peer_agent_ids
            if self.status_for(peer_agent_id) is PeerStatus.UNREACHABLE
        )

    @property
    def declared_failed_peer_ids(self) -> frozenset[int]:
        return frozenset(self._declared_failed_peer_ids)

    def state_for(self, peer_agent_id: int) -> PeerKnowledgeState:
        self._require_peer(peer_agent_id)
        return self._states[peer_agent_id]

    def status_for(self, peer_agent_id: int) -> PeerStatus:
        """Interpret freshness and link evidence without consulting world truth."""

        self._require_peer(peer_agent_id)
        if peer_agent_id in self._declared_failed_peer_ids:
            return PeerStatus.DECLARED_FAILED
        if self._link_reachable[peer_agent_id] is False:
            return PeerStatus.UNREACHABLE
        if (
            peer_agent_id not in self._silent_peer_ids
            and self._states[peer_agent_id] is PeerKnowledgeState.FRESH
        ):
            return PeerStatus.HEARD
        return PeerStatus.SILENT

    def link_reachable_for(self, peer_agent_id: int) -> bool | None:
        """Return the latest local link evidence, or ``None`` before observation."""

        self._require_peer(peer_agent_id)
        return self._link_reachable[peer_agent_id]

    def reachable_since_for(self, peer_agent_id: int) -> float | None:
        """Return when the current uninterrupted reachable interval began."""

        self._require_peer(peer_agent_id)
        return self._reachable_since[peer_agent_id]

    def observation_for(self, peer_agent_id: int) -> PeerObservation | None:
        self._require_peer(peer_agent_id)
        return self._observations.get(peer_agent_id)

    @property
    def fresh_observations(self) -> tuple[PeerObservation, ...]:
        """Return raw freshness-qualified observations in stable peer-ID order."""

        return tuple(
            self._observations[peer_agent_id]
            for peer_agent_id in self._peer_agent_ids
            if self._states[peer_agent_id] is PeerKnowledgeState.FRESH
        )

    @property
    def heard_observations(self) -> tuple[PeerObservation, ...]:
        """Return observations whose complete local peer status is ``HEARD``."""

        return tuple(
            self._observations[peer_agent_id]
            for peer_agent_id in self._peer_agent_ids
            if self.status_for(peer_agent_id) is PeerStatus.HEARD
        )

    def receive(self, snapshot: Heartbeat, received_at: float) -> bool:
        """Store one delivered snapshot and report refresh-after-stale."""

        self._require_peer(snapshot.agent_id)
        received_at = validate_timestamp(
            received_at,
            previous=self._last_timestamp,
            name="peer-state receipt timestamp",
        )
        previous = self._observations.get(snapshot.agent_id)
        if previous is not None and snapshot.timestamp < previous.snapshot.timestamp:
            raise ValueError("peer snapshots must not move backwards")
        observation = PeerObservation(snapshot=snapshot, received_at=received_at)
        refreshed = self._states[snapshot.agent_id] is PeerKnowledgeState.STALE
        self._observations[snapshot.agent_id] = observation
        self._states[snapshot.agent_id] = PeerKnowledgeState.FRESH
        # a successful delivery is positive receiver-local link evidence.
        if self._link_reachable[snapshot.agent_id] is not True:
            self._reachable_since[snapshot.agent_id] = received_at
        self._link_reachable[snapshot.agent_id] = True
        self._silent_peer_ids.discard(snapshot.agent_id)
        self._last_timestamp = received_at
        return refreshed

    def observe_silence(
        self,
        peer_agent_id: int,
        *,
        timestamp: float,
        silent_after: float,
    ) -> bool:
        """Record local timeout evidence without declaring the peer failed."""

        self._require_peer(peer_agent_id)
        silent_after = validate_positive_real(silent_after, name="silent_after")
        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="peer-silence timestamp",
        )
        observation = self._observations.get(peer_agent_id)
        reachable_since = self._reachable_since[peer_agent_id]
        if (
            observation is None
            or self._link_reachable[peer_agent_id] is not True
            or reachable_since is None
        ):
            self._last_timestamp = timestamp
            return False
        evidence_start = max(observation.received_at, reachable_since)
        if timestamp - evidence_start <= silent_after:
            self._last_timestamp = timestamp
            return False
        newly_silent = peer_agent_id not in self._silent_peer_ids
        self._silent_peer_ids.add(peer_agent_id)
        self._last_timestamp = timestamp
        return newly_silent

    def observe_link_state(
        self, peer_agent_id: int, *, reachable: bool, timestamp: float
    ) -> None:
        """Record the local link layer's current delivery evidence for one peer."""

        self._require_peer(peer_agent_id)
        if not isinstance(reachable, bool):
            raise TypeError("reachable must be boolean")
        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="peer link-state timestamp",
        )
        previous_reachability = self._link_reachable[peer_agent_id]
        self._link_reachable[peer_agent_id] = reachable
        if reachable and previous_reachability is not True:
            # a restored link starts a new grace interval before suspicion.
            self._reachable_since[peer_agent_id] = timestamp
        elif not reachable:
            self._reachable_since[peer_agent_id] = None
        self._last_timestamp = timestamp

    def apply_failure_declaration(
        self,
        peer_agent_id: int,
        *,
        voter_agent_ids: Iterable[int],
        required_votes: int,
    ) -> None:
        """Apply only a declaration carrying a valid distributed vote quorum."""

        self._require_peer(peer_agent_id)
        validate_positive_integer(required_votes, name="required_votes")
        voters = tuple(voter_agent_ids)
        for voter_agent_id in voters:
            validate_positive_integer(voter_agent_id, name="voter_agent_id")
        unique_voters = frozenset(voters)
        known_agent_ids = self._peer_agent_id_set | {self._owner_agent_id}
        unknown_voters = unique_voters - known_agent_ids
        if unknown_voters:
            raise ValueError(
                f"failure declaration has unknown voters: {sorted(unknown_voters)}"
            )
        expected_votes = max(2, len(self._peer_agent_ids) // 2 + 1)
        if required_votes != expected_votes:
            raise ValueError("failure declaration uses the wrong quorum threshold")
        if peer_agent_id in unique_voters:
            raise ValueError("a suspected peer cannot vote for its own failure")
        if len(unique_voters) < required_votes:
            raise ValueError("failure declaration does not carry a valid quorum")
        self._declared_failed_peer_ids.add(peer_agent_id)

    def advance_time(self, timestamp: float) -> tuple[int, ...]:
        """Apply strict freshness expiry and return newly stale peer IDs."""

        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="peer-state timestamp",
        )
        newly_stale: list[int] = []
        for peer_agent_id in self._peer_agent_ids:
            if self._states[peer_agent_id] is not PeerKnowledgeState.FRESH:
                continue
            observation = self._observations[peer_agent_id]
            if timestamp - observation.received_at > self._stale_after:
                self._states[peer_agent_id] = PeerKnowledgeState.STALE
                self._silent_peer_ids.add(peer_agent_id)
                newly_stale.append(peer_agent_id)
        self._last_timestamp = timestamp
        return tuple(newly_stale)

    def _require_peer(self, peer_agent_id: int) -> None:
        if peer_agent_id not in self._peer_agent_id_set:
            raise KeyError(
                f"UAV {peer_agent_id} is not a peer of UAV {self._owner_agent_id}"
            )


__all__ = [
    "PeerKnowledgeState",
    "PeerObservation",
    "PeerStateStore",
    "PeerStatus",
]
