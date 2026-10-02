"""The evidence-log report: every number derived from records alone."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest
from fake_fleet import FakeAutopilot, FakeWorld

from eudis_swarm.config import SimulationConfig
from eudis_swarm.live_mission import Record, run_logged, write_jsonl
from eudis_swarm.live_report import (
    _separation,
    check_test_card,
    format_checklist,
    format_comparison,
    format_vehicle_table,
    main,
    split_live_and_baseline,
    step_separation,
    summarize,
    summarize_run,
)
from eudis_swarm.mavlink_backend import MavlinkBackend
from eudis_swarm.simulation import build_scenario


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


def _live_records(config: SimulationConfig) -> list[Record]:
    world = FakeWorld()
    agents, _tasks = build_scenario(config)
    vehicles = {
        agent.agent_id: FakeAutopilot(world, agent.position) for agent in agents
    }
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)
    _result, records = run_logged(config, run_label="sitl", vehicle_backend=backend)
    return records


def test_a_point_mass_summary_matches_its_metrics_exactly() -> None:
    result, records = run_logged(_config(), run_label="point-mass")
    (summary,) = summarize(records)

    assert summary.run == "point-mass" and summary.deterministic
    assert summary.completed_task_count == result.metrics.completed_task_count == 8
    assert summary.mission_time_s == result.metrics.simulation_duration == 8.5
    assert summary.failed_agent_id == 2 and summary.failure_time_s == 4.0
    assert summary.failure_detection_latency_s == 1.75
    assert summary.orphaned_task_ids == (5,)
    assert summary.recovered and summary.recovery_latency_s == 2.5
    assert summary.concurrent_ownership_task_s == 0.0
    assert summary.vehicles == () and summary.fail_stops == ()


def test_recovery_is_measured_from_surviving_receivers_views() -> None:
    """Pinned against the log itself: the first survivor owning the orphan."""

    _result, records = run_logged(_config(), run_label="point-mass")
    steps = [r for r in records if r["type"] == "step"]
    first = next(
        step["t"]
        for step in steps
        if step["t"] >= 4.0
        and any(
            row[0] == 5 and row[1] in ("OWNED_BY_SELF", "COMPLETE")
            for receiver in ("1", "3", "4")
            for row in step["ownership"][receiver]
        )
    )
    (summary,) = summarize(records)
    assert summary.recovery_latency_s == first - 4.0
    # The failed UAV's own (frozen) replica can never count as recovery.
    assert all(
        row[1] == "OWNED_BY_SELF"
        for step in steps
        if step["t"] == 4.0
        for row in step["ownership"]["2"]
        if row[0] == 5
    )


def test_a_mission_without_failure_or_orphans_says_so() -> None:
    _result, records = run_logged(_config(failure_time=1000.0), run_label="no-failure")
    (summary,) = summarize(records)
    assert summary.failed_agent_id is None and summary.recovery_latency_s is None
    assert "no failure" in format_comparison([summary])

    _result, records = run_logged(_config(failure_time=0.0), run_label="at-zero")
    (summary,) = summarize(records)
    assert summary.orphaned_task_ids == ()
    assert "nothing orphaned" in format_comparison([summary])


def test_an_unrecovered_orphan_is_reported_as_such() -> None:
    _result, records = run_logged(_config(), run_label="point-mass")
    doctored = copy.deepcopy(records)
    for record in doctored:
        if record["type"] != "step" or record["t"] < 4.0:
            continue
        for receiver, rows in record["ownership"].items():
            if receiver != "2":
                for row in rows:
                    if row[0] == 5:
                        row[1] = "CLAIMED_BY_PEER_STALE"
    (summary,) = summarize(doctored)
    assert summary.orphaned_task_ids == (5,)
    assert not summary.recovered and summary.recovery_latency_s is None
    assert "NOT recovered" in format_comparison([summary])


def test_a_failure_after_the_last_step_orphans_nothing() -> None:
    _result, records = run_logged(_config(), run_label="point-mass")
    trimmed = [r for r in records if r["type"] != "step" or r["t"] < 4.0]
    trimmed.insert(
        2,
        {
            "type": "step",
            "run": "point-mass",
            "t": 0.1,
            "agents": [],
            "vehicles": [],
            "ownership": {},
            "events": [{"kind": "FAILURE_INJECTED", "t": 99.0, "agent_id": 2}],
        },
    )
    (summary,) = summarize(trimmed)
    assert summary.orphaned_task_ids == () and summary.failure_time_s == 99.0


def test_live_and_point_mass_side_by_side() -> None:
    config = _config()
    _result, point_mass = run_logged(config, run_label="point-mass")
    records = [*_live_records(config), *point_mass]

    live, baseline = summarize(records)

    assert (live.run, baseline.run) == ("sitl", "point-mass")
    assert live.backend == "mavlink" and not live.deterministic
    assert live.integrated_agent_ids == (1, 2, 3, 4)
    assert [v.agent_id for v in live.vehicles] == [1, 2, 3, 4]
    assert all(v.max_drift_s >= v.mean_drift_s >= 0.0 for v in live.vehicles)
    (fail_stop,) = live.fail_stops
    assert fail_stop.agent_id == 2 and fail_stop.mode == "BRAKE"
    assert fail_stop.confirmed
    assert fail_stop.residual_speed_mps == 0.0
    assert fail_stop.displacement_after_hold_m is not None
    assert fail_stop.observed_for_s is not None and fail_stop.observed_for_s > 0.0

    table = format_comparison([live, baseline])
    assert "sitl (NON-deterministic)" in table
    assert "point-mass (deterministic)" in table
    assert "UAV 2 BRAKE at 4.00 s (confirmed" in table
    assert "never in docs/measured_results.md" in table
    vehicles = format_vehicle_table(live)
    assert vehicles.count("\n") == 6
    assert format_vehicle_table(baseline) == "point-mass: no live vehicles"


def test_a_deterministic_only_table_has_no_banner() -> None:
    _result, records = run_logged(_config(), run_label="point-mass")
    table = format_comparison(summarize(records))
    assert "NON-deterministic" not in table
    with pytest.raises(ValueError, match="no runs"):
        format_comparison([])


def test_a_live_run_with_fail_stop_disabled_says_none_sent() -> None:
    config = _config()
    world = FakeWorld()
    agents, _tasks = build_scenario(config)
    vehicles = {a.agent_id: FakeAutopilot(world, a.position) for a in agents}
    backend = MavlinkBackend(dict(vehicles), fail_stop_mode=None)
    _result, records = run_logged(config, run_label="sitl", vehicle_backend=backend)

    (summary,) = summarize(records)
    assert summary.fail_stops == ()
    assert "none sent" in format_comparison([summary])


def test_malformed_logs_are_refused_with_a_reason() -> None:
    _result, records = run_logged(_config(task_count=2), run_label="point-mass")
    without_result = [r for r in records if r["type"] != "result"]
    with pytest.raises(ValueError, match="expected one result record, found 0"):
        summarize_run("point-mass", without_result)
    old = copy.deepcopy(records)
    old[0]["schema"] = 0
    with pytest.raises(ValueError, match="log schema 0"):
        summarize(old)


def test_the_command_line_report(tmp_path: Path, capsys) -> None:
    config = _config()
    _result, point_mass = run_logged(config, run_label="point-mass")
    live_path = tmp_path / "live.jsonl"
    baseline_path = tmp_path / "baseline.jsonl"
    with live_path.open("w", encoding="utf-8") as stream:
        write_jsonl(_live_records(config), stream)
    with baseline_path.open("w", encoding="utf-8") as stream:
        write_jsonl(point_mass, stream)

    assert main(["--combine", str(live_path), str(baseline_path)]) == 0
    printed = capsys.readouterr().out
    assert "| Tasks completed | 8/8 | 8/8 |" in printed
    assert "sitl: lock-step per vehicle" in printed

    broken = tmp_path / "broken.jsonl"
    with broken.open("w", encoding="utf-8") as stream:
        write_jsonl([r for r in point_mass if r["type"] != "result"], stream)
    assert main([str(broken)]) == 2
    assert "expected one result record" in capsys.readouterr().err


def _crossing_step(t: float, vehicles: list[tuple[int, float, float, float | None]]):
    return {
        "type": "step",
        "run": "sitl",
        "t": t,
        "agents": [],
        "vehicles": [
            {"id": agent, "held": False, "position": [x, y], "altitude_m": alt}
            for agent, x, y, alt in vehicles
        ],
        "ownership": {},
        "events": [],
    }


def test_separation_on_a_constructed_crossing() -> None:
    """Two copters cross the same point 5 m apart in altitude; a rover below."""

    steps = [
        _crossing_step(
            t,
            [
                (1, -10.0 + 10.0 * t, 0.0, 20.0),
                (2, 0.0, -10.0 + 10.0 * t, 25.0),
                (3, 0.0, 0.0, 0.0),
            ],
        )
        for t in (0.0, 0.5, 1.0, 1.5, 2.0)
    ]
    distance, pair = step_separation(steps[2])  # type: ignore[misc]
    assert (distance, pair) == (5.0, (1, 2))  # tracks cross: altitude is all
    minimum, which, at, below, counted = _separation(steps, 5.0)
    assert (minimum, which, at, below, counted) == (5.0, (1, 2), 1.0, 0, 5)
    assert _separation(steps, 6.0)[3] == 1  # only the crossing instant is < 6 m

    # Same crossing at one altitude: a conflict, and the metric says so.
    flat = [
        _crossing_step(
            t, [(1, -10.0 + 10.0 * t, 0.0, 20.0), (2, 0.0, -10.0 + 10.0 * t, 20.0)]
        )
        for t in (0.0, 1.0, 2.0)
    ]
    assert _separation(flat, 5.0)[:4] == (0.0, (1, 2), 1.0, 1)


def test_separation_ignores_vehicles_without_altitude_and_dedupes_a_step() -> None:
    step = _crossing_step(0.0, [(1, 0.0, 0.0, 20.0), (2, 1.0, 0.0, None)])
    assert step_separation(step) is None
    step = _crossing_step(
        0.0, [(1, 0.0, 0.0, 20.0), (1, 0.0, 0.0, 22.0), (2, 0.0, 0.0, 30.0)]
    )
    assert step_separation(step) == (8.0, (1, 2))  # later record of UAV 1 wins


def test_the_live_table_reports_separation_and_types() -> None:
    config = _config()
    world = FakeWorld()
    agents, _tasks = build_scenario(config)
    vehicles = {a.agent_id: FakeAutopilot(world, a.position) for a in agents}
    for index, vehicle in enumerate(vehicles.values()):
        vehicle.altitude_m = 20.0 + 5.0 * index
    backend = MavlinkBackend(dict(vehicles))
    _result, records = run_logged(
        config,
        run_label="sitl",
        vehicle_backend=backend,
        vehicle_types={1: "copter", 2: "copter", 3: "copter", 4: "copter"},
    )

    (summary,) = summarize(records, separation_threshold_m=100.0)
    assert summary.min_separation_m is not None and summary.min_separation_m >= 5.0
    assert summary.steps_below_separation == summary.separation_steps > 0
    table = format_comparison([summary])
    assert "| Vehicle types | 1:copter, 2:copter, 3:copter, 4:copter |" in table
    assert "steps < 100 m" in table
    with pytest.raises(ValueError, match="threshold"):
        summarize(records, separation_threshold_m=0.0)

    _result, point_mass = run_logged(config, run_label="point-mass")
    (baseline,) = summarize(point_mass)
    assert baseline.min_separation_m is None
    assert "| Min 3-D separation (live) | n/a |" in format_comparison([baseline])
    assert "| Vehicle types | n/a |" in format_comparison([baseline])


RECORDED = Path(__file__).resolve().parents[1] / "docs" / "data" / "sitl" / "phase5"


@pytest.mark.parametrize("run", ["c5", "c5b", "c5c", "mix5", "jam5"])
def test_the_recorded_phase5_flights_still_meet_the_test_card(run: str) -> None:
    """docs/sitl.md cites these logs; the report must keep reading them.

    Real ArduPilot SITL runs, recorded once. The numbers are not
    reproducible, so this pins only the pass criteria the doc claims.
    """

    from eudis_swarm.live_mission import read_jsonl

    with (RECORDED / f"{run}.jsonl").open(encoding="utf-8") as stream:
        live, baseline = summarize(read_jsonl(stream))

    assert not live.deterministic and baseline.deterministic
    assert live.mission_completed and live.completed_task_count == 8  # P1
    (fail_stop,) = live.fail_stops  # P2: exactly one
    assert fail_stop.confirmed
    assert fail_stop.residual_speed_mps is not None
    assert fail_stop.residual_speed_mps < 0.5  # P3
    assert live.recovered  # P4
    assert live.concurrent_ownership_task_s == 0.0  # P5
    assert live.steps_below_separation == 0  # P6
    assert max(v.max_drift_s for v in live.vehicles) < 1.0  # P7
    assert baseline.completed_task_count == 8  # P8


# --- the LT3 test-card checker -------------------------------------------------


def _recorded(run: str) -> list[Record]:
    from eudis_swarm.live_mission import read_jsonl

    with (RECORDED / f"{run}.jsonl").open(encoding="utf-8") as stream:
        return read_jsonl(stream)


def test_a_recorded_flight_passes_every_criterion() -> None:
    live, baseline = split_live_and_baseline(summarize(_recorded("c5")))
    assert live is not None and baseline is not None
    results = check_test_card(live, baseline)
    assert [r.criterion for r in results] == [f"P{n}" for n in range(1, 9)]
    assert all(r.passed for r in results)
    text = format_checklist("c5", results)
    assert text.startswith("c5: LT3 test card PASS")
    assert "[x] P3 the held vehicle actually stopped: final speed 0.00 m/s" in text


def _failed(results) -> list[str]:  # type: ignore[no-untyped-def]
    return [r.criterion for r in results if not r.passed]


def test_each_criterion_can_fail_on_its_own() -> None:
    live, baseline = split_live_and_baseline(summarize(_recorded("c5")))
    assert live is not None and baseline is not None

    def check(**changes: object) -> list[str]:
        return _failed(check_test_card(replace(live, **changes), baseline))  # type: ignore[arg-type]

    stop = live.fail_stops[0]
    assert check(mission_completed=False) == ["P1"]
    assert check(completed_task_count=7) == ["P1"]
    assert check(fail_stops=(replace(stop, confirmed=False),)) == ["P2"]
    assert check(fail_stops=(replace(stop, agent_id=3),)) == ["P2"]
    assert check(fail_stops=(stop, stop)) == ["P2", "P3"]
    assert check(fail_stops=()) == ["P2", "P3"]
    assert check(fail_stops=(replace(stop, residual_speed_mps=2.0),)) == ["P3"]
    assert check(fail_stops=(replace(stop, residual_speed_mps=None),)) == ["P3"]
    assert check(recovered=False) == ["P4"]
    assert check(concurrent_ownership_task_s=0.25) == ["P5"]
    assert check(steps_below_separation=3) == ["P6"]
    assert check(min_separation_m=None) == ["P6"]
    slow = replace(live.vehicles[0], max_drift_s=1.5)
    assert check(vehicles=(slow, *live.vehicles[1:])) == ["P7"]
    assert check(vehicles=()) == ["P7"]
    assert check(failed_agent_id=None, fail_stops=()) == ["P2", "P3", "P4"]
    assert _failed(check_test_card(live, None)) == ["P8"]
    assert _failed(
        check_test_card(live, replace(baseline, mission_completed=False))
    ) == ["P8"]
    assert format_checklist("x", check_test_card(live, None)).startswith(
        "x: LT3 test card FAIL"
    )


def test_a_run_whose_failure_orphaned_nothing_still_recovers() -> None:
    live, baseline = split_live_and_baseline(summarize(_recorded("c5")))
    assert live is not None
    results = check_test_card(
        replace(live, orphaned_task_ids=(), recovered=False), baseline
    )
    assert _failed(results) == []


def test_the_report_summarises_each_file_on_its_own_and_checks_them(
    tmp_path: Path, capsys
) -> None:
    """Two mission logs each hold a 'sitl' and a 'point-mass' run."""

    paths = [str(RECORDED / "c5.jsonl"), str(RECORDED / "mix5.jsonl")]
    assert main(paths) == 0
    printed = capsys.readouterr().out
    assert f"## {paths[0]}" in printed and f"## {paths[1]}" in printed
    assert "LT3 verdict" not in printed

    assert main(["--check", *paths]) == 0
    assert "LT3 verdict: PASS (2/2 runs passed)" in capsys.readouterr().out

    # A log with a live run that breaks the card fails the verdict.
    records = _recorded("c5")
    for record in records:
        if record["type"] == "result" and record["run"] == "sitl":
            record["concurrent_ownership_task_s"] = 1.0
    bad = tmp_path / "bad.jsonl"
    with bad.open("w", encoding="utf-8") as stream:
        write_jsonl(records, stream)
    assert main(["--check", paths[0], str(bad)]) == 1
    printed = capsys.readouterr().out
    assert "[ ] P5 no concurrent ownership: 1.00 task-s" in printed
    assert "LT3 verdict: FAIL (1/2 runs passed)" in printed

    # A deterministic-only log has nothing to check: that is a failure too.
    _result, point_mass = run_logged(_config(), run_label="point-mass")
    alone = tmp_path / "pm.jsonl"
    with alone.open("w", encoding="utf-8") as stream:
        write_jsonl(point_mass, stream)
    assert main(["--check", str(alone)]) == 1
    assert "no live run in this log" in capsys.readouterr().out
