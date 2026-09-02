"""Verify structured playback traces independently of the Streamlit interface.

The assertions keep observer data descriptive and separate from swarm decisions.
"""

from __future__ import annotations

import json

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.simulation import main, run_simulation
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
    # the link returns but the declared UAV is stopped, so the views stay stale.
    assert restored_frame.metrics.stale_peer_observations == 6
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
