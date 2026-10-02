"""The paired distance / connectivity / quantum study."""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from eudis_swarm.quantum_study import (
    POLICIES,
    RUN_COLUMNS,
    SOLVE_COLUMNS,
    TIMING_COLUMNS,
    main,
    run_case,
    study_cases,
    summarize,
    time_solvers,
)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_cases_are_paired_across_policies() -> None:
    cases = list(study_cases((1, 2), fleets=(4,), topologies=("sparse_jammed",)))
    assert len(cases) == 2 * len(POLICIES)
    first_seed = [case for case in cases if case.seed == 1]
    configs = [case.config for case in first_seed]
    assert [case.policy for case in first_seed] == [label for label, *_ in POLICIES]
    # Everything but the policy fields is identical within a pair.
    assert (
        len({(c.random_seed, c.region_jammer, c.communication_range) for c in configs})
        == 1
    )
    assert configs[0].region_jammer is not None
    assert {c.allocation_policy for c in configs} == {
        "distance",
        "connectivity",
        "quantum",
    }


def test_run_case_evaluates_each_solve_against_brute_force() -> None:
    case = next(
        case
        for case in study_cases((1,), fleets=(4,), topologies=("connected",))
        if case.policy == "quantum_sqa"
    )
    outcome = run_case(case)
    assert outcome.run_row["mission_completed"] == "1"
    assert int(outcome.run_row["quantum_solves"]) == len(outcome.solve_rows) > 0
    for row in outcome.solve_rows:
        assert row["feasible"] == "1"
        assert float(row["optimality_gap"]) >= 0.0
        assert int(row["qubits"]) == int(row["formulation_agents"]) * int(
            row["formulation_objectives"]
        )
    assert outcome.run_row["quantum_suboptimal_solves"] == str(
        sum(float(row["optimality_gap"]) > 1e-9 for row in outcome.solve_rows)
    )
    timing = time_solvers([outcome], solvers=("brute_force",))
    assert {row["solver"] for row in timing} == {"brute_force"}
    assert sum(int(row["solves"]) for row in timing) == len(outcome.solve_rows)


def test_main_writes_deterministic_csv(tmp_path: Path) -> None:
    arguments = [
        "--seeds",
        "1",
        "--seed-base",
        "3",
        "--fleets",
        "4",
        "--topologies",
        "sparse_jammed",
    ]
    outputs = []
    for run in ("a", "b"):
        runs = tmp_path / run / "runs.csv"
        solves = tmp_path / run / "solves.csv"
        summary = tmp_path / run / "summary.csv"
        assert (
            main(
                [
                    *arguments,
                    "--output",
                    str(runs),
                    "--solves-output",
                    str(solves),
                    "--summary-output",
                    str(summary),
                ]
            )
            == 0
        )
        outputs.append((runs.read_bytes(), solves.read_bytes(), summary.read_bytes()))
    assert outputs[0] == outputs[1]
    rows = _read(tmp_path / "a" / "runs.csv")
    assert list(rows[0]) == list(RUN_COLUMNS)
    assert [row["policy"] for row in rows] == [label for label, *_ in POLICIES]
    assert all(row["topology"] == "sparse_jammed" for row in rows)
    solve_rows = _read(tmp_path / "a" / "solves.csv")
    if solve_rows:
        assert list(solve_rows[0]) == list(SOLVE_COLUMNS)
    summary_rows = _read(tmp_path / "a" / "summary.csv")
    distance = next(row for row in summary_rows if row["policy"] == "distance")
    assert distance["faster_than_distance"] == "0"
    assert distance["slower_than_distance"] == "0"


def test_main_writes_timing_and_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    timing = tmp_path / "timing.csv"
    assert (
        main(
            [
                "--seeds",
                "1",
                "--fleets",
                "4",
                "--topologies",
                "connected",
                "--timing-output",
                str(timing),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == ",".join(RUN_COLUMNS)
    assert len(printed) == 1 + len(POLICIES)
    rows = _read(timing)
    assert list(rows[0]) == list(TIMING_COLUMNS)
    assert {row["solver"] for row in rows} == {"brute_force", "qaoa", "sqa"}


def test_main_rejects_bad_seed_arguments() -> None:
    with pytest.raises(SystemExit):
        main(["--seeds", "0"])


def test_summary_counts_paired_differences() -> None:
    def row(policy: str, seed: str, duration: float) -> dict[str, str]:
        return {
            "agents": "4",
            "topology": "connected",
            "seed": seed,
            "policy": policy,
            "mission_completed": "1",
            "mission_duration": str(duration),
            "flight_distance": "1.0",
            "concurrent_ownership_duration": "0",
            "contested_duration": "0",
            "claims_refused": "0",
            "contested_yields": "0",
            "quantum_solves": "0",
            "quantum_changed_choices": "0",
            "quantum_suboptimal_solves": "0",
        }

    summary = summarize(
        [
            row("distance", "1", 10.0),
            row("distance", "2", 10.0),
            row("quantum_qaoa", "1", 9.0),
            row("quantum_qaoa", "2", 11.0),
        ]
    )
    quantum = next(item for item in summary if item["policy"] == "quantum_qaoa")
    assert quantum["faster_than_distance"] == "1"
    assert quantum["slower_than_distance"] == "1"
    assert math.isclose(float(quantum["mean_duration"]), 10.0)
