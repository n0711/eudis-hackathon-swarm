"""UDP transport and relay: receipt time, rejection, dedupe, store-and-forward."""

from __future__ import annotations

import errno
import socket
from collections.abc import Callable

import pytest
from field_fakes import FakeClock, FakeNetwork

from eudis_swarm.agent import AgentStatus, Heartbeat
from eudis_swarm.failure_manager import FailureDeclaration, FailureVote
from eudis_swarm.field_codec import encode_envelope
from eudis_swarm.field_transport import (
    EvidenceRelay,
    UdpTransport,
    message_id,
    origin_agent_id,
    parse_address,
)
from eudis_swarm.task_claims import TaskClaim, TaskClaimRelease, TaskCompletionEvidence

CLAIM = TaskClaim(3, 2, 1, 1.0, 0.5, 1.0)
HEARTBEAT = Heartbeat(2, (0.0, 0.0), AgentStatus.IDLE, None, 1.0)
VOTE = FailureVote(1, 3, 2.0, 1.0, 1.1, None)
DECLARATION = FailureDeclaration(3, 1, 2.5, 1.0, None, (1, 2), 2)
RELEASE = TaskClaimRelease(CLAIM, None, 1.2)
COMPLETION = TaskCompletionEvidence(CLAIM, 1.3)

ADDRESSES = {1: ("127.0.0.1", 1), 2: ("127.0.0.1", 2), 3: ("127.0.0.1", 3)}


def _transport(
    network: FakeNetwork,
    agent_id: int,
    clock: FakeClock,
    *,
    drop_rx: Callable[[int], bool] | None = None,
    drop_tx: Callable[[int], bool] | None = None,
    on_reject: Callable[[str, str], None] | None = None,
):
    sock = network.socket(ADDRESSES[agent_id])
    return sock, UdpTransport(
        sock,
        agent_id=agent_id,
        mission_id="lab",
        known_agent_ids=ADDRESSES,
        clock=clock,
        destinations=ADDRESSES,
        drop_rx=drop_rx,
        drop_tx=drop_tx,
        on_reject=on_reject,
    )


def test_parse_address() -> None:
    assert parse_address("10.0.0.2:47001") == ("10.0.0.2", 47001)
    assert parse_address("0.0.0.0:0", allow_ephemeral=True) == ("0.0.0.0", 0)
    for text in ("nohost", ":47001", "host:port", "host:0", "host:70000"):
        with pytest.raises(ValueError):
            parse_address(text)


def test_structural_ids_match_the_simulator_vocabulary() -> None:
    assert message_id(CLAIM) == ("claim", (3, 2, 1))
    assert message_id(RELEASE) == ("release", (3, 2, 1))
    assert message_id(COMPLETION) == ("completion", (3, 2, 1))
    assert message_id(VOTE)[0] == "vote"
    assert message_id(DECLARATION)[0] == "declaration"
    # a renewal is a new generation and therefore new evidence.
    assert message_id(TaskClaim(3, 2, 2, 1.5, 0.5, 1.0)) != message_id(CLAIM)
    with pytest.raises(ValueError, match="never forwarded"):
        message_id(HEARTBEAT)
    assert [
        origin_agent_id(item)
        for item in (HEARTBEAT, CLAIM, RELEASE, COMPLETION, VOTE, DECLARATION)
    ] == [2, 2, 2, 2, 1, 1]


def test_unicast_send_reaches_every_peer_but_self_and_honours_the_filter() -> None:
    network = FakeNetwork()
    clock = FakeClock()
    blocked = {3}
    _, transport = _transport(network, 1, clock, drop_tx=lambda peer: peer in blocked)
    for agent_id in (2, 3):
        network.socket(ADDRESSES[agent_id])
    assert transport.send(CLAIM) == 1
    assert [target for _, target, _ in network.sent] == [ADDRESSES[2]]
    assert transport.counters.dropped_by_filter_tx == 1
    assert transport.counters.sent_by_kind == {"claim": 1}


def test_broadcast_mode_sends_one_datagram() -> None:
    network = FakeNetwork()
    sock = network.socket(("0.0.0.0", 9))
    transport = UdpTransport(
        sock,
        agent_id=1,
        mission_id="lab",
        known_agent_ids=(1, 2),
        clock=FakeClock(),
        broadcast_address=("192.168.1.255", 47000),
    )
    assert transport.send(HEARTBEAT) == 1
    assert network.sent[0][1] == ("192.168.1.255", 47000)


def test_exactly_one_addressing_mode_is_required() -> None:
    network = FakeNetwork()
    with pytest.raises(ValueError):
        UdpTransport(
            network.socket(("a", 1)),
            agent_id=1,
            mission_id="lab",
            known_agent_ids=(1, 2),
            clock=FakeClock(),
        )


def test_a_send_error_is_link_loss_not_a_crash() -> None:
    network = FakeNetwork()
    sock, transport = _transport(network, 1, FakeClock())
    sock.fail_sends = True
    assert transport.send(CLAIM) == 0
    assert transport.counters.send_errors == 2


def test_receipt_is_stamped_with_the_receiver_clock_on_arrival() -> None:
    network = FakeNetwork()
    clock = FakeClock(100.0)
    sock, transport = _transport(network, 1, clock)
    sock.inject(encode_envelope("lab", 2, HEARTBEAT))
    (received,) = transport.receive_available()
    # the source timestamp (1.0) is metadata; receipt time is this clock's.
    assert received.received_at == 100.0
    assert received.message == HEARTBEAT
    assert received.sender == 2


def test_rejections_are_counted_and_reported() -> None:
    network = FakeNetwork()
    reasons: list[str] = []
    blocked = {3}
    sock, transport = _transport(
        network,
        1,
        FakeClock(),
        drop_rx=lambda peer: peer in blocked,
        on_reject=lambda reason, detail: reasons.append(reason),
    )
    sock.inject(b"garbage")
    sock.inject(encode_envelope("other-mission", 2, CLAIM))
    sock.inject(encode_envelope("lab", 1, CLAIM))
    sock.inject(encode_envelope("lab", 9, CLAIM))
    sock.inject(encode_envelope("lab", 3, CLAIM))
    sock.inject(encode_envelope("lab", 2, CLAIM))
    accepted = transport.receive_available()
    assert [item.sender for item in accepted] == [2]
    counters = transport.counters
    assert counters.rejected_malformed == 1
    assert counters.rejected_foreign_mission == 1
    assert counters.own_echoes == 1
    assert counters.rejected_unknown_sender == 1
    assert counters.dropped_by_filter_rx == 1
    assert reasons == ["malformed", "foreign_mission", "unknown_sender"]
    assert counters.as_dict()["accepted"] == 1


class _ErrorSocket:
    def __init__(self, errors: list[OSError]) -> None:
        self.errors = errors

    def sendto(self, data: bytes, address, /) -> int:
        return len(data)

    def recvfrom(self, bufsize: int, /):
        raise self.errors.pop(0)

    def close(self) -> None:
        return None


def test_icmp_refusals_are_skipped_and_other_errors_end_the_drain() -> None:
    sock = _ErrorSocket(
        [
            OSError(errno.ECONNREFUSED, "refused"),
            OSError(errno.EBADF, "bad"),
        ]
    )
    transport = UdpTransport(
        sock,
        agent_id=1,
        mission_id="lab",
        known_agent_ids=(1, 2),
        clock=FakeClock(),
        destinations={2: ("127.0.0.1", 2)},
    )
    assert transport.receive_available() == ()
    assert sock.errors == []
    transport.wait(0.0)
    transport.wait(0.01)  # no fileno: returns at once
    transport.close()


def test_wait_uses_select_on_a_real_socket() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    transport = UdpTransport(
        sock,
        agent_id=1,
        mission_id="lab",
        known_agent_ids=(1, 2),
        clock=FakeClock(),
        destinations={2: sock.getsockname()},
    )
    transport.wait(0.01)
    transport.send(CLAIM)
    transport.wait(1.0)
    assert transport.socket is sock
    transport.close()
    transport.wait(0.01)  # closed socket: swallowed


def test_relay_admits_each_structural_id_once() -> None:
    relay = EvidenceRelay(retention=1.0)
    assert relay.admit(CLAIM, 0.0) is True
    assert relay.admit(CLAIM, 0.1) is False
    assert relay.seen(CLAIM)
    assert relay.counters.as_dict() == {
        "first_deliveries": 1,
        "duplicates_suppressed": 1,
        "forwarded": 0,
        "reoffered": 0,
    }
    with pytest.raises(ValueError, match="one-hop"):
        relay.admit(HEARTBEAT, 0.0)
    with pytest.raises(ValueError):
        EvidenceRelay(retention=0.0)


def test_relay_reoffers_lease_valid_evidence_and_completions_forever() -> None:
    relay = EvidenceRelay(retention=1.0)
    for message in (CLAIM, RELEASE, COMPLETION, VOTE, DECLARATION):
        relay.admit(message, 0.0)
    assert set(map(type, relay.reoffer(0.5))) == {
        TaskClaim,
        TaskClaimRelease,
        TaskCompletionEvidence,
    }
    # a superseded generation is not re-offered once the newer one is held.
    renewal = TaskClaim(3, 2, 2, 1.5, 0.5, 1.0)
    relay.admit(renewal, 0.6)
    assert renewal in relay.reoffer(0.7) and CLAIM not in relay.reoffer(0.7)
    # past retention only terminal evidence keeps being re-offered.
    assert relay.reoffer(2.0) == (COMPLETION,)
    assert relay.seen_count == 6
    # long-dead vote generations are forgotten to bound memory.
    relay.reoffer(10.0)
    assert relay.seen_count == 5
    relay.remember_own(VOTE, 10.0)
    relay.remember_own(VOTE, 11.0)
    assert relay.admit(VOTE, 11.0) is False
