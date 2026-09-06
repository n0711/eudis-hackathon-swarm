"""The parameter sweep entry point.

A sweep is only useful if it is reproducible, so the tests that matter here are
about determinism and about the swept parameter genuinely reaching the
simulation -- a sweep that silently ignores its own parameter produces a
perfectly formatted table of identical rows.
"""

from __future__ import annotations

import csv
import io
from dataclasses import replace

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.sweep import (
    CSV_COLUMNS,
    SWEEPABLE_PARAMETERS,
    main,
    parameter_values,
    run_sweep,
    write_csv,
)


def _base_config() -> SimulationConfig:
    return SimulationConfig(
        agent_count=4,
        task_count=3,
        communication_range=50.0,
        failure_time=100.0,
    )


# --------------------------------------------------------------------------
# parameter values
# --------------------------------------------------------------------------


def test_endpoints_are_exact_and_the_sequence_does_not_drift() -> None:
    """Built from integer fractions, not repeated addition."""

    values = parameter_values(0.5, 6.0, 12)

    assert len(values) == 12
    assert values[0] == 0.5
    assert values[-1] == 6.0
    assert values == tuple(sorted(values))


def test_a_single_step_returns_the_start() -> None:
    assert parameter_values(2.5, 9.0, 1) == (2.5,)


@pytest.mark.parametrize("steps", [0, -1])
def test_steps_must_be_positive(steps: int) -> None:
    with pytest.raises(ValueError):
        parameter_values(0.5, 6.0, steps)


# --------------------------------------------------------------------------
# row generation
# --------------------------------------------------------------------------


def test_unknown_parameters_are_rejected_by_name() -> None:
    with pytest.raises(ValueError, match="unknown sweep parameter"):
        list(run_sweep("not-a-parameter", (1.0,), (1,), _base_config()))


def test_every_advertised_parameter_maps_to_a_real_config_field() -> None:
    fields = set(SimulationConfig.__dataclass_fields__)

    assert set(SWEEPABLE_PARAMETERS.values()) <= fields


def test_rows_cover_the_product_in_a_fixed_order() -> None:
    rows = list(run_sweep("lease-timeout", (1.0, 2.0), (7, 8), _base_config()))

    assert [(row["param_value"], row["seed"]) for row in rows] == [
        ("1.000000", "7"),
        ("1.000000", "8"),
        ("2.000000", "7"),
        ("2.000000", "8"),
    ]
    assert all(row["param"] == "lease-timeout" for row in rows)


def test_the_swept_parameter_actually_reaches_the_simulation() -> None:
    """The failure this guards against produced a table of identical rows."""

    rows = list(run_sweep("lease-timeout", (0.5, 6.0), (1,), _base_config()))

    assert [row["claim_lease_duration"] for row in rows] == ["0.500000", "6.000000"]


def test_the_seed_reaches_the_simulation() -> None:
    rows = list(run_sweep("lease-timeout", (2.5,), (1, 2, 3), _base_config()))

    assert [row["seed"] for row in rows] == ["1", "2", "3"]


def test_rows_carry_every_declared_column() -> None:
    row = next(iter(run_sweep("lease-timeout", (2.5,), (1,), _base_config())))

    assert set(row) == set(CSV_COLUMNS)


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def test_the_same_seeds_reproduce_byte_identical_csv() -> None:
    def render() -> str:
        buffer = io.StringIO()
        write_csv(
            run_sweep("lease-timeout", (1.0, 3.0), (1, 2), _base_config()), buffer
        )
        return buffer.getvalue()

    assert render() == render()


def test_csv_uses_unix_line_endings_so_platforms_agree() -> None:
    buffer = io.StringIO()
    write_csv(run_sweep("lease-timeout", (1.0,), (1,), _base_config()), buffer)
    text = buffer.getvalue()

    assert "\r\n" not in text
    assert text.startswith(",".join(CSV_COLUMNS) + "\n")


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def test_cli_writes_a_readable_csv(tmp_path) -> None:
    destination = tmp_path / "nested" / "sweep.csv"

    exit_code = main(
        [
            "--param",
            "lease-timeout",
            "--from",
            "1.0",
            "--to",
            "2.0",
            "--steps",
            "2",
            "--seeds",
            "2",
            "--tasks",
            "3",
            "--failure-time",
            "100",
            "--output",
            str(destination),
        ]
    )

    assert exit_code == 0
    rows = list(csv.DictReader(destination.open(encoding="utf-8")))
    assert len(rows) == 4
    assert {row["claim_lease_duration"] for row in rows} == {"1.000000", "2.000000"}


def test_cli_writes_to_stdout_without_an_output_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--from",
            "2.5",
            "--to",
            "2.5",
            "--steps",
            "1",
            "--seeds",
            "1",
            "--tasks",
            "3",
            "--failure-time",
            "100",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert captured.startswith(",".join(CSV_COLUMNS))
    assert len(list(csv.DictReader(io.StringIO(captured)))) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        ["--from", "1", "--to", "2", "--steps", "0", "--seeds", "1"],
        ["--from", "1", "--to", "2", "--steps", "1", "--seeds", "0"],
    ],
)
def test_cli_rejects_empty_sweeps(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(arguments)

    assert exit_info.value.code == 2


# --------------------------------------------------------------------------
# held-constant dimensions
# --------------------------------------------------------------------------


def test_held_constants_are_recorded_on_every_row() -> None:
    """Two sweeps must be concatenable without tracking which file is which."""

    rows = list(run_sweep("lease-timeout", (2.5,), (1,), _base_config()))

    assert rows[0]["task_control"] == "distributed"
    assert rows[0]["degradation_policy"] == "none"


def test_the_control_mode_is_carried_into_the_simulation() -> None:
    exit_code = main(
        [
            "--from",
            "2.5",
            "--to",
            "2.5",
            "--steps",
            "1",
            "--seeds",
            "1",
            "--tasks",
            "3",
            "--failure-time",
            "100",
            "--task-control",
            "centralized",
        ]
    )
    assert exit_code == 0


def test_a_degradation_policy_reaches_the_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--from",
            "2.5",
            "--to",
            "2.5",
            "--steps",
            "1",
            "--seeds",
            "1",
            "--tasks",
            "3",
            "--failure-time",
            "100",
            "--degradation-policy",
            "HOLD",
        ]
    )

    assert exit_code == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert [row["degradation_policy"] for row in rows] == ["HOLD"]


def test_a_jammer_can_be_held_constant(capsys: pytest.CaptureFixture[str]) -> None:
    """Without degradation there is nothing for a policy sweep to measure."""

    exit_code = main(
        [
            "--from",
            "2.5",
            "--to",
            "2.5",
            "--steps",
            "1",
            "--seeds",
            "1",
            "--tasks",
            "4",
            "--failure-time",
            "1000",
            "--communication-range",
            "50",
            "--jammer",
            "40,50,15,3,8",
        ]
    )

    assert exit_code == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 1
    assert rows[0]["communication_range"] == "50.000000"


def test_a_malformed_jammer_is_rejected_cleanly() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--from",
                "1",
                "--to",
                "1",
                "--steps",
                "1",
                "--seeds",
                "1",
                "--jammer",
                "not,a,jammer",
            ]
        )

    assert exit_info.value.code == 2


def test_control_modes_remain_separable_in_one_concatenated_table() -> None:
    """The point of recording held constants: compare without bookkeeping."""

    rows = []
    for mode in ("distributed", "centralized"):
        config = replace(_base_config(), task_control=mode)
        rows.extend(run_sweep("lease-timeout", (2.5,), (1, 2), config))

    by_mode = {row["task_control"] for row in rows}
    assert by_mode == {"distributed", "centralized"}
    assert len(rows) == 4
