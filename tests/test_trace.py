"""Verify structured playback traces independently of the Streamlit interface.

The assertions keep observer data descriptive and separate from swarm decisions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.simulation import Simulation, main, run_simulation
from eudis_swarm.trace import SimulationTrace


def test_trace_capture_is_opt_in() -> None:
    assert run_simulation().trace is None


def test_default_trace_round_trip_preserves_failure_and_recovery(tmp_path) -> None:
    result = run_simulation(capture_trace=True)
    trace = result.trace
    assert trace is not None
    assert trace.frames[0].timestamp == 0.0
    assert trace.frames[-1].timestamp == pytest.approx(17.25)

    failure_frame = next(frame for frame in trace.frames if frame.timestamp == 4.0)
    failed_uav = next(agent for agent in failure_frame.agents if agent.agent_id == 2)
    assert failed_uav.physical_state == "FAILED"
    assert failed_uav.failure_detected is False

    detection_frame = next(frame for frame in trace.frames if frame.timestamp == 5.75)
    detected_uav = next(
        agent for agent in detection_frame.agents if agent.agent_id == 2
    )
    assert detected_uav.failure_detected is True
    assert {event.kind for event in detection_frame.events} >= {
        "HEARTBEAT_TIMEOUT",
        "FAILURE_DECLARED",
        "TASK_RELEASED",
    }

    final_metrics = trace.frames[-1].metrics
    assert final_metrics.protocol_messages_attempted == (
        result.metrics.protocol_messages_attempted
    )
    assert final_metrics.protocol_messages_delivered == (
        result.metrics.protocol_messages_delivered
    )
    assert final_metrics.protocol_messages_dropped == (
        result.metrics.protocol_messages_undelivered
    )
    assert final_metrics.protocol_messages_forwarded == (
        result.metrics.protocol_messages_forwarded
    )
    assert final_metrics.protocol_duplicates_suppressed == (
        result.metrics.protocol_duplicates_suppressed
    )
    assert final_metrics.protocol_unavailable_link_attempts == (
        result.metrics.protocol_messages_undelivered
    )
    assert final_metrics.protocol_useful_first_deliveries == (
        result.metrics.protocol_useful_first_deliveries
    )
    assert final_metrics.protocol_duplicate_source_publications == (
        result.metrics.protocol_duplicate_source_publications
    )
    assert final_metrics.protocol_duplicate_route_suppressions == (
        result.metrics.protocol_duplicate_route_suppressions
    )
    assert final_metrics.protocol_inactive_endpoint_deferrals == (
        result.metrics.protocol_inactive_endpoint_deferrals
    )
    assert final_metrics.protocol_messages_delivered > 0
    assert final_metrics.protocol_duplicates_suppressed > 0

    destination = tmp_path / "trace.json"
    trace.write_json(destination)
    restored = SimulationTrace.read_json(destination)
    assert restored == trace


def test_outage_trace_separates_physical_health_from_local_staleness() -> None:
    trace = run_simulation(
        SimulationConfig(
            failure_time=100.0,
            communication_range=130.0,
            comm_fault_agent_id=2,
            comm_fault_start=4.0,
            comm_fault_end=8.0,
            peer_state_stale_after=2.5,
        ),
        capture_trace=True,
    ).trace
    assert trace is not None

    stale_frame = next(frame for frame in trace.frames if frame.timestamp == 5.75)
    isolated_uav = next(agent for agent in stale_frame.agents if agent.agent_id == 2)
    observer = next(agent for agent in stale_frame.agents if agent.agent_id == 1)
    observation = next(
        peer for peer in observer.peer_knowledge if peer.peer_agent_id == 2
    )
    assert isolated_uav.physical_state == "FAILED"
    assert isolated_uav.neighbor_ids == ()
    assert observation.state == "STALE"
    assert observation.peer_status == "DECLARED_FAILED"
    assert observation.last_known_position is not None
    assert stale_frame.metrics.stale_peer_observations == 6

    restored_frame = next(frame for frame in trace.frames if frame.timestamp == 8.0)
    assert restored_frame.metrics.stale_peer_observations == 0
    assert (
        len(
            next(
                agent for agent in restored_frame.agents if agent.agent_id == 2
            ).neighbor_ids
        )
        == 3
    )


def test_connectivity_trace_explains_known_allocation_decision() -> None:
    trace = run_simulation(
        SimulationConfig(
            random_seed=1,
            failure_time=100.0,
            communication_range=35.0,
            allocation_policy="connectivity",
        ),
        capture_trace=True,
    ).trace
    assert trace is not None
    decisions = [
        event
        for frame in trace.frames
        for event in frame.events
        if event.timestamp == 4.5 and event.kind in {"TASK_ASSIGNED", "TASK_REASSIGNED"}
    ]
    assert len(decisions) == 1
    decision = decisions[0]
    assert (decision.agent_id, decision.task_id) == (2, 3)
    assert decision.policy == "connectivity"
    assert decision.distance == pytest.approx(24.20, abs=0.01)
    assert decision.predicted_peer_degree == 1
    assert decision.predicted_isolation is False


def test_trace_reader_rejects_unsupported_or_empty_documents(tmp_path) -> None:
    destination = tmp_path / "invalid.json"
    destination.write_text(
        json.dumps({"schema_version": 99, "metadata": {}, "frames": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported trace schema"):
        SimulationTrace.read_json(destination)

    valid = run_simulation(capture_trace=True).trace
    assert valid is not None
    document = valid.to_dict()
    document["frames"] = []
    with pytest.raises(ValueError, match="at least one frame"):
        SimulationTrace.from_dict(document)


def test_cli_record_trace_writes_loadable_artifact(tmp_path) -> None:
    destination = tmp_path / "recorded.trace.json"
    assert main(["--record-trace", str(destination), "--log-level", "ERROR"]) == 0
    trace = SimulationTrace.read_json(destination)
    assert trace.metadata.allocation_policy == "distance"
    assert trace.frames[-1].metrics.completed_tasks == 20


def test_trace_frame_carries_world_truth_belief_and_ownership_together() -> None:
    """One frame now records all three layers, so divergence is replayable.

    During the outage UAV 2 completes Task 20 locally while UAV 3 independently
    claims it. Neither can see the other evidence, so the disagreement is only
    visible by comparing replicas, which is exactly what the frame stores.
    """

    trace = (
        Simulation(
            SimulationConfig(
                failure_time=100.0,
                communication_range=130.0,
                comm_fault_agent_id=2,
                comm_fault_start=4.0,
                comm_fault_end=8.0,
            ),
            capture_trace=True,
        )
        .run()
        .trace
    )
    assert trace is not None
    assert trace.schema_version == 3

    disputed_frames = [frame for frame in trace.frames if frame.disputed_task_ids]
    assert disputed_frames, "the split brain never appeared in the trace"

    frame = next(item for item in disputed_frames if 20 in item.disputed_task_ids)

    # world truth and belief live side by side in the same frame.
    uav2 = next(agent for agent in frame.agents if agent.agent_id == 2)
    assert uav2.responsive is True

    owners = {
        view.known_owner_agent_id for view in frame.ownership if view.task_id == 20
    }
    assert owners == {2, 3}

    # and the disagreement resolves by the end of the mission.
    assert trace.frames[-1].disputed_task_ids == ()


def test_trace_carries_composed_autonomy_state_and_ordered_transitions() -> None:
    trace = run_simulation(
        SimulationConfig(
            failure_time=100.0,
            communication_range=130.0,
            comm_fault_agent_id=2,
            comm_fault_start=4.0,
            comm_fault_end=8.0,
        ),
        capture_trace=True,
    ).trace
    assert trace is not None

    every_transition = [
        transition for frame in trace.frames for transition in frame.transitions
    ]
    assert every_transition
    assert {transition.machine for transition in every_transition} >= {
        "ContactEFSM",
        "PeerAvailabilityEFSM",
        "TaskOwnershipEFSM",
        "CoordinationModeEFSM",
    }
    assert all(
        transition.guard and transition.reason for transition in every_transition
    )
    for observer_agent_id in range(1, 5):
        sequence = [
            transition.sequence
            for transition in every_transition
            if transition.observer_agent_id == observer_agent_id
        ]
        assert sequence == list(range(len(sequence)))

    local_autonomy = next(
        transition
        for transition in every_transition
        if transition.machine == "CoordinationModeEFSM"
        and transition.observer_agent_id == 2
        and transition.next_state == "LOCAL_AUTONOMY"
    )
    assert local_autonomy.effects == ("ENTER_LOCAL_AUTONOMY",)
    frame = next(item for item in trace.frames if item.timestamp == 7.5)
    modes = {agent.agent_id: agent.coordination_mode for agent in frame.agents}
    assert modes[2] == "LOCAL_AUTONOMY"
    assert len(set(modes.values())) > 1
    assert all(len(agent.contact_states) == 3 for agent in frame.agents)


def test_ownership_survives_a_json_round_trip(tmp_path: Path) -> None:
    original = (
        Simulation(
            SimulationConfig(
                failure_time=100.0,
                communication_range=130.0,
                comm_fault_agent_id=2,
                comm_fault_start=4.0,
                comm_fault_end=8.0,
            ),
            capture_trace=True,
        )
        .run()
        .trace
    )
    assert original is not None

    destination = tmp_path / "ownership.trace.json"
    original.write_json(destination)
    restored = SimulationTrace.read_json(destination)

    assert [frame.ownership for frame in restored.frames] == [
        frame.ownership for frame in original.frames
    ]
    assert [frame.disputed_task_ids for frame in restored.frames] == [
        frame.disputed_task_ids for frame in original.frames
    ]
