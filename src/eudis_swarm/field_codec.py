"""Encode protocol evidence as versioned JSON datagrams for the lab field node.

This is the wire format of :mod:`eudis_swarm.field_node`. It carries exactly the
immutable values the simulator already moves between receiver-local stores --
:class:`~eudis_swarm.agent.Heartbeat`, :class:`~eudis_swarm.task_claims.TaskClaim`,
:class:`~eudis_swarm.task_claims.TaskClaimRelease`,
:class:`~eudis_swarm.task_claims.TaskCompletionEvidence`,
:class:`~eudis_swarm.failure_manager.FailureVote` and
:class:`~eudis_swarm.failure_manager.FailureDeclaration` -- and nothing else. It
adds no field a receiver could use as a shortcut around the protocol.

One datagram is one envelope::

    {"proto": "eudis-field", "v": 1, "mission": "<id>", "sender": <hop id>,
     "kind": "claim", "body": {...}}

``sender`` names the UAV that *transmitted this hop*, not the evidence's
origin: a forwarded claim keeps its owner inside ``body``. It exists so a
receiver can apply the lab drop filter and enforce the heartbeat rule (a
heartbeat whose ``sender`` is not its own ``agent_id`` was forwarded, which the
protocol forbids, and is rejected).

Every packet arriving from a network is untrusted. :func:`decode_envelope`
therefore checks size before parsing, rejects any key it does not expect, any
missing key, booleans posing as integers, integers posing as reals where a
fraction is meaningful, non-finite numbers, and unknown versions or kinds, and
finally lets the domain dataclasses run their own invariants. Whatever goes
wrong surfaces as :class:`CodecError` and nothing else, so a hostile or corrupt
datagram can be counted and dropped but can never crash a node.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Any, TypeAlias

from .agent import AgentStatus, Heartbeat
from .failure_manager import FailureDeclaration, FailureVote
from .task_claims import TaskClaim, TaskClaimRelease, TaskCompletionEvidence

#: Identifies this protocol so stray JSON on the same port is not mistaken for it.
PROTOCOL_NAME = "eudis-field"
#: Bumped on any incompatible change to an envelope or body.
WIRE_VERSION = 1
#: Hard ceiling for one encoded datagram. Far above any legitimate message
#: (a release carrying two claims is ~400 bytes) and well under a safe UDP
#: payload, so oversized input is rejected before it is parsed.
MAX_DATAGRAM_BYTES = 4096
#: Mission identifiers are short operator-chosen labels.
MAX_MISSION_ID_LENGTH = 64
#: Largest identifier accepted on the wire for an agent or task.
MAX_WIRE_ID = 2**31 - 1

FieldMessage: TypeAlias = (
    Heartbeat
    | TaskClaim
    | TaskClaimRelease
    | TaskCompletionEvidence
    | FailureVote
    | FailureDeclaration
)


class CodecError(ValueError):
    """A datagram that is not a valid, supported protocol envelope."""


class MessageKind(str, Enum):
    """The six evidence kinds that may appear on the wire."""

    HEARTBEAT = "heartbeat"
    CLAIM = "claim"
    RELEASE = "release"
    COMPLETION = "completion"
    VOTE = "vote"
    DECLARATION = "declaration"


@dataclass(frozen=True, slots=True)
class Envelope:
    """One decoded datagram: the hop sender and the immutable evidence."""

    mission_id: str
    sender: int
    message: FieldMessage

    @property
    def kind(self) -> MessageKind:
        return message_kind(self.message)


def message_kind(message: FieldMessage) -> MessageKind:
    """Return the wire kind of one evidence value."""

    if isinstance(message, Heartbeat):
        return MessageKind.HEARTBEAT
    if isinstance(message, TaskClaim):
        return MessageKind.CLAIM
    if isinstance(message, TaskClaimRelease):
        return MessageKind.RELEASE
    if isinstance(message, TaskCompletionEvidence):
        return MessageKind.COMPLETION
    if isinstance(message, FailureVote):
        return MessageKind.VOTE
    if isinstance(message, FailureDeclaration):
        return MessageKind.DECLARATION
    raise TypeError(f"not a field-protocol message: {type(message).__name__}")


# --- encoding ---------------------------------------------------------------


def _claim_body(claim: TaskClaim) -> dict[str, Any]:
    return {
        "task_id": claim.task_id,
        "owner_agent_id": claim.owner_agent_id,
        "epoch": claim.epoch,
        "created_at": claim.created_at,
        "freshness_timeout": claim.freshness_timeout,
        "lease_timeout": claim.lease_timeout,
    }


def _body(message: FieldMessage) -> dict[str, Any]:
    if isinstance(message, Heartbeat):
        return {
            "agent_id": message.agent_id,
            "position": [float(message.position[0]), float(message.position[1])],
            "status": message.status.value,
            "current_task": message.current_task,
            "timestamp": float(message.timestamp),
        }
    if isinstance(message, TaskClaim):
        return _claim_body(message)
    if isinstance(message, TaskClaimRelease):
        return {
            "losing_claim": _claim_body(message.losing_claim),
            "winning_claim": (
                None
                if message.winning_claim is None
                else _claim_body(message.winning_claim)
            ),
            "created_at": message.created_at,
        }
    if isinstance(message, TaskCompletionEvidence):
        return {"claim": _claim_body(message.claim), "created_at": message.created_at}
    if isinstance(message, FailureVote):
        return {
            "voter_agent_id": message.voter_agent_id,
            "suspected_agent_id": message.suspected_agent_id,
            "created_at": message.created_at,
            "last_heartbeat": message.last_heartbeat,
            "last_heard_at": message.last_heard_at,
            "task_id": message.task_id,
        }
    if isinstance(message, FailureDeclaration):
        return {
            "agent_id": message.agent_id,
            "declarer_agent_id": message.declarer_agent_id,
            "detected_at": message.detected_at,
            "last_heartbeat": message.last_heartbeat,
            "task_id": message.task_id,
            "voter_agent_ids": list(message.voter_agent_ids),
            "required_votes": message.required_votes,
        }
    raise TypeError(f"not a field-protocol message: {type(message).__name__}")


def encode_envelope(mission_id: str, sender: int, message: FieldMessage) -> bytes:
    """Serialise one message for one hop; refuses anything it could not decode."""

    _require_mission_id(mission_id)
    _require_id(sender, "sender")
    envelope = {
        "proto": PROTOCOL_NAME,
        "v": WIRE_VERSION,
        "mission": mission_id,
        "sender": sender,
        "kind": message_kind(message).value,
        "body": _body(message),
    }
    data = json.dumps(
        envelope,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(data) > MAX_DATAGRAM_BYTES:
        raise CodecError(f"encoded datagram exceeds {MAX_DATAGRAM_BYTES} bytes")
    return data


# --- decoding ---------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise CodecError(f"non-finite number {name} is not allowed")


def _require_mission_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_MISSION_ID_LENGTH
        or not value.isprintable()
    ):
        raise CodecError("mission must be a short printable non-empty string")
    return value


def _require_id(value: object, name: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_WIRE_ID
    ):
        raise CodecError(f"{name} must be an integer in [1, {MAX_WIRE_ID}]")
    return value


def _optional_id(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _require_id(value, name)


def _require_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CodecError(f"{name} must be an integer")
    return value


def _require_real(value: object, name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(value)
    ):
        raise CodecError(f"{name} must be a finite number")
    return float(value)


def _require_object(value: object, name: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodecError(f"{name} must be a JSON object")
    supplied = frozenset(value)
    if supplied != keys:
        missing = sorted(keys - supplied)
        unexpected = sorted(supplied - keys)
        raise CodecError(
            f"{name} has missing keys {missing} or unexpected keys {unexpected}"
        )
    return value


_ENVELOPE_KEYS = frozenset({"proto", "v", "mission", "sender", "kind", "body"})
_CLAIM_KEYS = frozenset(
    {
        "task_id",
        "owner_agent_id",
        "epoch",
        "created_at",
        "freshness_timeout",
        "lease_timeout",
    }
)
_HEARTBEAT_KEYS = frozenset(
    {"agent_id", "position", "status", "current_task", "timestamp"}
)
_RELEASE_KEYS = frozenset({"losing_claim", "winning_claim", "created_at"})
_COMPLETION_KEYS = frozenset({"claim", "created_at"})
_VOTE_KEYS = frozenset(
    {
        "voter_agent_id",
        "suspected_agent_id",
        "created_at",
        "last_heartbeat",
        "last_heard_at",
        "task_id",
    }
)
_DECLARATION_KEYS = frozenset(
    {
        "agent_id",
        "declarer_agent_id",
        "detected_at",
        "last_heartbeat",
        "task_id",
        "voter_agent_ids",
        "required_votes",
    }
)


def _decode_claim(value: object, name: str) -> TaskClaim:
    body = _require_object(value, name, _CLAIM_KEYS)
    return TaskClaim(
        task_id=_require_id(body["task_id"], "task_id"),
        owner_agent_id=_require_id(body["owner_agent_id"], "owner_agent_id"),
        epoch=_require_int(body["epoch"], "epoch"),
        created_at=_require_real(body["created_at"], "created_at"),
        freshness_timeout=_require_real(body["freshness_timeout"], "freshness_timeout"),
        lease_timeout=_require_real(body["lease_timeout"], "lease_timeout"),
    )


def _decode_heartbeat(value: object) -> Heartbeat:
    body = _require_object(value, "heartbeat body", _HEARTBEAT_KEYS)
    position = body["position"]
    if not isinstance(position, list) or len(position) != 2:
        raise CodecError("position must be a two-element list")
    status = body["status"]
    if not isinstance(status, str) or status not in AgentStatus._value2member_map_:
        raise CodecError("status is not a known agent status")
    return Heartbeat(
        agent_id=_require_id(body["agent_id"], "agent_id"),
        position=(
            _require_real(position[0], "position"),
            _require_real(position[1], "position"),
        ),
        status=AgentStatus(status),
        current_task=_optional_id(body["current_task"], "current_task"),
        timestamp=_require_real(body["timestamp"], "timestamp"),
    )


def _decode_release(value: object) -> TaskClaimRelease:
    body = _require_object(value, "release body", _RELEASE_KEYS)
    winning = body["winning_claim"]
    return TaskClaimRelease(
        losing_claim=_decode_claim(body["losing_claim"], "losing_claim"),
        winning_claim=(
            None if winning is None else _decode_claim(winning, "winning_claim")
        ),
        created_at=_require_real(body["created_at"], "created_at"),
    )


def _decode_completion(value: object) -> TaskCompletionEvidence:
    body = _require_object(value, "completion body", _COMPLETION_KEYS)
    return TaskCompletionEvidence(
        claim=_decode_claim(body["claim"], "claim"),
        created_at=_require_real(body["created_at"], "created_at"),
    )


def _decode_vote(value: object) -> FailureVote:
    body = _require_object(value, "vote body", _VOTE_KEYS)
    return FailureVote(
        voter_agent_id=_require_id(body["voter_agent_id"], "voter_agent_id"),
        suspected_agent_id=_require_id(
            body["suspected_agent_id"], "suspected_agent_id"
        ),
        created_at=_require_real(body["created_at"], "created_at"),
        last_heartbeat=_require_real(body["last_heartbeat"], "last_heartbeat"),
        last_heard_at=_require_real(body["last_heard_at"], "last_heard_at"),
        task_id=_optional_id(body["task_id"], "task_id"),
    )


def _decode_declaration(value: object) -> FailureDeclaration:
    body = _require_object(value, "declaration body", _DECLARATION_KEYS)
    voters = body["voter_agent_ids"]
    if not isinstance(voters, list) or len(voters) > 1024:
        raise CodecError("voter_agent_ids must be a bounded list")
    return FailureDeclaration(
        agent_id=_require_id(body["agent_id"], "agent_id"),
        declarer_agent_id=_require_id(body["declarer_agent_id"], "declarer_agent_id"),
        detected_at=_require_real(body["detected_at"], "detected_at"),
        last_heartbeat=_require_real(body["last_heartbeat"], "last_heartbeat"),
        task_id=_optional_id(body["task_id"], "task_id"),
        voter_agent_ids=tuple(_require_id(item, "voter_agent_id") for item in voters),
        required_votes=_require_id(body["required_votes"], "required_votes"),
    )


_DECODERS = {
    MessageKind.HEARTBEAT: _decode_heartbeat,
    MessageKind.CLAIM: lambda body: _decode_claim(body, "claim body"),
    MessageKind.RELEASE: _decode_release,
    MessageKind.COMPLETION: _decode_completion,
    MessageKind.VOTE: _decode_vote,
    MessageKind.DECLARATION: _decode_declaration,
}


def decode_envelope(data: bytes) -> Envelope:
    """Parse one untrusted datagram, raising only :class:`CodecError`."""

    if not isinstance(data, (bytes, bytearray)):
        raise CodecError("a datagram must be bytes")
    if len(data) > MAX_DATAGRAM_BYTES:
        raise CodecError(f"datagram exceeds {MAX_DATAGRAM_BYTES} bytes")
    try:
        text = bytes(data).decode("utf-8")
        raw = json.loads(text, parse_constant=_reject_constant)
    except CodecError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise CodecError(f"datagram is not valid UTF-8 JSON: {error}") from None
    envelope = _require_object(raw, "envelope", _ENVELOPE_KEYS)
    if envelope["proto"] != PROTOCOL_NAME:
        raise CodecError("datagram is not an eudis-field envelope")
    version = envelope["v"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise CodecError("wire version must be an integer")
    if version != WIRE_VERSION:
        raise CodecError(f"unsupported wire version {version}")
    mission_id = _require_mission_id(envelope["mission"])
    sender = _require_id(envelope["sender"], "sender")
    kind_value = envelope["kind"]
    if not isinstance(kind_value, str) or kind_value not in (
        MessageKind._value2member_map_
    ):
        raise CodecError(f"unknown message kind {kind_value!r}")
    try:
        message = _DECODERS[MessageKind(kind_value)](envelope["body"])
    except CodecError:
        raise
    except (ValueError, TypeError, OverflowError) as error:
        # the domain dataclasses enforce their own invariants; a violation is
        # simply one more way for an untrusted packet to be invalid.
        raise CodecError(f"{kind_value} body violates an invariant: {error}") from None
    return Envelope(mission_id=mission_id, sender=sender, message=message)


__all__ = [
    "CodecError",
    "Envelope",
    "FieldMessage",
    "MAX_DATAGRAM_BYTES",
    "MessageKind",
    "PROTOCOL_NAME",
    "WIRE_VERSION",
    "decode_envelope",
    "encode_envelope",
    "message_kind",
]
