"""Receiver-local external observations never become global task authority."""

from __future__ import annotations

import json

import pytest

from eudis_swarm.observation_replay import (
    ReceiverObservations,
    SourceStatus,
    TrackObservation,
    parse_record,
    replay_lines,
)


def track(uav_id: str, timestamp_ms: int, track_id: int = 17) -> TrackObservation:
    result = parse_record(
        {
            "schema_version": 1,
            "uav_id": uav_id,
            "track_id": track_id,
            "timestamp_ms": timestamp_ms,
            "class_label": "vehicle",
            "confidence": 0.87,
            "bbox_xyxy": [120, 85, 220, 160],
            "image_size": [640, 480],
            "track_status": "observed",
        }
    )
    assert isinstance(result, TrackObservation)
    return result


def status(uav_id: str, timestamp_ms: int, *, empty: bool = False) -> SourceStatus:
    return SourceStatus(uav_id, timestamp_ms, "running", empty)


def test_two_sources_can_reuse_track_id_without_collision() -> None:
    store = ReceiverObservations()
    for uav_id in ("isr_01", "isr_02"):
        store.receive(status(uav_id, 100), received_at=0.1)
        store.receive(track(uav_id, 101), received_at=0.1)
    snapshot = store.snapshot(now=0.5)
    assert [(item["uav_id"], item["track_id"]) for item in snapshot["tracks"]] == [
        ("isr_01", 17),
        ("isr_02", 17),
    ]


def test_empty_scene_and_old_deliveries_do_not_resurrect_a_track() -> None:
    store = ReceiverObservations()
    store.receive(status("isr_01", 100), received_at=0)
    store.receive(track("isr_01", 101), received_at=0.1)
    assert len(store.snapshot(now=0.1)["tracks"]) == 1
    store.receive(status("isr_01", 103, empty=True), received_at=0.2)
    assert store.snapshot(now=0.2)["tracks"] == []
    assert not store.receive(track("isr_01", 102), received_at=0.3)
    assert store.snapshot(now=0.3)["tracks"] == []
    assert store.receive(track("isr_01", 104), received_at=0.4)
    assert len(store.snapshot(now=0.4)["tracks"]) == 1


def test_timeout_is_receiver_local_and_reconnect_requires_fresh_status() -> None:
    left = ReceiverObservations(track_timeout=1, source_timeout=2)
    right = ReceiverObservations(track_timeout=1, source_timeout=2)
    left.receive(status("isr_01", 100), received_at=0)
    left.receive(track("isr_01", 101), received_at=0.1)
    assert right.snapshot(now=0.5) == {"sources": {}, "tracks": []}
    assert left.snapshot(now=1.2)["tracks"] == []
    assert left.snapshot(now=2.1)["sources"] == {"isr_01": "timed_out"}
    assert not left.receive(status("isr_01", 100), received_at=2.2)
    assert left.snapshot(now=2.2)["sources"] == {"isr_01": "timed_out"}
    left.receive(status("isr_01", 102), received_at=2.3)
    left.receive(track("isr_01", 103), received_at=2.4)
    assert len(left.snapshot(now=2.4)["tracks"]) == 1


def test_replay_respects_drops_receiver_identity_and_query_time() -> None:
    events = [
        {
            "receiver_id": "uav_1",
            "received_at": 0,
            "record": {
                "schema_version": 1,
                "record_type": "source_status",
                "uav_id": "isr_01",
                "timestamp_ms": 100,
                "status": "running",
            },
        },
        {
            "receiver_id": "uav_1",
            "received_at": 0.2,
            "record": {
                "schema_version": 1,
                "uav_id": "isr_01",
                "track_id": 17,
                "timestamp_ms": 101,
                "class_label": "vehicle",
                "confidence": 0.87,
                "bbox_xyxy": [1, 2, 3, 4],
                "image_size": [10, 10],
                "track_status": "observed",
            },
        },
        {"receiver_id": "uav_2", "received_at": 0.2, "delivered": False},
        {
            "receiver_id": "uav_1",
            "received_at": 5,
            "record": {
                "schema_version": 1,
                "record_type": "source_status",
                "uav_id": "isr_01",
                "timestamp_ms": 102,
                "status": "running",
            },
        },
    ]
    lines = [json.dumps(event) for event in events]
    assert len(replay_lines(lines, now=0.5)["uav_1"]["tracks"]) == 1
    assert "uav_2" not in replay_lines(lines, now=0.5)
    assert replay_lines(lines, now=4)["uav_1"]["sources"] == {"isr_01": "timed_out"}
    assert replay_lines(lines, now=5)["uav_1"]["sources"] == {"isr_01": "running"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("timestamp_ms", -1),
        ("confidence", float("nan")),
        ("bbox_xyxy", [1, 2, 999, 4]),
        ("image_size", [0, 480]),
        ("track_status", "unknown"),
    ],
)
def test_invalid_observation_is_rejected(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "uav_id": "isr_01",
        "track_id": 1,
        "timestamp_ms": 100,
        "class_label": "vehicle",
        "confidence": 0.8,
        "bbox_xyxy": [1, 2, 3, 4],
        "image_size": [10, 10],
        "track_status": "observed",
    }
    payload[field] = value
    with pytest.raises(ValueError):
        parse_record(payload)


def test_duplicate_does_not_refresh_track_and_time_must_be_ordered() -> None:
    store = ReceiverObservations(track_timeout=1)
    store.receive(status("isr_01", 100), received_at=0)
    observation = track("isr_01", 101)
    store.receive(observation, received_at=0.1)
    assert not store.receive(observation, received_at=0.9)
    assert store.snapshot(now=1.2)["tracks"] == []
    with pytest.raises(ValueError, match="monotonic"):
        store.receive(track("isr_01", 102), received_at=0.8)
