"""Description gossip preserves private evidence and permits lease-based recovery."""

from dataclasses import replace

import pytest

from eudis_swarm.agent import Agent
from eudis_swarm.communication import CommunicationGraph
from eudis_swarm.config import SimulationConfig
from eudis_swarm.observation_replay import SourceStatus, TrackObservation
from eudis_swarm.observation_transport import ObservationPublication
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import Task, TaskStatus
from eudis_swarm.task_description_transport import TaskDescriptionTransport
from eudis_swarm.task_discovery import ReceiverTaskDiscovery, TaskDescription
from eudis_swarm.task_utility import TaskObjective


def description() -> TaskDescription:
    return TaskDescription(TaskObjective(2, (5.0, 0.0)), "camera_1", 17)


def test_each_round_is_one_hop_and_cycles_are_suppressed() -> None:
    graph = CommunicationGraph((1, 2, 3), 5.0)
    graph.update({1: (0.0, 0.0), 2: (4.0, 0.0), 3: (8.0, 0.0)})
    stores = {i: ReceiverTaskDiscovery({("camera_1", 17): 2}) for i in (1, 2, 3)}
    stores[1].receive_description(description(), received_at=0.0, forwarder_agent_id=1)
    transport = TaskDescriptionTransport(graph, stores)
    first = transport.exchange(now=0.0, participants=(1, 2, 3))
    assert stores[2].task_ids == {2}
    assert not stores[3].task_ids
    assert any(
        r.forwarder_agent_id == 1 and r.receiver_agent_id == 3 and not r.delivered
        for r in first
    )
    second = transport.exchange(now=0.0, participants=(1, 2, 3))
    assert stores[3].task_ids == {2}
    assert stores[3].tasks[0].learned_via == "task_description"
    assert stores[3].tasks[0].forwarder_agent_id == 2
    assert any(
        r.forwarder_agent_id == 2 and r.receiver_agent_id == 3 and r.accepted
        for r in second
    )
    receipts = second
    for _ in range(3):
        receipts = transport.exchange(now=0.0, participants=(1, 2, 3))
    assert not any(r.delivered for r in receipts)
    assert stores[3].tasks[0].discovered_at == 0.0


def test_blocked_routes_retry_after_reconnect_without_live_source() -> None:
    graph = CommunicationGraph((1, 2, 3), 20.0)
    positions = {1: (0.0, 0.0), 2: (4.0, 0.0), 3: (8.0, 0.0)}
    graph.update(positions, blocked_links=((1, 3), (2, 3)))
    stores = {i: ReceiverTaskDiscovery({("camera_1", 17): 2}) for i in (1, 2, 3)}
    stores[1].receive_description(description(), received_at=0.0, forwarder_agent_id=1)
    transport = TaskDescriptionTransport(graph, stores)
    transport.exchange(now=0.0, participants=(1, 2, 3))
    assert not stores[3].task_ids
    graph.update(positions)
    receipts = transport.exchange(now=2.0, participants=(2, 3))  # source has failed
    assert stores[3].task_ids == {2}
    assert stores[3].tasks[0].discovered_at == 2.0
    assert all(r.forwarder_agent_id != 1 for r in receipts)
    assert not transport.exchange(now=3.0, participants=())
    with pytest.raises(ValueError, match="endpoint"):
        transport.exchange(now=3.0, participants=(4,))
    with pytest.raises(ValueError, match="backwards"):
        transport.exchange(now=1.0, participants=(2, 3))


def test_identity_conflicts_duplicates_and_clock_validation() -> None:
    store = ReceiverTaskDiscovery({("camera_1", 17): 2})
    assert store.receive_description(
        description(), received_at=1.0, forwarder_agent_id=1
    )
    assert not store.receive_description(
        description(), received_at=2.0, forwarder_agent_id=3
    )
    assert store.tasks[0].discovered_at == 1.0
    assert store.tasks[0].forwarder_agent_id == 1
    with pytest.raises(ValueError, match="conflicting"):
        store.receive_description(
            replace(description(), objective=TaskObjective(2, (6.0, 0.0))),
            received_at=2.0,
            forwarder_agent_id=3,
        )
    with pytest.raises(ValueError, match="reserved"):
        store.receive_description(
            replace(description(), track_id=18), received_at=2.0, forwarder_agent_id=3
        )
    with pytest.raises(ValueError, match="backwards"):
        store.receive_description(description(), received_at=0.0, forwarder_agent_id=1)
    with pytest.raises(ValueError, match="forwarder"):
        store.receive_description(description(), received_at=2.0, forwarder_agent_id=0)


@pytest.mark.parametrize(
    "changes", [{"track_id": True}, {"track_id": -1}, {"source_uav_id": ""}]
)
def test_invalid_description_metadata_rejected(changes: dict) -> None:
    with pytest.raises(ValueError):
        replace(description(), **changes)


def scenario(*, share: bool, relay_busy: bool = False) -> Simulation:
    config = SimulationConfig(
        agent_count=3,
        task_count=2 if relay_busy else 1,
        agent_speed=1.0,
        communication_range=5.0,
        failure_agent_id=1,
        failure_time=1.0,
        max_simulation_time=30.0 if relay_busy else 12.0,
        completion_tolerance=0.0,
        time_step=0.5,
    )
    status = SourceStatus("camera_1", 100, "running")
    observation = TrackObservation(
        "camera_1",
        17,
        101,
        "vehicle",
        0.9,
        (0, 0, 10, 10),
        (20, 20),
        "observed",
        (5.0, 0.0),
    )
    publications = (
        ObservationPublication(1, 0.1, status),
        ObservationPublication(1, 0.1, observation),
    )
    return Simulation(
        config,
        agents=tuple(Agent(i, ((i - 1) * 4.0, 0.0), 1.0) for i in (1, 2, 3)),
        tasks=(
            (Task(1, (0.0, 0.0)), Task(2, (4.0, 20.0)))
            if relay_busy
            else (Task(1, (0.0, 0.0)),)
        ),
        observation_publications=publications,
        share_discovered_tasks=share,
        capture_trace=True,
    )


def test_multihop_description_does_not_copy_live_track_and_survives_source_failure() -> (
    None
):
    result = scenario(share=True).run()
    assert result.metrics.mission_completed
    assert result.mission.tasks[2].status is TaskStatus.COMPLETED
    assert result.mission.tasks[2].assigned_agent == 2
    assert result.task_discoveries is not None and result.observation_stores is not None
    assert result.task_discoveries[3].tasks[0].learned_via == "task_description"
    assert result.task_discoveries[3].tasks[0].forwarder_agent_id == 2
    assert result.observation_stores[3].snapshot(now=12.0) == {
        "sources": {},
        "tracks": [],
    }
    assert all(
        not r.delivered for r in result.observation_receipts if r.receiver_agent_id == 3
    )
    assert any(
        r.forwarder_agent_id == 2 and r.receiver_agent_id == 3 and r.accepted
        for r in result.task_description_receipts
    )
    # The failed owner's claim expires before a surviving UAV acquires the work.
    assignments = [
        r
        for r in result.metrics.allocation_decisions
        if r.allocation.task_id == 2 and r.allocation.agent_id == 2
    ]
    assert assignments and assignments[0].timestamp > 0.1 + 2.5


def test_disabled_sharing_retains_one_hop_observation_boundary() -> None:
    result = scenario(share=False).run()
    assert result.task_discoveries is not None
    assert not result.task_discoveries[3].task_ids
    assert not result.task_description_receipts


def test_sharing_scenario_is_deterministic() -> None:
    a, b = scenario(share=True).run(), scenario(share=True).run()
    assert a.mission.events == b.mission.events
    assert a.position_history == b.position_history
    assert a.task_description_receipts == b.task_description_receipts
    assert a.metrics == b.metrics


def test_sharing_flag_requires_boolean() -> None:
    with pytest.raises(ValueError, match="boolean"):
        Simulation(SimulationConfig(), share_discovered_tasks=1)  # type: ignore[arg-type]


def test_receiver_without_raw_observation_can_take_over_failed_source_work() -> None:
    result = scenario(share=True, relay_busy=True).run()
    assert result.metrics.mission_completed
    assert result.mission.tasks[3].status is TaskStatus.COMPLETED
    assert result.mission.tasks[3].assigned_agent == 3
    assert result.observation_stores is not None and result.task_discoveries is not None
    assert result.observation_stores[3].snapshot(now=30.0) == {
        "sources": {},
        "tracks": [],
    }
    assert result.task_discoveries[3].tasks[0].learned_via == "task_description"
    assignments = [
        r
        for r in result.metrics.allocation_decisions
        if r.allocation.task_id == 3 and r.allocation.agent_id == 3
    ]
    assert assignments[0].timestamp > 2.6
    unshared = scenario(share=False, relay_busy=True).run()
    assert not unshared.metrics.mission_completed
    assert unshared.mission.tasks[3].status is not TaskStatus.COMPLETED


def test_recovery_demo_exports_paired_measurements(tmp_path) -> None:
    import json
    from pathlib import Path

    from eudis_swarm.observation_demo import main

    fixture = Path(__file__).parents[1] / "examples" / "isr_recovery_publications.jsonl"
    shared_path = tmp_path / "shared.json"
    unshared_path = tmp_path / "unshared.json"
    assert (
        main(
            [str(fixture), "--scenario", "relay-recovery", "--output", str(shared_path)]
        )
        == 0
    )
    assert (
        main(
            [
                str(fixture),
                "--scenario",
                "relay-recovery",
                "--no-task-sharing",
                "--output",
                str(unshared_path),
            ]
        )
        == 0
    )
    shared, unshared = (
        json.loads(shared_path.read_text()),
        json.loads(unshared_path.read_text()),
    )
    assert shared["completed_tasks"] == 3 and shared["duration_s"] == 20.0
    assert shared["mission_completed"]
    assert unshared["completed_tasks"] == 2 and unshared["duration_s"] == 30.0
    assert not unshared["mission_completed"]
    assert shared["discoveries"]["3"][0]["learned_via"] == "task_description"
    assert not any(
        r["delivered"]
        for r in shared["observation_receipts"]
        if r["receiver_agent_id"] == 3
    )
