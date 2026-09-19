"""Lease-timeout resilience study: aggregate duplicate work vs. abandoned time.

This is a read-only observer on top of the existing deterministic mission,
exactly like ``sweep.py``: every row comes from reading ``SimulationMetrics``
after a run has already finished, so nothing here can influence a UAV's own
decision. It differs from ``sweep.run_sweep`` in two ways -- it also reads
``SimulationMetrics.task_reassignment_latencies`` (a property ``sweep.py``'s
CSV schema does not surface), and it aggregates across seeds into one row per
lease-timeout value instead of emitting one row per ``(value, seed)``.

"Duplicate-work rate" is a quantity this module defines and is not otherwise
named in the codebase: ``concurrent_ownership_duration / mission_duration``,
i.e. the fraction of a mission's own duration spent with more than one UAV
believing it owns the same task. The other evaluator counters
(``concurrent_ownership_duration``, ``abandoned_task_time``) remain the
project's existing absolute task-second quantities.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import fmean
from typing import Iterable, Iterator, Sequence

from .config import SimulationConfig
from .simulation import Simulation
from .sweep import parameter_values

_PRECISION = 6

RUN_COLUMNS: tuple[str, ...] = (
    "lease_timeout",
    "seed",
    "concurrent_ownership_duration",
    "abandoned_task_time",
    "mission_duration",
    "mission_completed",
    "reassignment_latency_mean",
    "reassignment_latency_samples",
)

AGGREGATE_COLUMNS: tuple[str, ...] = (
    "lease_timeout",
    "seeds",
    "mean_duplicate_work_rate",
    "mean_abandoned_task_time",
    "mean_mission_duration",
    "mission_completed_fraction",
    "mean_reassignment_latency",
    "reassignment_latency_samples",
)


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One (lease_timeout, seed) observation, read from a finished mission."""

    lease_timeout: float
    seed: int
    concurrent_ownership_duration: float
    abandoned_task_time: float
    mission_duration: float
    mission_completed: bool
    reassignment_latencies: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class AggregateRecord:
    """One lease-timeout value, aggregated across every seed run at it."""

    lease_timeout: float
    seed_count: int
    mean_duplicate_work_rate: float
    mean_abandoned_task_time: float
    mean_mission_duration: float
    mission_completed_fraction: float
    mean_reassignment_latency: float | None
    reassignment_latency_samples: int


def _run(lease_timeout: float, seed: int, base_config: SimulationConfig) -> RunRecord:
    config = replace(base_config, random_seed=seed, claim_lease_duration=lease_timeout)
    metrics = Simulation(config).run().metrics
    return RunRecord(
        lease_timeout=lease_timeout,
        seed=seed,
        concurrent_ownership_duration=metrics.concurrent_ownership_duration,
        abandoned_task_time=metrics.abandoned_task_time,
        mission_duration=metrics.simulation_duration,
        mission_completed=metrics.mission_completed,
        reassignment_latencies=tuple(metrics.task_reassignment_latencies.values()),
    )


def run_study(
    lease_timeouts: Sequence[float],
    seeds: Sequence[int],
    base_config: SimulationConfig,
) -> Iterator[RunRecord]:
    """Yield one ``RunRecord`` per (lease_timeout, seed) in a fixed order."""

    for lease_timeout in lease_timeouts:
        for seed in seeds:
            yield _run(lease_timeout, seed, base_config)


def _duplicate_work_rate(record: RunRecord) -> float:
    if record.mission_duration <= 0:
        return 0.0
    return record.concurrent_ownership_duration / record.mission_duration


def aggregate(records: Iterable[RunRecord]) -> list[AggregateRecord]:
    """Aggregate run records into one row per lease-timeout value.

    Reassignment latency is pooled across every seed at that value rather than
    averaged as a mean-of-means, so a lease value with more reassignment
    events weighs proportionally more. A value with zero observed
    reassignments reports ``None`` (0 samples) rather than an invented zero.
    """

    by_lease: dict[float, list[RunRecord]] = {}
    for record in records:
        by_lease.setdefault(record.lease_timeout, []).append(record)

    aggregates: list[AggregateRecord] = []
    for lease_timeout in sorted(by_lease):
        group = by_lease[lease_timeout]
        pooled_latencies = [
            latency for record in group for latency in record.reassignment_latencies
        ]
        aggregates.append(
            AggregateRecord(
                lease_timeout=lease_timeout,
                seed_count=len(group),
                mean_duplicate_work_rate=fmean(
                    _duplicate_work_rate(record) for record in group
                ),
                mean_abandoned_task_time=fmean(
                    record.abandoned_task_time for record in group
                ),
                mean_mission_duration=fmean(
                    record.mission_duration for record in group
                ),
                mission_completed_fraction=(
                    sum(record.mission_completed for record in group) / len(group)
                ),
                mean_reassignment_latency=(
                    fmean(pooled_latencies) if pooled_latencies else None
                ),
                reassignment_latency_samples=len(pooled_latencies),
            )
        )
    return aggregates


def _number(value: float) -> str:
    return f"{value:.{_PRECISION}f}"


def write_aggregate_csv(
    records: Iterable[AggregateRecord], stream: io.TextIOBase
) -> int:
    writer = csv.DictWriter(
        stream, fieldnames=list(AGGREGATE_COLUMNS), lineterminator="\n"
    )
    writer.writeheader()
    count = 0
    for record in records:
        writer.writerow(
            {
                "lease_timeout": _number(record.lease_timeout),
                "seeds": str(record.seed_count),
                "mean_duplicate_work_rate": _number(record.mean_duplicate_work_rate),
                "mean_abandoned_task_time": _number(record.mean_abandoned_task_time),
                "mean_mission_duration": _number(record.mean_mission_duration),
                "mission_completed_fraction": _number(
                    record.mission_completed_fraction
                ),
                "mean_reassignment_latency": (
                    ""
                    if record.mean_reassignment_latency is None
                    else _number(record.mean_reassignment_latency)
                ),
                "reassignment_latency_samples": str(
                    record.reassignment_latency_samples
                ),
            }
        )
        count += 1
    return count


def write_run_csv(records: Iterable[RunRecord], stream: io.TextIOBase) -> int:
    """Write the raw, un-aggregated (lease_timeout, seed) rows behind a study."""

    writer = csv.DictWriter(stream, fieldnames=list(RUN_COLUMNS), lineterminator="\n")
    writer.writeheader()
    count = 0
    for record in records:
        latencies = record.reassignment_latencies
        writer.writerow(
            {
                "lease_timeout": _number(record.lease_timeout),
                "seed": str(record.seed),
                "concurrent_ownership_duration": _number(
                    record.concurrent_ownership_duration
                ),
                "abandoned_task_time": _number(record.abandoned_task_time),
                "mission_duration": _number(record.mission_duration),
                "mission_completed": "1" if record.mission_completed else "0",
                "reassignment_latency_mean": (
                    "" if not latencies else _number(fmean(latencies))
                ),
                "reassignment_latency_samples": str(len(latencies)),
            }
        )
        count += 1
    return count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eudis-swarm-lease-study",
        description=(
            "Aggregate duplicate-work rate, abandoned-task time, mission "
            "duration, and reassignment latency across a lease-timeout range."
        ),
    )
    parser.add_argument("--from", dest="start", type=float, default=0.5)
    parser.add_argument("--to", dest="stop", type=float, default=6.0)
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument(
        "--seeds", type=int, default=20, help="number of consecutive seeds per value"
    )
    parser.add_argument(
        "--seed-base", type=int, default=1, help="first seed in the range"
    )
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument("--communication-range", type=float, default=130.0)
    parser.add_argument(
        "--failure-time",
        type=float,
        default=4.0,
        help="use a large value to disable the injected physical failure",
    )
    parser.add_argument(
        "--task-control",
        choices=("distributed", "centralized"),
        default="distributed",
        help="held constant across the study",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="write CSV here instead of stdout"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.steps < 1 or arguments.seeds < 1:
        _parser().error("--steps and --seeds must be at least 1")

    lease_timeouts = parameter_values(arguments.start, arguments.stop, arguments.steps)
    seeds = tuple(range(arguments.seed_base, arguments.seed_base + arguments.seeds))
    base_config = SimulationConfig(
        agent_count=arguments.agents,
        task_count=arguments.tasks,
        communication_range=arguments.communication_range,
        failure_time=arguments.failure_time,
        task_control=arguments.task_control,
    )

    aggregates = aggregate(run_study(lease_timeouts, seeds, base_config))
    if arguments.output is None:
        write_aggregate_csv(aggregates, sys.stdout)  # type: ignore[arg-type]
        return 0
    buffer = io.StringIO()
    write_aggregate_csv(aggregates, buffer)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
