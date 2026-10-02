"""One UAV's failure-detector replica, for a node that runs in its own process.

:class:`~eudis_swarm.failure_manager.FailureManager` holds every replica of a
simulated swarm side by side and is bound to all of their peer-state stores at
once. A field node has exactly one store, so it cannot use that class without
inventing the others. :class:`LocalFailureDetector` is the same protocol for a
single receiver, rule for rule:

* a vote is proposed only from this receiver's own silence evidence
  (``local_now - received_at > heartbeat_timeout``, then
  :meth:`~eudis_swarm.peer_state.PeerStateStore.observe_silence`, then status
  ``SILENT``) -- silence alone never declares anything;
* a delivered vote lands in this receiver's mailbox stamped with *its* receipt
  time, idempotent per voter and ordered only within the vote's own clock
  domains;
* a declaration needs the local vote plus a quorum
  ``max(2, floor((N-1)/2) + 1)`` of matching votes received within
  ``heartbeat_timeout``;
* a delivered certificate is applied through
  :meth:`~eudis_swarm.peer_state.PeerStateStore.apply_failure_declaration`,
  which validates the quorum and refuses a certificate that first-hand contact
  has already outranked;
* a declaration is belief only. Nothing here stops a peer moving, transmitting
  or holding its task, and first-hand contact retracts it
  (:meth:`forget`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .failure_manager import FailureDeclaration, FailureVote
from .peer_state import PeerStateStore, PeerStatus
from .validation import validate_positive_real


@dataclass(frozen=True, slots=True)
class _HeldVote:
    vote: FailureVote
    received_at: float


def required_votes(agent_count: int) -> int:
    """Return the strict-majority quorum the simulator uses for ``N`` UAVs."""

    return max(2, (agent_count - 1) // 2 + 1)


class LocalFailureDetector:
    """Run the vote/quorum/declaration protocol for this receiver only."""

    def __init__(
        self,
        peer_state_store: PeerStateStore,
        *,
        agent_ids: Iterable[int],
        heartbeat_timeout: float,
    ) -> None:
        self._store = peer_state_store
        self._owner = peer_state_store.owner_agent_id
        agent_id_tuple = tuple(sorted(agent_ids))
        expected_peers = tuple(a for a in agent_id_tuple if a != self._owner)
        if self._owner not in agent_id_tuple:
            raise ValueError("detector agent_ids must include its owner")
        if peer_state_store.peer_agent_ids != expected_peers:
            raise ValueError("the detector store must contain every other UAV")
        self._agent_ids = frozenset(agent_id_tuple)
        self._heartbeat_timeout = validate_positive_real(
            heartbeat_timeout, name="heartbeat_timeout"
        )
        self._required_votes = required_votes(len(agent_id_tuple))
        # suspected -> voter -> latest vote generation, as this receiver got it.
        self._mailbox: dict[int, dict[int, _HeldVote]] = {}
        # certificates this receiver originated, re-offered until withdrawn.
        self._declarations: dict[int, FailureDeclaration] = {}

    @property
    def required_votes(self) -> int:
        return self._required_votes

    @property
    def heartbeat_timeout(self) -> float:
        return self._heartbeat_timeout

    def propose_votes(self, timestamp: float) -> tuple[FailureVote, ...]:
        """Vote against every peer this receiver itself finds silent."""

        votes: list[FailureVote] = []
        for suspected in self._store.peer_agent_ids:
            observation = self._store.observation_for(suspected)
            if observation is None:
                continue
            if timestamp - observation.received_at <= self._heartbeat_timeout:
                continue
            self._store.observe_silence(
                suspected,
                timestamp=timestamp,
                silent_after=self._heartbeat_timeout,
            )
            if self._store.status_for(suspected) is not PeerStatus.SILENT:
                continue
            vote = FailureVote(
                voter_agent_id=self._owner,
                suspected_agent_id=suspected,
                created_at=timestamp,
                last_heartbeat=observation.snapshot.timestamp,
                last_heard_at=observation.received_at,
                task_id=observation.snapshot.current_task,
            )
            self.record_vote(vote, received_at=timestamp)
            votes.append(vote)
        return tuple(votes)

    def record_vote(self, vote: FailureVote, *, received_at: float) -> bool:
        """Put one delivered vote in this receiver's mailbox."""

        for agent_id in (vote.voter_agent_id, vote.suspected_agent_id):
            if agent_id not in self._agent_ids:
                raise ValueError(f"vote names unknown UAV {agent_id}")
        if vote.suspected_agent_id == self._owner:
            # a vote against this receiver is not evidence it can act on.
            return False
        by_voter = self._mailbox.setdefault(vote.suspected_agent_id, {})
        previous = by_voter.get(vote.voter_agent_id)
        if previous is not None:
            if previous.vote == vote:
                return False
            if _generation(vote) < _generation(previous.vote):
                return False
            if _generation(vote) == _generation(previous.vote):
                raise ValueError(
                    "one failure-vote generation cannot carry conflicting evidence"
                )
        by_voter[vote.voter_agent_id] = _HeldVote(vote, received_at)
        return True

    def detect(self, timestamp: float) -> tuple[FailureDeclaration, ...]:
        """Declare each silent peer for which a matching quorum is held."""

        declared: list[FailureDeclaration] = []
        for suspected in self._store.peer_agent_ids:
            observation = self._store.observation_for(suspected)
            if observation is None:
                continue
            if timestamp - observation.received_at <= self._heartbeat_timeout:
                continue
            self._store.observe_silence(
                suspected,
                timestamp=timestamp,
                silent_after=self._heartbeat_timeout,
            )
            if self._store.status_for(suspected) is not PeerStatus.SILENT:
                continue
            votes = self._mailbox.get(suspected, {})
            matching = tuple(
                sorted(
                    voter
                    for voter, held in votes.items()
                    if held.vote.last_heartbeat == observation.snapshot.timestamp
                    and held.vote.task_id == observation.snapshot.current_task
                    and 0.0 <= timestamp - held.received_at <= self._heartbeat_timeout
                )
            )
            if self._owner not in matching or len(matching) < self._required_votes:
                continue
            declaration = FailureDeclaration(
                agent_id=suspected,
                declarer_agent_id=self._owner,
                detected_at=timestamp,
                last_heartbeat=observation.snapshot.timestamp,
                task_id=observation.snapshot.current_task,
                voter_agent_ids=matching,
                required_votes=self._required_votes,
            )
            self._store.apply_failure_declaration(
                suspected,
                voter_agent_ids=matching,
                required_votes=self._required_votes,
                evidence_last_heartbeat=declaration.last_heartbeat,
            )
            self._declarations[suspected] = declaration
            declared.append(declaration)
        return tuple(declared)

    def receive_declaration(self, declaration: FailureDeclaration) -> bool:
        """Apply a delivered certificate; ``False`` if it does not apply here.

        Raises ``ValueError``/``KeyError`` for a certificate whose quorum or
        participants do not validate, so the caller can count it as rejected.
        """

        if declaration.agent_id == self._owner:
            # this receiver is plainly alive; a certificate about it changes
            # nothing it believes, though it is still relayed onward.
            return False
        if declaration.declarer_agent_id not in self._agent_ids:
            raise ValueError("declaration names an unknown declarer")
        return self._store.apply_failure_declaration(
            declaration.agent_id,
            voter_agent_ids=declaration.voter_agent_ids,
            required_votes=declaration.required_votes,
            evidence_last_heartbeat=declaration.last_heartbeat,
        )

    def declarations_for_broadcast(self) -> tuple[FailureDeclaration, ...]:
        return tuple(self._declarations[key] for key in sorted(self._declarations))

    def forget(self, peer_agent_id: int) -> None:
        """Drop local certificates and votes after first-hand contact."""

        self._declarations.pop(peer_agent_id, None)
        self._mailbox.pop(peer_agent_id, None)


def _generation(vote: FailureVote) -> tuple[float, float, float]:
    return (vote.last_heartbeat, vote.last_heard_at, vote.created_at)


__all__ = ["LocalFailureDetector", "required_votes"]
