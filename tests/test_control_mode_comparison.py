"""Same-seed control-mode comparison: determinism and real divergence.

The point of this runner is to prove the two control modes are being compared
on genuinely identical conditions (same seed, same everything else) and that
the reported delta is not a fixed placeholder -- it must reflect an actual
difference the sweep-derived tables in docs/measured_results.md already
document (centralized strands work under a partition; distributed does not).
"""

from __future__ import annotations

import io

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.control_mode_comparison import (
    CSV_COLUMNS,
    compare_seed,
    main,
    run_comparison,
    write_csv,
)


def _partitioned_config() -> SimulationConfig:
    """A config where the two control modes are known to diverge.

    Mirrors the setup behind docs/measured_results.md section 2: a jammer
    partitions the swarm for a window, the physical failure is disabled
    (failure_time huge) so the divergence is purely about how ownership is
    held under a partition, not about failure detection.
    """

    return SimulationConfig(
        agent_count=4,
        task_count=6,
        communication_range=50.0,
        failure_time=1000.0,
        claim_lease_duration=2.5,
    )


def test_rows_cover_every_seed_in_order() -> None:
    rows = list(run_comparison((5, 6, 7), _partitioned_config()))

    assert [row["seed"] for row in rows] == ["5", "6", "7"]


def test_every_row_carries_every_declared_column() -> None:
    row = compare_seed(1, _partitioned_config())

    assert set(row) == set(CSV_COLUMNS)


def test_the_two_modes_see_the_same_seed() -> None:
    """A seed only affects task placement, so both modes must place tasks
    identically -- the divergence in outcomes must come from the control
    model, not from secretly running different scenarios."""

    from dataclasses import replace

    from eudis_swarm.simulation import Simulation

    config = _partitioned_config()
    distributed_tasks = (
        Simulation(replace(config, random_seed=3, task_control="distributed"))
        .run()
        .mission.tasks
    )
    centralized_tasks = (
        Simulation(replace(config, random_seed=3, task_control="centralized"))
        .run()
        .mission.tasks
    )

    distributed_positions = {
        task_id: task.position for task_id, task in distributed_tasks.items()
    }
    centralized_positions = {
        task_id: task.position for task_id, task in centralized_tasks.items()
    }
    assert distributed_positions == centralized_positions


def test_a_known_divergent_scenario_shows_a_nonzero_delta() -> None:
    """Centralized control strands work under a partition; distributed does
    not -- dropped-ownership time must diverge, not read as a placeholder."""

    row = compare_seed(1, _partitioned_config())

    delta = float(row["delta_dropped_ownership_time"])
    assert delta != 0.0


def test_csv_carries_every_declared_column_and_unix_line_endings() -> None:
    buffer = io.StringIO()
    write_csv(iter([compare_seed(1, _partitioned_config())]), buffer)

    text = buffer.getvalue()
    assert "\r\n" not in text
    assert text.startswith(",".join(CSV_COLUMNS) + "\n")


def test_the_same_seed_reproduces_byte_identical_rows() -> None:
    def render() -> str:
        buffer = io.StringIO()
        write_csv(run_comparison((1, 2), _partitioned_config()), buffer)
        return buffer.getvalue()

    assert render() == render()


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def test_cli_writes_a_readable_csv(tmp_path) -> None:
    destination = tmp_path / "nested" / "compare.csv"

    exit_code = main(
        [
            "--seeds",
            "2",
            "--tasks",
            "4",
            "--communication-range",
            "50",
            "--failure-time",
            "1000",
            "--jammer",
            "40,50,15,3,8",
            "--output",
            str(destination),
        ]
    )

    assert exit_code == 0
    import csv as csv_module

    rows = list(csv_module.DictReader(destination.open(encoding="utf-8")))
    assert len(rows) == 2
    assert {row["seed"] for row in rows} == {"1", "2"}


def test_cli_writes_to_stdout_without_an_output_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--seeds",
            "1",
            "--tasks",
            "4",
            "--failure-time",
            "1000",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert captured.startswith(",".join(CSV_COLUMNS))


def test_cli_rejects_zero_seeds() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--seeds", "0"])

    assert exit_info.value.code == 2


def test_a_malformed_jammer_is_rejected_cleanly() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--seeds", "1", "--jammer", "not,a,jammer"])

    assert exit_info.value.code == 2
