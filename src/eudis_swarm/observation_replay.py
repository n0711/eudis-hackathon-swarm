"""Deterministic, receiver-local replay of external perception observations.

This is an evidence adapter, not a task-discovery or flight-control path. Input
events describe deliveries that already occurred; a dropped event never reaches
the receiver. Source timestamps are metadata, while freshness uses receiver time.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, TypedDict

TRACK_STATES = frozenset({"observed", "temporarily_missing", "expired"})
SOURCE_STATES = frozenset({"running", "stalled", "stopped"})


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _time(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and nonnegative")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


@dataclass(frozen=True)
class TrackObservation:
    uav_id: str
    track_id: int
    timestamp_ms: int
    class_label: str
    confidence: float
    bbox_xyxy: tuple[int, int, int, int]
    image_size: tuple[int, int]
    track_status: str


@dataclass(frozen=True)
class SourceStatus:
    uav_id: str
    timestamp_ms: int
    status: str
    empty_scene: bool = False


Record = TrackObservation | SourceStatus


class TrackView(TypedDict):
    uav_id: str
    track_id: int
    timestamp_ms: int
    track_status: str


class ReceiverSnapshot(TypedDict):
    sources: dict[str, str]
    tracks: list[TrackView]


def parse_record(payload: Mapping[str, object]) -> Record:
    """Validate a version-1 JSON record without interpreting its timestamp as time.

    Track IDs are local to their source. An explicit source-status record marks
    an empty scene; the absence of track messages does not.
    """

    if _integer(payload.get("schema_version"), "schema_version", minimum=1) != 1:
        raise ValueError("unsupported schema_version")
    uav_id = _identifier(payload.get("uav_id"), "uav_id")
    timestamp = _integer(payload.get("timestamp_ms"), "timestamp_ms")
    if payload.get("record_type", "track") == "source_status":
        status = payload.get("status")
        if not isinstance(status, str) or status not in SOURCE_STATES:
            raise ValueError("invalid source status")
        empty = payload.get("empty_scene", False)
        if not isinstance(empty, bool):
            raise ValueError("empty_scene must be boolean")
        return SourceStatus(uav_id, timestamp, str(status), empty)
    if payload.get("record_type", "track") != "track":
        raise ValueError("invalid record_type")

    track_id = _integer(payload.get("track_id"), "track_id")
    label = _identifier(payload.get("class_label"), "class_label")
    raw_confidence = payload.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise ValueError("confidence must be in [0, 1]")
    confidence = float(raw_confidence)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be in [0, 1]")
    image = payload.get("image_size")
    box = payload.get("bbox_xyxy")
    if not isinstance(image, list) or len(image) != 2:
        raise ValueError("image_size must contain width and height")
    width, height = (_integer(v, "image_size", minimum=1) for v in image)
    if not isinstance(box, list) or len(box) != 4:
        raise ValueError("bbox_xyxy must contain four coordinates")
    x0, y0, x1, y1 = (_integer(v, "bbox_xyxy") for v in box)
    if not (x0 < x1 <= width and y0 < y1 <= height):
        raise ValueError("bbox_xyxy must fit inside image_size")
    track_status = payload.get("track_status")
    if not isinstance(track_status, str) or track_status not in TRACK_STATES:
        raise ValueError("invalid track_status")
    return TrackObservation(
        uav_id,
        track_id,
        timestamp,
        label,
        confidence,
        (x0, y0, x1, y1),
        (width, height),
        str(track_status),
    )


class ReceiverObservations:
    """One receiver's delivered evidence, with no access to other replicas."""

    def __init__(self, *, track_timeout: float = 2.0, source_timeout: float = 3.0):
        self.track_timeout = _time(track_timeout, "track_timeout")
        self.source_timeout = _time(source_timeout, "source_timeout")
        if not self.track_timeout or not self.source_timeout:
            raise ValueError("timeouts must be positive")
        self._tracks: dict[tuple[str, int], tuple[TrackObservation, float]] = {}
        self._sources: dict[str, tuple[SourceStatus, float]] = {}
        self._latest_track_timestamp: dict[tuple[str, int], int] = {}
        self._latest_source_timestamp: dict[str, int] = {}
        self._last_receipt = 0.0

    def receive(self, record: Record, *, received_at: float) -> bool:
        """Return false for old/duplicate source evidence, without refreshing age."""

        received_at = _time(received_at, "received_at")
        if received_at < self._last_receipt:
            raise ValueError("received_at must be monotonic for each receiver")
        self._last_receipt = received_at
        if isinstance(record, SourceStatus):
            previous = self._latest_source_timestamp.get(record.uav_id, -1)
            if record.timestamp_ms <= previous:
                return False
            self._latest_source_timestamp[record.uav_id] = record.timestamp_ms
            self._sources[record.uav_id] = (record, received_at)
            if record.empty_scene or record.status != "running":
                for key, (track, _) in list(self._tracks.items()):
                    if (
                        key[0] == record.uav_id
                        and track.timestamp_ms <= record.timestamp_ms
                    ):
                        del self._tracks[key]
            return True

        key = (record.uav_id, record.track_id)
        previous = self._latest_track_timestamp.get(key, -1)
        if record.timestamp_ms <= previous:
            return False
        source = self._sources.get(record.uav_id)
        if source is not None and source[0].timestamp_ms >= record.timestamp_ms:
            if source[0].empty_scene or source[0].status != "running":
                return False
        self._latest_track_timestamp[key] = record.timestamp_ms
        if record.track_status == "expired":
            self._tracks.pop(key, None)
        else:
            self._tracks[key] = (record, received_at)
        return True

    def snapshot(self, *, now: float) -> ReceiverSnapshot:
        """Expose local evidence only; no field asserts physical source health."""

        now = _time(now, "now")
        if now < self._last_receipt:
            raise ValueError("now precedes the last receipt")
        sources: dict[str, str] = {}
        for uav_id, (status, receipt) in sorted(self._sources.items()):
            sources[uav_id] = (
                "timed_out" if now - receipt > self.source_timeout else status.status
            )
        tracks: list[TrackView] = [
            {
                "uav_id": track.uav_id,
                "track_id": track.track_id,
                "timestamp_ms": track.timestamp_ms,
                "track_status": track.track_status,
            }
            for (uav_id, _), (track, receipt) in sorted(self._tracks.items())
            if sources.get(uav_id) == "running" and now - receipt <= self.track_timeout
        ]
        return {"sources": sources, "tracks": tracks}


def replay_lines(lines: Sequence[str], *, now: float) -> dict[str, ReceiverSnapshot]:
    """Replay explicit delivery envelopes into independent receiver stores."""

    receivers: dict[str, ReceiverObservations] = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            receiver = _identifier(event.get("receiver_id"), "receiver_id")
            delivered = event.get("delivered", True)
            if not isinstance(delivered, bool):
                raise ValueError("delivered must be boolean")
            at = _time(event.get("received_at"), "received_at")
            if at > now:
                continue
            if delivered:
                payload = event.get("record")
                if not isinstance(payload, dict):
                    raise ValueError("record must be an object")
                receivers.setdefault(receiver, ReceiverObservations()).receive(
                    parse_record(payload), received_at=at
                )
        except (ValueError, TypeError) as error:
            raise ValueError(f"line {number}: {error}") from error
    return {key: store.snapshot(now=now) for key, store in sorted(receivers.items())}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay delivered ISR observations")
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--at", type=float, required=True, help="receiver-local time")
    args = parser.parse_args(argv)
    try:
        result = replay_lines(
            args.jsonl.read_text(encoding="utf-8").splitlines(), now=args.at
        )
    except ValueError as error:
        parser.error(str(error))
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
