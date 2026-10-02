"""Carry field-protocol datagrams over real UDP with the simulator's semantics.

Two pieces, kept apart so each can be tested on its own:

* :class:`UdpTransport` moves bytes. It encodes one envelope per datagram,
  sends it to a broadcast address or to a configured unicast peer list, and on
  receipt stamps every datagram with the *receiver's* clock the moment it is
  read off the socket. That receipt time is the only time a node ever uses for
  freshness; source timestamps inside the body stay metadata.
* :class:`EvidenceRelay` decides what to forward. Protocol evidence (claims,
  releases, completions, votes, declarations) is admitted at most once per
  structural ID at this receiver -- the per-receiver duplicate suppression of
  :mod:`eudis_swarm.messaging` -- and a first admission is what triggers a
  forward. Heartbeats are refused outright: they are one-hop observations and
  are never forwarded.

Store-and-forward on a real network. The simulator keeps a route pending until
the one-hop delivery succeeds. UDP gives no delivery signal, so the field relay
gets the same effect from persistence instead: every node periodically
re-offers the lease-valid evidence it holds (:meth:`EvidenceRelay.reoffer`).
Receivers that already have a message suppress the duplicate, so a re-offer
neither refreshes a lease nor floods further; a receiver that missed it --
lost packet, or a bridge that only just appeared -- applies it as a first
delivery. There is no TTL and no random backoff, exactly as in the simulator;
the bound on flooding is the finite set of structural IDs.

Nothing here reads another node's state. The only inputs are datagrams that
actually arrived.
"""

from __future__ import annotations

import errno
import select
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias

from .agent import Heartbeat
from .failure_manager import FailureDeclaration, FailureVote
from .field_codec import (
    MAX_DATAGRAM_BYTES,
    CodecError,
    FieldMessage,
    MessageKind,
    decode_envelope,
    encode_envelope,
    message_kind,
)
from .task_claims import TaskClaim, TaskClaimRelease, TaskCompletionEvidence

Address: TypeAlias = tuple[str, int]
#: A structural identifier. Two values with the same ID must be the same
#: evidence; the stores reject a conflicting duplicate.
MessageId: TypeAlias = tuple[Any, ...]


def parse_address(text: str, *, allow_ephemeral: bool = False) -> Address:
    """Parse ``host:port`` into a socket address (port 0 only if allowed)."""

    host, separator, port_text = text.rpartition(":")
    if not separator or not host:
        raise ValueError(f"address must be host:port, got {text!r}")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"address port must be an integer, got {text!r}") from None
    if not (0 if allow_ephemeral else 1) <= port < 65536:
        raise ValueError(f"address port must be in 1..65535, got {text!r}")
    return (host, port)


def message_id(message: FieldMessage) -> MessageId:
    """Return the structural ID used for duplicate suppression.

    Task evidence follows :class:`~eudis_swarm.messaging.TaskProtocolMessageId`
    (kind plus exact claim ID). Failure evidence uses the fields that make one
    vote generation or one certificate distinct.
    """

    kind = message_kind(message)
    if isinstance(message, TaskClaim):
        return (kind.value, message.claim_id)
    if isinstance(message, TaskClaimRelease):
        return (kind.value, message.losing_claim.claim_id)
    if isinstance(message, TaskCompletionEvidence):
        return (kind.value, message.claim.claim_id)
    if isinstance(message, FailureVote):
        return (
            kind.value,
            message.voter_agent_id,
            message.suspected_agent_id,
            message.last_heartbeat,
            message.last_heard_at,
            message.created_at,
        )
    if isinstance(message, FailureDeclaration):
        return (
            kind.value,
            message.declarer_agent_id,
            message.agent_id,
            message.last_heartbeat,
            message.detected_at,
        )
    raise ValueError("heartbeats have no flooding identity; they are never forwarded")


def origin_agent_id(message: FieldMessage) -> int:
    """Return the UAV that authored one piece of evidence."""

    if isinstance(message, Heartbeat):
        return message.agent_id
    if isinstance(message, TaskClaim):
        return message.owner_agent_id
    if isinstance(message, TaskClaimRelease):
        return message.releasing_agent_id
    if isinstance(message, TaskCompletionEvidence):
        return message.owner_agent_id
    if isinstance(message, FailureVote):
        return message.voter_agent_id
    return message.declarer_agent_id


class DatagramSocket(Protocol):
    """The slice of :class:`socket.socket` the transport uses (fakeable)."""

    def sendto(self, data: bytes, address: Address, /) -> int: ...

    def recvfrom(self, bufsize: int, /) -> tuple[bytes, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    """One accepted datagram and the receiver-local time it was read."""

    message: FieldMessage
    sender: int
    received_at: float
    address: Any


@dataclass(slots=True)
class TransportCounters:
    """Packet accounting for one node; observer-facing only."""

    datagrams_sent: int = 0
    send_errors: int = 0
    datagrams_received: int = 0
    accepted: int = 0
    rejected_malformed: int = 0
    rejected_foreign_mission: int = 0
    rejected_unknown_sender: int = 0
    own_echoes: int = 0
    dropped_by_filter_rx: int = 0
    dropped_by_filter_tx: int = 0
    sent_by_kind: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "datagrams_sent": self.datagrams_sent,
            "send_errors": self.send_errors,
            "datagrams_received": self.datagrams_received,
            "accepted": self.accepted,
            "rejected_malformed": self.rejected_malformed,
            "rejected_foreign_mission": self.rejected_foreign_mission,
            "rejected_unknown_sender": self.rejected_unknown_sender,
            "own_echoes": self.own_echoes,
            "dropped_by_filter_rx": self.dropped_by_filter_rx,
            "dropped_by_filter_tx": self.dropped_by_filter_tx,
            "sent_by_kind": dict(sorted(self.sent_by_kind.items())),
        }


#: Answers "should this datagram to/from this peer be dropped right now?"
DropPredicate: TypeAlias = Callable[[int], bool]


class UdpTransport:
    """Send and receive envelopes for exactly one node.

    ``destinations`` maps each peer ID to the address its datagrams go to
    (unicast). In broadcast mode pass ``broadcast_address`` instead; then a
    single datagram reaches every listener and the drop filter can act only on
    the receiving side, which is enough because every node applies the same
    symmetric filter.
    """

    def __init__(
        self,
        sock: DatagramSocket,
        *,
        agent_id: int,
        mission_id: str,
        known_agent_ids: Iterable[int],
        clock: Callable[[], float],
        destinations: Mapping[int, Address] | None = None,
        broadcast_address: Address | None = None,
        drop_rx: DropPredicate | None = None,
        drop_tx: DropPredicate | None = None,
        on_reject: Callable[[str, str], None] | None = None,
    ) -> None:
        if (destinations is None) == (broadcast_address is None):
            raise ValueError("configure exactly one of destinations or broadcast")
        self._sock = sock
        self._agent_id = agent_id
        self._mission_id = mission_id
        self._known = frozenset(known_agent_ids)
        self._clock = clock
        self._destinations = (
            None
            if destinations is None
            else tuple(
                (peer_id, destinations[peer_id])
                for peer_id in sorted(destinations)
                if peer_id != agent_id
            )
        )
        self._broadcast = broadcast_address
        self._drop_rx = drop_rx
        self._drop_tx = drop_tx
        self._on_reject = on_reject
        self.counters = TransportCounters()

    @property
    def socket(self) -> DatagramSocket:
        return self._sock

    def set_reject_hook(self, hook: Callable[[str, str], None] | None) -> None:
        """Route rejected-datagram notices to the node (for counting/logging)."""

        self._on_reject = hook

    def send(self, message: FieldMessage) -> int:
        """Transmit one message as this hop; return datagrams handed to the OS."""

        data = encode_envelope(self._mission_id, self._agent_id, message)
        kind = message_kind(message).value
        sent = 0
        if self._destinations is None:
            assert self._broadcast is not None
            sent += self._sendto(data, self._broadcast)
        else:
            for peer_id, address in self._destinations:
                if self._drop_tx is not None and self._drop_tx(peer_id):
                    self.counters.dropped_by_filter_tx += 1
                    continue
                sent += self._sendto(data, address)
        self.counters.sent_by_kind[kind] = self.counters.sent_by_kind.get(kind, 0) + 1
        return sent

    def _sendto(self, data: bytes, address: Address) -> int:
        try:
            self._sock.sendto(data, address)
        except OSError:
            # a peer that is not listening yet, an interface going down when
            # WiFi is switched off: both are link loss, not a node failure.
            self.counters.send_errors += 1
            return 0
        self.counters.datagrams_sent += 1
        return 1

    def receive_available(self, limit: int = 1024) -> tuple[ReceivedMessage, ...]:
        """Drain what has already arrived, without blocking."""

        accepted: list[ReceivedMessage] = []
        for _ in range(limit):
            try:
                data, address = self._sock.recvfrom(MAX_DATAGRAM_BYTES + 1)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as error:
                # Windows and some Linux paths report an ICMP port-unreachable
                # from an earlier send as a receive error; it is link loss.
                if error.errno in (errno.ECONNREFUSED, errno.ECONNRESET):
                    continue
                break
            # the receipt time belongs to this receiver and is taken on arrival.
            received_at = self._clock()
            self.counters.datagrams_received += 1
            item = self._accept(data, address, received_at)
            if item is not None:
                accepted.append(item)
        return tuple(accepted)

    def _accept(
        self, data: bytes, address: Any, received_at: float
    ) -> ReceivedMessage | None:
        try:
            envelope = decode_envelope(data)
        except CodecError as error:
            self.counters.rejected_malformed += 1
            self._reject("malformed", str(error))
            return None
        if envelope.mission_id != self._mission_id:
            self.counters.rejected_foreign_mission += 1
            self._reject("foreign_mission", envelope.mission_id)
            return None
        if envelope.sender == self._agent_id:
            # our own broadcast looped back; not evidence about anyone.
            self.counters.own_echoes += 1
            return None
        if envelope.sender not in self._known:
            self.counters.rejected_unknown_sender += 1
            self._reject("unknown_sender", str(envelope.sender))
            return None
        if self._drop_rx is not None and self._drop_rx(envelope.sender):
            self.counters.dropped_by_filter_rx += 1
            return None
        self.counters.accepted += 1
        return ReceivedMessage(
            message=envelope.message,
            sender=envelope.sender,
            received_at=received_at,
            address=address,
        )

    def _reject(self, reason: str, detail: str) -> None:
        if self._on_reject is not None:
            self._on_reject(reason, detail)

    def wait(self, timeout: float) -> None:
        """Block until a datagram may be readable or ``timeout`` elapses."""

        if timeout <= 0.0:
            return
        fileno = getattr(self._sock, "fileno", None)
        if fileno is None:
            return
        try:
            select.select([fileno()], [], [], timeout)
        except (OSError, ValueError):
            return

    def close(self) -> None:
        self._sock.close()


def open_udp_socket(
    bind: Address, *, broadcast: bool = False
) -> socket.socket:  # pragma: no cover - exercised by the subprocess end-to-end test
    """Open a non-blocking UDP socket bound to ``bind``."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # several listeners on one broadcast port (e.g. two nodes on one host).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(bind)
    sock.setblocking(False)
    return sock


@dataclass(slots=True)
class _HeldEvidence:
    message: FieldMessage
    first_received_at: float


@dataclass(slots=True)
class RelayCounters:
    first_deliveries: int = 0
    duplicates_suppressed: int = 0
    forwarded: int = 0
    reoffered: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "first_deliveries": self.first_deliveries,
            "duplicates_suppressed": self.duplicates_suppressed,
            "forwarded": self.forwarded,
            "reoffered": self.reoffered,
        }


class EvidenceRelay:
    """Per-receiver duplicate suppression and store-and-forward retention."""

    def __init__(self, *, retention: float) -> None:
        if retention <= 0.0:
            raise ValueError("retention must be positive")
        self._retention = retention
        self._held: dict[MessageId, _HeldEvidence] = {}
        self.counters = RelayCounters()

    def seen(self, message: FieldMessage) -> bool:
        return message_id(message) in self._held

    @property
    def seen_count(self) -> int:
        return len(self._held)

    def admit(self, message: FieldMessage, received_at: float) -> bool:
        """Return ``True`` on the first delivery of this structural ID here."""

        if isinstance(message, Heartbeat):
            raise ValueError("heartbeats are one-hop and never enter the relay")
        identity = message_id(message)
        if identity in self._held:
            self.counters.duplicates_suppressed += 1
            return False
        self._held[identity] = _HeldEvidence(message, received_at)
        self.counters.first_deliveries += 1
        return True

    def remember_own(self, message: FieldMessage, created_at: float) -> None:
        """Mark locally originated evidence as seen so echoes are not re-relayed."""

        identity = message_id(message)
        if identity not in self._held:
            self._held[identity] = _HeldEvidence(message, created_at)

    def reoffer(self, now: float) -> tuple[FieldMessage, ...]:
        """Return held evidence still worth re-offering, in stable order.

        Completions are terminal facts and are always re-offered. Claims and
        releases matter only while some lease they describe can still be
        valid, so they are retained for ``retention`` receiver-local seconds
        after first receipt. Failure votes and declarations are re-offered by
        their origins (which keep them while they hold), not by relays.
        """

        offered: list[tuple[MessageId, FieldMessage]] = []
        expired_votes: list[MessageId] = []
        for identity, held in self._held.items():
            kind = identity[0]
            if kind == MessageKind.VOTE.value:
                # origins replace votes every round, so an old generation can
                # only come back as a badly delayed packet; forgetting it
                # bounds memory on a long partition.
                if now - held.first_received_at > 4.0 * self._retention:
                    expired_votes.append(identity)
                continue
            if kind == MessageKind.COMPLETION.value:
                offered.append((identity, held.message))
            elif kind in (MessageKind.CLAIM.value, MessageKind.RELEASE.value):
                if now - held.first_received_at <= self._retention:
                    offered.append((identity, held.message))
        for identity in expired_votes:
            del self._held[identity]
        offered.sort(key=lambda item: repr(item[0]))
        self.counters.reoffered += len(offered)
        return tuple(message for _, message in offered)


__all__ = [
    "Address",
    "DatagramSocket",
    "EvidenceRelay",
    "MessageId",
    "ReceivedMessage",
    "RelayCounters",
    "TransportCounters",
    "UdpTransport",
    "message_id",
    "open_udp_socket",
    "origin_agent_id",
    "parse_address",
]
