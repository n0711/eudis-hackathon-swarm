"""Run isolated local failure-detector replicas over delivered peer evidence.

The protocol turns corroborated suspicion votes into declarations while keeping
silence and link loss non-authoritative on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .peer_state import PeerStateStore, PeerStatus
from .validation import (
    validate_positive_integer,
    validate_positive_real,
    validate_timestamp,
)


@dataclass(frozen=True, slots=True)
class FailureVote:
    """One UAV's suspicion based only on its own delivered peer evidence."""

    voter_agent_id: int
    suspected_agent_id: int
    created_at: float
    last_heartbeat: float
    last_heard_at: float
    task_id: int | None

    def __post_init__(self) -> None:
        validate_positive_integer(self.voter_agent_id, name="voter_agent_id")
        validate_positive_integer(self.suspected_agent_id, name="suspected_agent_id")
        if self.voter_agent_id == self.suspected_agent_id:
            raise ValueError("an agent cannot vote for its own failure")
        last_heartbeat = validate_timestamp(
            self.last_heartbeat, name="last heartbeat timestamp"
        )
        last_heard_at = validate_timestamp(
            self.last_heard_at,
            previous=last_heartbeat,
            name="last-heard timestamp",
        )
        validate_timestamp(
            self.created_at,
            previous=last_heard_at,
            name="failure-vote timestamp",
        )
        if self.task_id is not None:
            validate_positive_integer(self.task_id, name="task_id")


@dataclass(frozen=True, slots=True)
class FailureDeclaration:
    """A local detector result backed by a validated quorum of delivered votes."""

    agent_id: int
    declarer_agent_id: int
    detected_at: float
    last_heartbeat: float
    task_id: int | None
    voter_agent_ids: tuple[int, ...]
    required_votes: int

    def __post_init__(self) -> None:
        validate_positive_integer(self.agent_id, name="agent_id")
        validate_positive_integer(self.declarer_agent_id, name="declarer_agent_id")
        if self.agent_id == self.declarer_agent_id:
            raise ValueError("an agent cannot declare its own failure")
        validate_timestamp(self.last_heartbeat, name="last heartbeat timestamp")
        validate_timestamp(
            self.detected_at,
            previous=self.last_heartbeat,
            name="failure-declaration timestamp",
        )
        if self.task_id is not None:
            validate_positive_integer(self.task_id, name="task_id")
        validate_positive_integer(self.required_votes, name="required_votes")
        canonical_voters = tuple(sorted(set(self.voter_agent_ids)))
        for voter_agent_id in canonical_voters:
            validate_positive_integer(voter_agent_id, name="voter_agent_id")
        if self.voter_agent_ids != canonical_voters:
            raise ValueError("failure-declaration voters must be unique and sorted")
        if self.agent_id in canonical_voters:
            raise ValueError("a suspected agent cannot vote for its own failure")
        if self.declarer_agent_id not in canonical_voters:
            raise ValueError("the declaring agent must contribute local evidence")
        if len(canonical_voters) < self.required_votes:
            raise ValueError("failure declaration does not carry a valid quorum")


# the legacy name remains import-compatible while its semantics are now explicit.
HeartbeatTimeout = FailureDeclaration


class FailureManager:
    """Coordinate local detector replicas without reading authoritative agents."""

    def __init__(
        self,
        heartbeat_timeout: float,
        peer_state_stores: Mapping[int, PeerStateStore] | None = None,
    ) -> None:
        self.heartbeat_timeout = validate_positive_real(
            heartbeat_timeout, name="heartbeat_timeout"
        )
        self._stores: dict[int, PeerStateStore] = {}
        self._agent_ids: tuple[int, ...] = ()
        self._agent_id_set: frozenset[int] = frozenset()
        # each receiver owns an isolated vote mailbox in the protocol model.
        self._votes_by_receiver: dict[int, dict[int, dict[int, FailureVote]]] = {}
        # locally originated certificates persist so dropped broadcasts can retry.
        self._declarations_by_declarer: dict[int, dict[int, FailureDeclaration]] = {}
        self._reported_agent_ids: set[int] = set()
        self._last_timestamp: float | None = None
        if peer_state_stores is not None:
            self.bind_peer_state_stores(peer_state_stores)

    @property
    def configured(self) -> bool:
        return bool(self._stores)

    @property
    def required_votes(self) -> int:
        self._require_configured()
        # a strict majority is required, and one observer never counts as consensus.
        possible_voters = len(self._agent_ids) - 1
        return max(2, possible_voters // 2 + 1)

    @property
    def declared_agent_ids(self) -> frozenset[int]:
        """Return declarations already handed to the world-state observer."""

        return frozenset(self._reported_agent_ids)

    def retract_declaration(self, agent_id: int) -> bool:
        """Forget a declaration contradicted by first-hand contact."""

        self._require_configured()
        self._require_agent(agent_id, name="agent_id")
        if agent_id not in self._reported_agent_ids:
            return False
        self._reported_agent_ids.discard(agent_id)
        self._declarations_by_declarer = {
            declarer_agent_id: {
                suspected_agent_id: declaration
                for suspected_agent_id, declaration in declarations.items()
                if suspected_agent_id != agent_id
            }
            for declarer_agent_id, declarations in self._declarations_by_declarer.items()
        }
        for votes_by_target in self._votes_by_receiver.values():
            votes_by_target.pop(agent_id, None)
        return True

    def declarations_for_broadcast(
        self,
        *,
        participating_agent_ids: Iterable[int] | None = None,
    ) -> tuple[FailureDeclaration, ...]:
        """Return persistent local certificates for graph-mediated retries."""

        participants = self._participants(participating_agent_ids)
        return tuple(
            declaration
            for declarer_agent_id in participants
            for _, declaration in sorted(
                self._declarations_by_declarer[declarer_agent_id].items()
            )
        )

    def recognizes_declaration(self, declaration: FailureDeclaration) -> bool:
        """Confirm that the exact certificate came from a configured replica."""

        known = self._declarations_by_declarer.get(
            declaration.declarer_agent_id, {}
        ).get(declaration.agent_id)
        return known == declaration

    def bind_peer_state_stores(
        self, peer_state_stores: Mapping[int, PeerStateStore]
    ) -> None:
        """Bind exactly one receiver-local store to every detector replica."""

        if self._stores:
            raise RuntimeError("failure manager peer-state stores are already bound")
        if not peer_state_stores:
            raise ValueError("failure manager requires at least one peer-state store")
        agent_ids = tuple(sorted(peer_state_stores))
        agent_id_set = frozenset(agent_ids)
        for owner_agent_id, store in peer_state_stores.items():
            if owner_agent_id != store.owner_agent_id:
                raise ValueError("peer-state store key must match its owner")
            expected_peers = tuple(
                agent_id for agent_id in agent_ids if agent_id != owner_agent_id
            )
            if store.peer_agent_ids != expected_peers:
                raise ValueError("each detector store must contain every other UAV")
        self._stores = dict(peer_state_stores)
        self._agent_ids = agent_ids
        self._agent_id_set = agent_id_set
        self._votes_by_receiver = {agent_id: {} for agent_id in agent_ids}
        self._declarations_by_declarer = {agent_id: {} for agent_id in agent_ids}

    def propose_votes(
        self,
        timestamp: float,
        *,
        participating_agent_ids: Iterable[int] | None = None,
    ) -> tuple[FailureVote, ...]:
        """Create retryable local votes from continuously reachable stale evidence."""

        timestamp = self._observe_time(timestamp)
        participants = self._participants(participating_agent_ids)
        votes: list[FailureVote] = []
        for voter_agent_id in participants:
            store = self._stores[voter_agent_id]
            for suspected_agent_id in store.peer_agent_ids:
                observation = store.observation_for(suspected_agent_id)
                if observation is None:
                    continue
                # silence is the only evidence available.  A voter cannot tell
                # a jammed peer from a destroyed one, so this deliberately can
                # suspect a healthy peer on the far side of a partition.
                if timestamp - observation.received_at <= self.heartbeat_timeout:
                    continue
                store.observe_silence(
                    suspected_agent_id,
                    timestamp=timestamp,
                    silent_after=self.heartbeat_timeout,
                )
                if store.status_for(suspected_agent_id) is not PeerStatus.SILENT:
                    continue
                vote = FailureVote(
                    voter_agent_id=voter_agent_id,
                    suspected_agent_id=suspected_agent_id,
                    created_at=timestamp,
                    last_heartbeat=observation.snapshot.timestamp,
                    last_heard_at=observation.received_at,
                    task_id=observation.snapshot.current_task,
                )
                # the local mailbox is idempotent by voter, while retransmission
                # lets the same evidence cross a link that was previously down.
                self.record_vote(voter_agent_id, vote)
                votes.append(vote)
        return tuple(votes)

    def record_vote(self, receiver_agent_id: int, vote: FailureVote) -> None:
        """Put a delivered vote only into the named receiver's local mailbox."""

        self._require_configured()
        self._require_agent(receiver_agent_id, name="receiver_agent_id")
        self._require_agent(vote.voter_agent_id, name="voter_agent_id")
        self._require_agent(vote.suspected_agent_id, name="suspected_agent_id")
        if receiver_agent_id == vote.suspected_agent_id:
            return
        votes_for_target = self._votes_by_receiver[receiver_agent_id].setdefault(
            vote.suspected_agent_id, {}
        )
        previous = votes_for_target.get(vote.voter_agent_id)
        if previous is not None:
            if vote.created_at < previous.created_at:
                raise ValueError("delivered failure votes must not move backwards")
            if vote.last_heartbeat < previous.last_heartbeat:
                raise ValueError("failure-vote evidence must not move backwards")
        votes_for_target[vote.voter_agent_id] = vote

    def detect_declarations(
        self,
        timestamp: float,
        *,
        participating_agent_ids: Iterable[int] | None = None,
    ) -> tuple[FailureDeclaration, ...]:
        """Let each local replica declare only after receiving a vote quorum."""

        timestamp = self._observe_time(timestamp)
        participants = self._participants(participating_agent_ids)
        declarations: list[FailureDeclaration] = []
        required_votes = self.required_votes
        for declarer_agent_id in participants:
            store = self._stores[declarer_agent_id]
            for suspected_agent_id in store.peer_agent_ids:
                observation = store.observation_for(suspected_agent_id)
                if observation is None:
                    continue
                if timestamp - observation.received_at <= self.heartbeat_timeout:
                    continue
                store.observe_silence(
                    suspected_agent_id,
                    timestamp=timestamp,
                    silent_after=self.heartbeat_timeout,
                )
                if store.status_for(suspected_agent_id) is not PeerStatus.SILENT:
                    continue
                votes = self._votes_by_receiver[declarer_agent_id].get(
                    suspected_agent_id, {}
                )
                matching_voters = tuple(
                    sorted(
                        voter_agent_id
                        for voter_agent_id, vote in votes.items()
                        if vote.last_heartbeat == observation.snapshot.timestamp
                        and vote.task_id == observation.snapshot.current_task
                        and vote.created_at <= timestamp
                        and timestamp - vote.created_at <= self.heartbeat_timeout
                    )
                )
                if (
                    declarer_agent_id not in matching_voters
                    or len(matching_voters) < required_votes
                ):
                    continue
                declaration = FailureDeclaration(
                    agent_id=suspected_agent_id,
                    declarer_agent_id=declarer_agent_id,
                    detected_at=timestamp,
                    last_heartbeat=observation.snapshot.timestamp,
                    task_id=observation.snapshot.current_task,
                    voter_agent_ids=matching_voters,
                    required_votes=required_votes,
                )
                # the declaring replica learns its own result locally; peers
                # still require graph-mediated declaration delivery.
                store.apply_failure_declaration(
                    suspected_agent_id,
                    voter_agent_ids=matching_voters,
                    required_votes=required_votes,
                )
                self._declarations_by_declarer[declarer_agent_id][
                    suspected_agent_id
                ] = declaration
                # observer-level deduplication does not change any replica's
                # local status, mailbox contents, or certificate retries.
                if suspected_agent_id not in self._reported_agent_ids:
                    self._reported_agent_ids.add(suspected_agent_id)
                    declarations.append(declaration)
        return tuple(declarations)

    def _participants(
        self, supplied_agent_ids: Iterable[int] | None
    ) -> tuple[int, ...]:
        self._require_configured()
        if supplied_agent_ids is None:
            return self._agent_ids
        participants = tuple(sorted(set(supplied_agent_ids)))
        for agent_id in participants:
            self._require_agent(agent_id, name="participating_agent_id")
        return participants

    def _observe_time(self, timestamp: float) -> float:
        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="failure-protocol timestamp",
        )
        self._last_timestamp = timestamp
        return timestamp

    def _require_configured(self) -> None:
        if not self._stores:
            raise RuntimeError("failure manager requires bound peer-state stores")

    def _require_agent(self, agent_id: int, *, name: str) -> None:
        if (
            not isinstance(agent_id, int)
            or isinstance(agent_id, bool)
            or agent_id not in self._agent_id_set
        ):
            raise KeyError(f"unknown {name} {agent_id}")


__all__ = [
    "FailureDeclaration",
    "FailureManager",
    "FailureVote",
    "HeartbeatTimeout",
]
