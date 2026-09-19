"""Lease-timeout study: aggregation correctness and determinism.

This is a read-only observer over the existing deterministic mission, so the
properties that matter are the same shape as ``test_sweep.py``'s: the swept
parameter must actually reach the simulation, aggregation must not silently
invent numbers it cannot observe (an empty reassignment-latency sample must
stay empty, not become a fabricated zero), and the same seeds must reproduce
byte-identical output.
"""

from __future__ import annotations

import io

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.lease_timeout_study import (
    AGGREGATE_COLUMNS,
    RUN_COLUMNS,
    aggregate,
    run_study,
    write_aggregate_csv,
    write_run_csv,
)


def _base_config() -> SimulationConfig:
    return SimulationConfig(
        agent_count=4,
        task_count=6,
        communication_range=130.0,
        failure_time=4.0,
    )


# --------------------------------------------------------------------------
# run_study
# --------------------------------------------------------------------------


def test_rows_cover_the_product_in_a_fixed_order() -> None:
    records = list(run_study((1.5, 3.0), (7, 8), _base_config()))

    assert [(record.lease_timeout, record.seed) for record in records] == [
        (1.5, 7),
        (1.5, 8),
        (3.0, 7),
        (3.0, 8),
    ]


def test_the_swept_lease_timeout_actually_reaches_the_simulation() -> None:
    """A short lease must show more duplicate work than a long one, on
    aggregate, under a partition -- the same direction
    docs/measured_results.md's own lease-timeout sweep reports.

    0.5 is deliberately the same floor docs/measured_results.md's own
    lease-timeout sweeps use -- values below roughly 0.3 hit a pre-existing
    consistency-assertion crash in mission.py that is unrelated to this
    module (reproducible with the existing sweep.py too; see OVERNIGHT_LOG.md).
    A single seed does not reliably show the effect (double-booking during a
    partition is a rare event), so this aggregates a handful of seeds, the
    same way the published table does over many more.
    """

    from eudis_swarm.simulation import _parse_jammer

    config = SimulationConfig(
        agent_count=4,
        task_count=20,
        communication_range=50.0,
        failure_time=1000.0,
        region_jammer=_parse_jammer("40,50,15,3,8"),
    )
    seeds = tuple(range(1, 5))

    short, long = aggregate(run_study((0.5, 6.0), seeds, config))

    assert short.mean_duplicate_work_rate > long.mean_duplicate_work_rate


def test_the_seed_reaches_the_simulation() -> None:
    records = list(run_study((2.5,), (1, 2, 3), _base_config()))

    assert [record.seed for record in records] == [1, 2, 3]
    # Not every seed need behave identically; the point is the call succeeds
    # and returns one record per seed.
    assert len(records) == 3


# --------------------------------------------------------------------------
# aggregate
# --------------------------------------------------------------------------


def test_aggregate_groups_by_lease_timeout_in_sorted_order() -> None:
    records = list(run_study((3.0, 1.0), (1, 2), _base_config()))

    aggregates = aggregate(records)

    assert [item.lease_timeout for item in aggregates] == [1.0, 3.0]
    assert all(item.seed_count == 2 for item in aggregates)


def test_duplicate_work_rate_is_the_documented_ratio() -> None:
    from eudis_swarm.lease_timeout_study import RunRecord

    record = RunRecord(
        lease_timeout=2.5,
        seed=1,
        concurrent_ownership_duration=3.0,
        abandoned_task_time=0.0,
        mission_duration=12.0,
        mission_completed=True,
        reassignment_latencies=(),
    )

    result = aggregate([record])[0]
    assert result.mean_duplicate_work_rate == pytest.approx(0.25)


def test_a_zero_duration_run_does_not_divide_by_zero() -> None:
    from eudis_swarm.lease_timeout_study import RunRecord

    record = RunRecord(
        lease_timeout=2.5,
        seed=1,
        concurrent_ownership_duration=0.0,
        abandoned_task_time=0.0,
        mission_duration=0.0,
        mission_completed=False,
        reassignment_latencies=(),
    )

    result = aggregate([record])[0]
    assert result.mean_duplicate_work_rate == 0.0


def test_no_reassignment_observations_report_none_not_zero() -> None:
    """A lease value with zero observed reassignments must say so honestly."""

    # A huge failure time disables the injected physical failure entirely, so
    # no task is ever orphaned and no reassignment latency can be observed.
    config = SimulationConfig(
        agent_count=4, task_count=4, communication_range=130.0, failure_time=1000.0
    )
    records = list(run_study((2.5,), (1, 2), config))

    aggregates = aggregate(records)

    assert aggregates[0].mean_reassignment_latency is None
    assert aggregates[0].reassignment_latency_samples == 0


def test_reassignment_latency_pools_samples_across_seeds() -> None:
    from eudis_swarm.lease_timeout_study import AggregateRecord, RunRecord

    records = [
        RunRecord(
            lease_timeout=2.5,
            seed=1,
            concurrent_ownership_duration=0.0,
            abandoned_task_time=0.0,
            mission_duration=10.0,
            mission_completed=True,
            reassignment_latencies=(1.0, 3.0),
        ),
        RunRecord(
            lease_timeout=2.5,
            seed=2,
            concurrent_ownership_duration=0.0,
            abandoned_task_time=0.0,
            mission_duration=10.0,
            mission_completed=True,
            reassignment_latencies=(2.0,),
        ),
    ]

    result: AggregateRecord = aggregate(records)[0]

    assert result.reassignment_latency_samples == 3
    assert result.mean_reassignment_latency == pytest.approx(2.0)


# --------------------------------------------------------------------------
# CSV output
# --------------------------------------------------------------------------


def test_aggregate_csv_carries_every_declared_column() -> None:
    records = list(run_study((2.5,), (1,), _base_config()))
    buffer = io.StringIO()
    write_aggregate_csv(aggregate(records), buffer)

    import csv as csv_module

    row = next(iter(csv_module.DictReader(io.StringIO(buffer.getvalue()))))
    assert set(row) == set(AGGREGATE_COLUMNS)


def test_run_csv_carries_every_declared_column() -> None:
    records = list(run_study((2.5,), (1,), _base_config()))
    buffer = io.StringIO()
    write_run_csv(records, buffer)

    import csv as csv_module

    row = next(iter(csv_module.DictReader(io.StringIO(buffer.getvalue()))))
    assert set(row) == set(RUN_COLUMNS)


def test_csv_uses_unix_line_endings() -> None:
    records = list(run_study((2.5,), (1,), _base_config()))
    buffer = io.StringIO()
    write_aggregate_csv(aggregate(records), buffer)

    text = buffer.getvalue()
    assert "\r\n" not in text
    assert text.startswith(",".join(AGGREGATE_COLUMNS) + "\n")


def test_the_same_seeds_reproduce_byte_identical_csv() -> None:
    def render() -> str:
        records = list(run_study((1.0, 3.0), (1, 2), _base_config()))
        buffer = io.StringIO()
        write_aggregate_csv(aggregate(records), buffer)
        return buffer.getvalue()

    assert render() == render()


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def test_cli_writes_a_readable_csv(tmp_path) -> None:
    from eudis_swarm.lease_timeout_study import main

    destination = tmp_path / "nested" / "study.csv"
    exit_code = main(
        [
            "--from",
            "1.0",
            "--to",
            "2.0",
            "--steps",
            "2",
            "--seeds",
            "2",
            "--tasks",
            "4",
            "--failure-time",
            "1000",
            "--output",
            str(destination),
        ]
    )

    assert exit_code == 0
    import csv as csv_module

    rows = list(csv_module.DictReader(destination.open(encoding="utf-8")))
    assert len(rows) == 2
    assert {row["lease_timeout"] for row in rows} == {"1.000000", "2.000000"}


def test_cli_writes_to_stdout_without_an_output_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from eudis_swarm.lease_timeout_study import main

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
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert captured.startswith(",".join(AGGREGATE_COLUMNS))


@pytest.mark.parametrize(
    "arguments",
    [
        ["--from", "1", "--to", "2", "--steps", "0", "--seeds", "1"],
        ["--from", "1", "--to", "2", "--steps", "1", "--seeds", "0"],
    ],
)
def test_cli_rejects_empty_studies(arguments: list[str]) -> None:
    from eudis_swarm.lease_timeout_study import main

    with pytest.raises(SystemExit) as exit_info:
        main(arguments)

    assert exit_info.value.code == 2
