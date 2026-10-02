"""Delivery and discovery never grant another receiver's private knowledge."""

from dataclasses import replace

import pytest

from eudis_swarm.agent import Agent
from eudis_swarm.communication import CommunicationGraph
from eudis_swarm.config import SimulationConfig
from eudis_swarm.observation_replay import (
    ReceiverObservations,
    SourceStatus,
    TrackObservation,
    parse_record,
)
from eudis_swarm.observation_transport import (
    ObservationPublication,
    ObservationTransport,
)
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import Task, TaskStatus
from eudis_swarm.task_discovery import ReceiverTaskDiscovery, build_discovery_catalog


def track(timestamp: int = 101, **changes: object) -> TrackObservation:
    record = TrackObservation(
        "camera_1",
        17,
        timestamp,
        "vehicle",
        0.9,
        (0, 0, 10, 10),
        (20, 20),
        "observed",
        (5.0, 0.0),
    )
    return replace(record, **changes)


def publications(at: float = 0.0) -> tuple[ObservationPublication, ...]:
    return (
        ObservationPublication(1, at, SourceStatus("camera_1", 100, "running")),
        ObservationPublication(1, at, track()),
    )


def test_transport_drop_reconnect_and_duplicate_do_not_refresh() -> None:
    graph = CommunicationGraph((1, 2), 10.0)
    graph.update({1: (0.0, 0.0), 2: (1.0, 0.0)}, blocked_links=((1, 2),))
    stores = {i: ReceiverObservations() for i in (1, 2)}
    transport = ObservationTransport(graph, stores)
    for publication in publications():
        receipts = transport.deliver(publication, now=0.0, participants=(1, 2))
        assert receipts[0].delivered and receipts[0].accepted
        assert not receipts[1].delivered
    assert stores[2].snapshot(now=0.0) == {"sources": {}, "tracks": []}
    graph.update({1: (0.0, 0.0), 2: (1.0, 0.0)})
    # No implicit backlog on reconnect.
    assert stores[2].snapshot(now=1.0)["tracks"] == []
    for publication in publications():
        transport.deliver(publication, now=1.0, participants=(1, 2))
    assert len(stores[2].snapshot(now=1.0)["tracks"]) == 1
    for publication in publications():
        receipts = transport.deliver(publication, now=2.0, participants=(1, 2))
        assert not any(r.accepted for r in receipts)
    assert stores[2].snapshot(now=3.1)["tracks"] == []
    assert all(
        not r.delivered
        for r in transport.deliver(publications()[0], now=4.0, participants=(2,))
    )
    with pytest.raises(ValueError, match="unknown observation receiver"):
        transport.deliver(publications()[0], now=4.0, participants=(3,))
    with pytest.raises(ValueError, match="unknown observation source"):
        transport.deliver(
            ObservationPublication(3, 0.0, track()), now=4.0, participants=(2,)
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"mission_position_m": None},
        {"confidence": 0.59},
        {"track_status": "temporarily_missing"},
        {"track_status": "expired"},
    ],
)
def test_pixel_only_low_confidence_and_nonobserved_tracks_do_not_discover(
    changes: dict,
) -> None:
    catalog = build_discovery_catalog(
        publications(), first_task_id=2, width=10, height=10
    )
    discovery = ReceiverTaskDiscovery(
        {key: objective.task_id for key, objective in catalog.items()}
    )
    store = ReceiverObservations()
    store.receive(SourceStatus("camera_1", 100, "running"), received_at=0.0)
    store.receive(track(**changes), received_at=0.0)
    assert discovery.discover(store, now=0.0) == ()
    assert discovery.task_ids == frozenset()


def test_discovery_requires_fresh_source_and_is_idempotent() -> None:
    catalog = build_discovery_catalog(
        publications(), first_task_id=2, width=10, height=10
    )
    discovery = ReceiverTaskDiscovery(
        {key: objective.task_id for key, objective in catalog.items()}
    )
    store = ReceiverObservations()
    store.receive(track(), received_at=0.0)
    assert discovery.discover(store, now=0.0) == ()
    store.receive(SourceStatus("camera_1", 100, "running"), received_at=0.0)
    assert discovery.discover(store, now=3.1) == ()
    store.receive(SourceStatus("camera_1", 102, "running"), received_at=4.0)
    store.receive(track(103), received_at=4.0)
    added = discovery.discover(store, now=4.0)
    assert len(added) == 1 and added[0].objective.task_id == 2
    assert discovery.discover(store, now=4.0) == ()
    store.receive(SourceStatus("camera_1", 104, "running", True), received_at=5.0)
    assert discovery.tasks == added  # Accepted inspection work is not tracking.


def make_sim(
    events: tuple[ObservationPublication, ...], **changes: object
) -> Simulation:
    config = SimulationConfig(
        agent_count=2,
        task_count=1,
        agent_speed=1.0,
        failure_time=100.0,
        failure_agent_id=1,
        max_simulation_time=12.0,
        communication_range=100.0,
        time_step=0.5,
        completion_tolerance=0.0,
    )
    config = replace(config, **changes)
    return Simulation(
        config,
        agents=(Agent(1, (0.0, 0.0), 1.0), Agent(2, (10.0, 0.0), 1.0)),
        tasks=(Task(1, (0.0, 0.0)),),
        observation_publications=events,
        capture_trace=True,
    )


def test_simulation_partition_limits_discovery_and_execution() -> None:
    sim = make_sim(publications(0.1), blocked_links=((1, 2),))
    result = sim.run()
    assert result.task_discoveries is not None
    assert result.task_discoveries[1].task_ids == {2}
    assert result.task_discoveries[2].task_ids == frozenset()
    assert all(
        d.allocation.task_id != 2
        for d in result.metrics.allocation_decisions
        if d.allocation.agent_id == 2
    )
    assert result.mission.tasks[2].status is TaskStatus.COMPLETED
    assert result.mission.tasks[2].assigned_agent == 1
    assert result.task_discoveries[1].tasks[0].discovered_at == 0.1
    assert result.trace is not None


def test_reconnect_with_new_publications_discovers_for_other_receiver() -> None:
    events = publications(0.1) + (
        ObservationPublication(1, 2.0, SourceStatus("camera_1", 102, "running")),
        ObservationPublication(1, 2.0, track(103)),
    )
    result = make_sim(
        events, comm_fault_agent_id=1, comm_fault_start=0.0, comm_fault_end=2.0
    ).run()
    assert result.task_discoveries is not None
    assert result.task_discoveries[2].tasks[0].discovered_at == 2.0
    assert any(
        r.receiver_agent_id == 2 and not r.delivered
        for r in result.observation_receipts
    )
    assert any(
        r.receiver_agent_id == 2 and r.accepted for r in result.observation_receipts
    )


def test_failed_source_cannot_publish_or_create_work() -> None:
    result = make_sim(publications(1.0), failure_time=1.0).run()
    assert result.task_discoveries is not None
    assert not any(store.task_ids for store in result.task_discoveries.values())
    assert result.mission.tasks[2].status is TaskStatus.UNASSIGNED
    assert not any(r.delivered for r in result.observation_receipts)


def test_observation_scenario_is_deterministic() -> None:
    left = make_sim(publications(0.1)).run()
    right = make_sim(publications(0.1)).run()
    assert left.mission.events == right.mission.events
    assert left.position_history == right.position_history
    assert left.metrics == right.metrics
    assert left.trace == right.trace


def test_same_source_track_id_is_not_cross_uav_identity() -> None:
    records = publications() + (
        ObservationPublication(2, 0.0, replace(track(), uav_id="camera_2")),
    )
    catalog = build_discovery_catalog(records, first_task_id=2, width=10, height=10)
    assert {o.task_id for o in catalog.values()} == {2, 3}


@pytest.mark.parametrize("position", [(11.0, 0.0), (0.0, -1.0)])
def test_out_of_area_positions_rejected(position: tuple[float, float]) -> None:
    with pytest.raises(ValueError, match="outside"):
        make_sim(
            (ObservationPublication(1, 0.0, track(mission_position_m=position)),),
            area_width=10.0,
            area_height=10.0,
        )


def test_moving_tracks_and_ambiguous_sources_rejected() -> None:
    with pytest.raises(ValueError, match="stationary"):
        make_sim(
            publications()
            + (
                ObservationPublication(
                    1, 1.0, track(102, mission_position_m=(6.0, 0.0))
                ),
            )
        )
    with pytest.raises(ValueError, match="exactly one UAV"):
        make_sim(publications() + (ObservationPublication(2, 1.0, track(102)),))
    with pytest.raises(ValueError, match="unknown observation source"):
        make_sim((ObservationPublication(3, 1.0, track()),))
    with pytest.raises(ValueError, match="distributed"):
        make_sim(publications(), task_control="centralized")


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "5"])
def test_invalid_mission_coordinate_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="finite"):
        parse_record(
            {
                "schema_version": 1,
                "uav_id": "camera_1",
                "track_id": 1,
                "timestamp_ms": 100,
                "class_label": "vehicle",
                "confidence": 1.0,
                "bbox_xyxy": [0, 0, 1, 1],
                "image_size": [2, 2],
                "track_status": "observed",
                "coordinate_frame": "mission_xy_m",
                "mission_position_m": [value, 0],
            }
        )


def test_demo_exports_deterministic_evidence(tmp_path) -> None:
    import json
    from pathlib import Path

    from eudis_swarm.observation_demo import main

    source = Path(__file__).parents[1] / "examples" / "isr_publications.jsonl"
    output = tmp_path / "evidence.json"
    assert main([str(source), "--output", str(output)]) == 0
    first = output.read_text()
    assert main([str(source), "--output", str(output)]) == 0
    assert first == output.read_text()
    evidence = json.loads(first)
    assert evidence["mission_completed"]
    assert evidence["total_tasks"] == evidence["completed_tasks"] == 7
    assert evidence["discoveries"]["1"][0]["discovered_at"] == 0.1
    assert evidence["discoveries"]["2"][0]["discovered_at"] == 2.0
    assert main([str(source)]) == 0


def test_demo_rejects_malformed_and_missing_logs(tmp_path) -> None:
    from eudis_swarm.observation_demo import load_publications, main

    bad = tmp_path / "bad.jsonl"
    bad.write_text('\n{"source_agent_id":true}\n')
    with pytest.raises(ValueError, match="line 2"):
        load_publications(bad)
    with pytest.raises(SystemExit):
        main([str(bad)])
    with pytest.raises(SystemExit):
        main([str(tmp_path / "absent.jsonl")])


def test_coordinate_frame_and_pair_are_required() -> None:
    payload = {
        "schema_version": 1,
        "uav_id": "camera_1",
        "track_id": 1,
        "timestamp_ms": 100,
        "class_label": "vehicle",
        "confidence": 1.0,
        "bbox_xyxy": [0, 0, 1, 1],
        "image_size": [2, 2],
        "track_status": "observed",
    }
    for changes in (
        {"mission_position_m": [1, 2]},
        {"coordinate_frame": "mission_xy_m"},
        {"mission_position_m": [1], "coordinate_frame": "mission_xy_m"},
    ):
        with pytest.raises(ValueError):
            parse_record({**payload, **changes})
    record = parse_record(
        {**payload, "mission_position_m": [1, 2], "coordinate_frame": "mission_xy_m"}
    )
    assert isinstance(record, TrackObservation) and record.mission_position_m == (
        1.0,
        2.0,
    )


def test_discovery_position_comes_only_from_delivered_evidence() -> None:
    store = ReceiverObservations()
    store.receive(SourceStatus("camera_1", 100, "running"), received_at=0.0)
    store.receive(track(), received_at=0.0)
    discovery = ReceiverTaskDiscovery({("camera_1", 17): 2})
    assert discovery.discover(store, now=0.0)[0].objective.position == (5.0, 0.0)
    with pytest.raises(ValueError, match="minimum_confidence"):
        ReceiverTaskDiscovery({}, minimum_confidence=1.1)
    with pytest.raises(ValueError, match="finite"):
        track(mission_position_m=(float("nan"), 0.0))


@pytest.mark.parametrize("timestamp", [-1.0, True, float("nan"), float("inf")])
def test_publication_time_validation(timestamp: float) -> None:
    with pytest.raises(ValueError, match="publication time"):
        ObservationPublication(1, timestamp, track())


def test_publications_use_simulator_clock_precision() -> None:
    assert ObservationPublication(1, 0.10000000000001, track()).published_at == 0.1
    assert ObservationPublication(1, 1e-13, track()).published_at == 0.0
