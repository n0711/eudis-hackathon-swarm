"""The field wire codec: exact round trips and strict rejection of bad input."""

from __future__ import annotations

import json
import random

import pytest

from eudis_swarm.agent import AgentStatus, Heartbeat
from eudis_swarm.failure_manager import FailureDeclaration, FailureVote
from eudis_swarm.field_codec import (
    MAX_DATAGRAM_BYTES,
    CodecError,
    MessageKind,
    decode_envelope,
    encode_envelope,
    message_kind,
)
from eudis_swarm.task_claims import TaskClaim, TaskClaimRelease, TaskCompletionEvidence

CLAIM = TaskClaim(
    task_id=3,
    owner_agent_id=2,
    epoch=7,
    created_at=12.345678901234,
    freshness_timeout=0.75,
    lease_timeout=1.5,
)
WINNER = TaskClaim(
    task_id=3,
    owner_agent_id=1,
    epoch=2,
    created_at=0.1,
    freshness_timeout=0.75,
    lease_timeout=1.5,
)
MESSAGES = [
    Heartbeat(
        agent_id=4,
        position=(1.25, -3.5),
        status=AgentStatus.ACTIVE,
        current_task=3,
        timestamp=9.000000001,
    ),
    Heartbeat(
        agent_id=1,
        position=(0.0, 0.0),
        status=AgentStatus.IDLE,
        current_task=None,
        timestamp=0.0,
    ),
    CLAIM,
    TaskClaimRelease(losing_claim=CLAIM, winning_claim=WINNER, created_at=13.0),
    TaskClaimRelease(losing_claim=CLAIM, winning_claim=None, created_at=12.5),
    TaskCompletionEvidence(claim=CLAIM, created_at=13.2),
    FailureVote(
        voter_agent_id=1,
        suspected_agent_id=3,
        created_at=5.0,
        last_heartbeat=4.1,
        last_heard_at=4.2,
        task_id=None,
    ),
    FailureDeclaration(
        agent_id=3,
        declarer_agent_id=1,
        detected_at=5.5,
        last_heartbeat=4.1,
        task_id=2,
        voter_agent_ids=(1, 2),
        required_votes=2,
    ),
]


@pytest.mark.parametrize("message", MESSAGES, ids=lambda item: type(item).__name__)
def test_every_message_round_trips_exactly(message) -> None:
    data = encode_envelope("lab", 5, message)
    envelope = decode_envelope(data)
    assert envelope.message == message
    assert envelope.sender == 5
    assert envelope.mission_id == "lab"
    assert envelope.kind is message_kind(message)


def test_encoding_is_canonical_and_versioned() -> None:
    first = encode_envelope("lab", 2, CLAIM)
    assert first == encode_envelope("lab", 2, CLAIM)
    raw = json.loads(first)
    assert raw["proto"] == "eudis-field"
    assert raw["v"] == 1
    assert raw["kind"] == "claim"
    # the hop sender is not the evidence origin: a forwarded claim keeps its owner.
    forwarded = decode_envelope(encode_envelope("lab", 4, CLAIM))
    assert forwarded.sender == 4
    assert isinstance(forwarded.message, TaskClaim)
    assert forwarded.message.owner_agent_id == 2


def test_encode_refuses_values_it_could_not_decode() -> None:
    with pytest.raises(CodecError):
        encode_envelope("", 1, CLAIM)
    with pytest.raises(CodecError):
        encode_envelope("x" * 65, 1, CLAIM)
    with pytest.raises(CodecError):
        encode_envelope("lab", 0, CLAIM)
    with pytest.raises(CodecError):
        encode_envelope("lab", True, CLAIM)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        encode_envelope("lab", 1, "not a message")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        message_kind(object())  # type: ignore[arg-type]
    huge = FailureDeclaration(
        agent_id=9999,
        declarer_agent_id=1,
        detected_at=1.0,
        last_heartbeat=1.0,
        task_id=None,
        voter_agent_ids=tuple(range(1, 1500)),
        required_votes=2,
    )
    with pytest.raises(CodecError, match="exceeds"):
        encode_envelope("lab", 1, huge)


def _valid(kind_body: tuple[str, object] | None = None, **envelope) -> bytes:
    base = json.loads(encode_envelope("lab", 2, CLAIM))
    if kind_body is not None:
        base["kind"], base["body"] = kind_body
    base.update(envelope)
    return json.dumps(base).encode()


def _claim_body(**changes) -> dict:
    body = json.loads(encode_envelope("lab", 2, CLAIM))["body"]
    body.update(changes)
    return body


BAD_PACKETS = {
    "oversized": b" " * (MAX_DATAGRAM_BYTES + 1),
    "not utf-8": b"\xff\xfe\x00",
    "not json": b"{not json",
    "top-level array": b"[1, 2, 3]",
    "empty": b"",
    "NaN token": _valid().replace(b'"lease_timeout": 1.5', b'"lease_timeout": NaN'),
    "Infinity token": _valid().replace(b'"epoch": 7', b'"epoch": Infinity'),
    "overflowing real": _valid().replace(
        b'"created_at": 12.345678901234', b'"created_at": 1e999'
    ),
    "deep nesting": b"[" * 3000 + b"]" * 3000,
    "wrong protocol": _valid(proto="other"),
    "future version": _valid(v=2),
    "string version": _valid(v="1"),
    "bool version": _valid(v=True),
    "unknown kind": _valid(kind="gossip"),
    "kind not string": _valid(kind=3),
    "missing envelope key": json.dumps({"proto": "eudis-field", "v": 1}).encode(),
    "extra envelope key": _valid(extra=1),
    "bad mission": _valid(mission=""),
    "bool sender": _valid(sender=True),
    "zero sender": _valid(sender=0),
    "huge sender": _valid(sender=2**40),
    "body not object": _valid(("claim", [1, 2])),
    "missing body key": _valid(("claim", {"task_id": 1})),
    "extra body key": _valid(("claim", _claim_body(signature="x"))),
    "float epoch": _valid(("claim", _claim_body(epoch=7.0))),
    "bool epoch": _valid(("claim", _claim_body(epoch=True))),
    "negative task": _valid(("claim", _claim_body(task_id=-1))),
    "string timestamp": _valid(("claim", _claim_body(created_at="12"))),
    "lease below freshness": _valid(("claim", _claim_body(lease_timeout=0.5))),
    "zero epoch": _valid(("claim", _claim_body(epoch=0))),
    "release winner higher id": _valid(
        (
            "release",
            {
                "losing_claim": _claim_body(owner_agent_id=1),
                "winning_claim": _claim_body(owner_agent_id=2),
                "created_at": 13.0,
            },
        )
    ),
    "completion before claim": _valid(
        ("completion", {"claim": _claim_body(), "created_at": 1.0})
    ),
    "heartbeat bad position": _valid(
        (
            "heartbeat",
            {
                "agent_id": 1,
                "position": [1.0],
                "status": "IDLE",
                "current_task": None,
                "timestamp": 1.0,
            },
        )
    ),
    "heartbeat bad status": _valid(
        (
            "heartbeat",
            {
                "agent_id": 1,
                "position": [1.0, 2.0],
                "status": "ALIVE",
                "current_task": None,
                "timestamp": 1.0,
            },
        )
    ),
    "self vote": _valid(
        (
            "vote",
            {
                "voter_agent_id": 2,
                "suspected_agent_id": 2,
                "created_at": 2.0,
                "last_heartbeat": 1.0,
                "last_heard_at": 1.0,
                "task_id": None,
            },
        )
    ),
    "unsorted voters": _valid(
        (
            "declaration",
            {
                "agent_id": 3,
                "declarer_agent_id": 1,
                "detected_at": 2.0,
                "last_heartbeat": 1.0,
                "task_id": None,
                "voter_agent_ids": [2, 1],
                "required_votes": 2,
            },
        )
    ),
    "voters not a list": _valid(
        (
            "declaration",
            {
                "agent_id": 3,
                "declarer_agent_id": 1,
                "detected_at": 2.0,
                "last_heartbeat": 1.0,
                "task_id": None,
                "voter_agent_ids": "1,2",
                "required_votes": 2,
            },
        )
    ),
}


@pytest.mark.parametrize("packet", BAD_PACKETS.values(), ids=list(BAD_PACKETS))
def test_untrusted_input_is_rejected_with_codec_error_only(packet: bytes) -> None:
    with pytest.raises(CodecError):
        decode_envelope(packet)


def test_non_bytes_input_is_rejected() -> None:
    with pytest.raises(CodecError):
        decode_envelope("text")  # type: ignore[arg-type]


def test_random_corruption_never_escapes_as_another_exception() -> None:
    """Fuzz: flipped, truncated and spliced datagrams only ever raise CodecError."""

    rng = random.Random(2026)
    seeds = [encode_envelope("lab", 1, message) for message in MESSAGES]
    outcomes = {"accepted": 0, "rejected": 0}
    for _ in range(3000):
        data = bytearray(rng.choice(seeds))
        operation = rng.randrange(4)
        if operation == 0:
            for _ in range(rng.randrange(1, 4)):
                data[rng.randrange(len(data))] = rng.randrange(256)
        elif operation == 1:
            del data[rng.randrange(len(data)) :]
        elif operation == 2:
            other = rng.choice(seeds)
            cut = rng.randrange(len(other))
            data = data[:cut] + other[cut:]
        else:
            position = rng.randrange(len(data))
            data[position:position] = bytes(rng.randrange(32, 127) for _ in range(5))
        try:
            decode_envelope(bytes(data))
        except CodecError:
            outcomes["rejected"] += 1
        else:
            outcomes["accepted"] += 1
    assert outcomes["rejected"] > 2000


def test_message_kinds_cover_the_protocol_vocabulary() -> None:
    assert {kind.value for kind in MessageKind} == {
        "heartbeat",
        "claim",
        "release",
        "completion",
        "vote",
        "declaration",
    }
