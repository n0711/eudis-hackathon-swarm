"""The single-receiver failure detector: silence votes, quorum declares, contact retracts."""

from __future__ import annotations

import pytest

from eudis_swarm.agent import AgentStatus, Heartbeat
from eudis_swarm.failure_manager import FailureDeclaration, FailureVote
from eudis_swarm.field_detector import LocalFailureDetector, required_votes
from eudis_swarm.peer_state import PeerStateStore, PeerStatus

AGENTS = (1, 2, 3, 4)


def _detector(owner: int = 1, timeout: float = 0.5):
    store = PeerStateStore(owner, [a for a in AGENTS if a != owner], 0.4)
    return store, LocalFailureDetector(
        store, agent_ids=AGENTS, heartbeat_timeout=timeout
    )


def _hear(store: PeerStateStore, peer: int, source_t: float, at: float) -> None:
    store.receive(Heartbeat(peer, (0.0, 0.0), AgentStatus.IDLE, None, source_t), at)


@pytest.mark.parametrize(
    ("agents", "quorum"), [(2, 2), (3, 2), (4, 2), (5, 3), (6, 3), (7, 4)]
)
def test_quorum_matches_the_simulator(agents: int, quorum: int) -> None:
    assert required_votes(agents) == quorum


def test_construction_is_validated() -> None:
    store = PeerStateStore(1, (2, 3), 0.4)
    with pytest.raises(ValueError):
        LocalFailureDetector(store, agent_ids=(2, 3), heartbeat_timeout=0.5)
    with pytest.raises(ValueError):
        LocalFailureDetector(store, agent_ids=AGENTS, heartbeat_timeout=0.5)
    _, detector = _detector()
    assert detector.required_votes == 2
    assert detector.heartbeat_timeout == 0.5


def test_silence_alone_never_declares() -> None:
    """Negative: a peer silent for any length of time is suspected, never failed."""

    store, detector = _detector()
    _hear(store, 3, 1.0, 1.0)
    assert detector.propose_votes(1.2) == ()
    for t in (2.0, 5.0, 50.0, 500.0):
        votes = detector.propose_votes(t)
        assert [vote.suspected_agent_id for vote in votes] == [3]
        assert detector.detect(t) == ()
        assert store.status_for(3) is PeerStatus.SILENT
    assert store.declared_failed_peer_ids == frozenset()
    # a never-heard peer is not even suspected: there is no evidence to age.
    assert all(vote.suspected_agent_id != 4 for vote in detector.propose_votes(501.0))


def test_a_matching_quorum_declares_and_contact_retracts() -> None:
    store, detector = _detector()
    _hear(store, 3, 7.0, 1.0)
    (own,) = detector.propose_votes(2.0)
    peer_vote = FailureVote(2, 3, 9.0, 7.0, 1.05, None)
    assert detector.record_vote(peer_vote, received_at=2.1) is True
    assert detector.record_vote(peer_vote, received_at=2.2) is False
    (declaration,) = detector.detect(2.2)
    assert declaration.voter_agent_ids == (1, 2)
    assert declaration.required_votes == 2
    assert store.status_for(3) is PeerStatus.DECLARED_FAILED
    assert detector.declarations_for_broadcast() == (declaration,)
    assert own.task_id is None
    # first-hand contact outranks the certificate.
    _hear(store, 3, 8.0, 2.5)
    assert store.status_for(3) is PeerStatus.HEARD
    assert 3 in store.retracted_peer_ids
    detector.forget(3)
    assert detector.declarations_for_broadcast() == ()


def test_votes_outside_the_window_or_for_older_evidence_do_not_count() -> None:
    store, detector = _detector()
    _hear(store, 3, 7.0, 1.0)
    detector.propose_votes(2.0)
    # a vote about an older heartbeat of the suspect does not match.
    detector.record_vote(FailureVote(2, 3, 9.0, 6.0, 0.5, None), received_at=2.0)
    assert detector.detect(2.1) == ()
    # a matching vote that arrived too long ago has lapsed.
    detector.record_vote(FailureVote(4, 3, 9.0, 7.0, 1.0, None), received_at=2.1)
    assert detector.detect(3.0) == ()


def test_vote_generations_are_ordered_within_source_clocks() -> None:
    _, detector = _detector()
    newer = FailureVote(2, 3, 9.0, 7.0, 1.0, None)
    older = FailureVote(2, 3, 8.0, 7.0, 1.0, None)
    assert detector.record_vote(newer, received_at=1.0)
    assert detector.record_vote(older, received_at=1.1) is False
    conflicting = FailureVote(2, 3, 9.0, 7.0, 1.0, 4)
    with pytest.raises(ValueError, match="conflicting"):
        detector.record_vote(conflicting, received_at=1.2)
    with pytest.raises(ValueError, match="unknown"):
        detector.record_vote(FailureVote(9, 3, 1.0, 1.0, 1.0, None), received_at=1.3)
    # votes against this receiver itself are not actionable here.
    assert (
        detector.record_vote(FailureVote(2, 1, 1.0, 1.0, 1.0, None), received_at=1.4)
        is False
    )


def test_delivered_certificates_are_validated_and_applied() -> None:
    store, detector = _detector()
    _hear(store, 3, 7.0, 1.0)
    good = FailureDeclaration(3, 2, 5.0, 7.0, None, (2, 4), 2)
    assert detector.receive_declaration(good) is True
    assert store.status_for(3) is PeerStatus.DECLARED_FAILED
    about_me = FailureDeclaration(1, 2, 5.0, 7.0, None, (2, 4), 2)
    assert detector.receive_declaration(about_me) is False
    weak = FailureDeclaration(4, 2, 5.0, 7.0, None, (2,), 1)
    with pytest.raises(ValueError):
        detector.receive_declaration(weak)
    stranger = FailureDeclaration(4, 9, 5.0, 7.0, None, (2, 9), 2)
    with pytest.raises(ValueError):
        detector.receive_declaration(stranger)
    # a certificate older than first-hand contact does not apply.
    _hear(store, 2, 1.0, 1.5)
    stale = FailureDeclaration(2, 3, 5.0, 0.5, None, (3, 4), 2)
    assert detector.receive_declaration(stale) is False
