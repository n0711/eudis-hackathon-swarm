"""The instrumented mission and its evidence log, with no autopilot attached.

The two properties that matter most: instrumenting a mission must not change
it (the point-mass run is the deterministic comparison baseline), and a live
run must log the drift and the fail-stop it is there to measure.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from dataclasses import replace

import pytest
from fake_fleet import FakeAutopilot, FakeWorld

from eudis_swarm.communication import RegionJammer
from eudis_swarm.config import SimulationConfig
from eudis_swarm.live_mission import (
    LOG_SCHEMA_VERSION,
    config_record,
    read_jsonl,
    run_logged,
    write_jsonl,
)
from eudis_swarm.mavlink_backend import MavlinkBackend
from eudis_swarm.simulation import Simulation, build_scenario


def _config(**changes: object) -> SimulationConfig:
    base = replace(
        SimulationConfig(),
        agent_count=4,
        task_count=8,
        task_control="distributed",
        max_simulation_time=120.0,
        completion_tolerance=2.0,
        failure_agent_id=2,
        failure_time=4.0,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _fleet(config: SimulationConfig) -> dict[int, FakeAutopilot]:
    world = FakeWorld()
    agents, _tasks = build_scenario(config)
    return {
        agent.agent_id: FakeAutopilot(
            world, agent.position, offset_s=20.0 + agent.agent_id
        )
        for agent in agents
    }


@pytest.mark.parametrize(
    "config",
    [
        SimulationConfig(),
        _config(),
        _config(region_jammer=RegionJammer(50.0, 50.0, 30.0, 2.0, 5.0)),
        _config(comm_fault_agent_id=3, comm_fault_start=1.0, comm_fault_end=3.0),
    ],
    ids=["default", "swarm-mission", "jammer", "comm-fault"],
)
def test_instrumenting_a_point_mass_mission_changes_nothing(
    config: SimulationConfig,
) -> None:
    plain = Simulation(config).run()
    logged, records = run_logged(config, run_label="point-mass")

    assert logged.metrics.simulation_duration == plain.metrics.simulation_duration
    assert logged.metrics.completed_task_ids == plain.metrics.completed_task_ids
    assert (
        logged.metrics.concurrent_ownership_duration
        == plain.metrics.concurrent_ownership_duration
    )
    assert logged.position_history == plain.position_history
    assert [event.kind for event in logged.mission.events] == [
        event.kind for event in plain.mission.events
    ]
    assert records[0]["type"] == "header" and records[-1]["type"] == "result"


def test_the_default_mission_still_takes_its_published_seventeen_seconds() -> None:
    result, _records = run_logged(SimulationConfig(), run_label="point-mass")
    assert result.metrics.simulation_duration == 17.25


def test_a_point_mass_log_is_byte_identical_across_runs() -> None:
    def render() -> str:
        _result, records = run_logged(_config(), run_label="point-mass")
        stream = io.StringIO()
        write_jsonl(records, stream)
        return stream.getvalue()

    assert render() == render()


def test_the_log_records_every_receivers_view_of_every_task() -> None:
    config = _config()
    _result, records = run_logged(config, run_label="point-mass")
    header, steps = records[0], [r for r in records if r["type"] == "step"]

    assert header["schema"] == LOG_SCHEMA_VERSION
    assert header["deterministic"] is True
    assert header["backend"] == "point-mass"
    assert header["task_ids"] == list(range(1, 9))
    assert steps[0]["t"] == 0.0
    assert all(step["vehicles"] == [] for step in steps)
    for step in steps:
        assert sorted(step["ownership"]) == ["1", "2", "3", "4"]
        for rows in step["ownership"].values():
            assert [row[0] for row in rows] == list(range(1, 9))
    kinds = [event["kind"] for step in steps for event in step["events"]]
    assert "FAILURE_INJECTED" in kinds and "FAILURE_DECLARED" in kinds
    assert "FAIL_STOP" not in kinds  # no live vehicle, nothing to hold


def test_a_failed_uavs_replica_learns_nothing_after_it_goes_silent() -> None:
    """Belief and truth stay separate in the log as in the mission.

    The silent UAV's view may still *age* (claims go stale on its own clock),
    but it receives no new evidence: its set of known completions is frozen.
    """

    _result, records = run_logged(_config(), run_label="point-mass")
    steps = [r for r in records if r["type"] == "step"]
    after = [step for step in steps if step["t"] >= 4.0]

    def completions(step: dict, receiver: str) -> set[int]:
        return {row[0] for row in step["ownership"][receiver] if row[1] == "COMPLETE"}

    assert not any(
        agent["responsive"]
        for step in after
        for agent in step["agents"]
        if agent["id"] == 2
    )
    frozen = completions(after[0], "2")
    assert all(completions(step, "2") == frozen for step in after)
    assert completions(after[-1], "1") > frozen  # survivors kept learning


def test_a_live_mission_logs_drift_and_the_one_fail_stop() -> None:
    config = _config()
    vehicles = _fleet(config)
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)

    result, records = run_logged(config, run_label="sitl", vehicle_backend=backend)

    assert result.metrics.completed_task_count == 8
    header = records[0]
    assert header["deterministic"] is False
    assert header["integrated_agent_ids"] == [1, 2, 3, 4]
    assert header["fail_stop_mode"] == "BRAKE"
    steps = [r for r in records if r["type"] == "step"]
    fail_stops = [
        (step["t"], event)
        for step in steps
        for event in step["events"]
        if event["kind"] == "FAIL_STOP"
    ]
    assert len(fail_stops) == 1
    logged_at, event = fail_stops[0]
    assert logged_at == 4.0 and event["t"] == 4.0
    assert event["agent_id"] == 2 and event["mode"] == "BRAKE" and event["confirmed"]
    assert vehicles[2].holds == ["BRAKE"]
    assert all(vehicles[agent].holds == [] for agent in (1, 3, 4))

    stepped = [v for step in steps for v in step["vehicles"] if not v["held"]]
    held = [v for step in steps for v in step["vehicles"] if v["held"]]
    assert {v["id"] for v in stepped} == {1, 2, 3, 4}
    assert all(v["drift_s"] >= 0.0 for v in stepped)
    assert max(v["drift_s"] for v in stepped) < 0.5
    assert {v["id"] for v in held} == {2}
    assert len({tuple(v["position"]) for v in held}) == 1  # it stopped
    # One stepped sample per live, responsive vehicle per mission boundary.
    for step in steps[1:]:
        ids = sorted(v["id"] for v in step["vehicles"] if not v["held"])
        alive = sorted(a["id"] for a in step["agents"] if a["responsive"])
        assert ids == alive or (step["t"] == 4.0 and ids == [1, 2, 3, 4])


def test_a_subset_of_live_agents_leaves_the_rest_on_point_mass() -> None:
    config = _config()
    vehicles = _fleet(config)
    backend = MavlinkBackend({3: vehicles[3]}, step_timeout=5.0)

    _result, records = run_logged(config, run_label="sitl", vehicle_backend=backend)

    steps = [r for r in records if r["type"] == "step"]
    assert {v["id"] for step in steps for v in step["vehicles"]} == {3}
    assert vehicles[2].holds == []  # agent 2 failed, but it was never live
    assert all(
        event["kind"] != "FAIL_STOP" for step in steps for event in step["events"]
    )


def test_jsonl_round_trips_and_rejects_garbage() -> None:
    _result, records = run_logged(_config(task_count=2), run_label="point-mass")
    stream = io.StringIO()
    write_jsonl(records, stream)
    stream.seek(0)
    assert read_jsonl(stream) == records
    assert read_jsonl(io.StringIO("\n\n")) == []

    with pytest.raises(ValueError, match="line 2: not JSON"):
        read_jsonl(io.StringIO('{"type": "header"}\n{oops\n'))
    with pytest.raises(ValueError, match="line 1: not an evidence-log record"):
        read_jsonl(io.StringIO("[1, 2]\n"))


def test_the_config_record_is_plain_json() -> None:
    config = _config(
        region_jammer=RegionJammer(50.0, 50.0, 30.0, 2.0, 5.0),
        blocked_links=frozenset({(1, 2)}),
    )
    rendered = config_record(config)
    assert json.loads(json.dumps(rendered)) == rendered
    assert rendered["region_jammer"]["radius"] == 30.0
    assert rendered["blocked_links"] == [[1, 2]]


def test_the_simulator_never_reaches_the_live_harness() -> None:
    probe = (
        "import sys; import eudis_swarm.simulation, eudis_swarm.sweep;"
        "mods = ('eudis_swarm.live_mission', 'eudis_swarm.live_report',"
        " 'eudis_swarm.sitl_swarm', 'eudis_swarm.sitl', 'pymavlink');"
        "print(','.join(m for m in mods if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


def test_the_live_harness_never_imports_pymavlink_on_import() -> None:
    probe = (
        "import sys; import eudis_swarm.live_mission, eudis_swarm.live_report,"
        " eudis_swarm.sitl_swarm, eudis_swarm.mavlink_backend;"
        "print(','.join(m for m in ('pymavlink', 'eudis_swarm.sitl')"
        " if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
