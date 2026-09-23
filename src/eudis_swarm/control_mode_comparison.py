"""Same-seed centralized-vs-distributed comparison on the wasted-work metrics.

For each seed, this runs the identical scenario twice -- once per control
mode -- and reports evaluator-computed wasted-work counters side by side,
plus the centralized-minus-distributed delta. Two of the nine columns here
(``concurrent_ownership_duration`` / "Double-booked" and
``dropped_ownership_time`` / "Stranded work") are the two headline wasted-work
figures the trace already carries per frame (`TraceMetrics`); the rest are the
other evaluator counters ``sweep.py``'s own CSV already tracks. Each run is an
independent ``Simulation``; nothing here lets one mode's replicas see the
other mode's evidence, and every value is read from ``SimulationMetrics``
after a run has already finished, exactly like ``sweep.py``.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Sequence

from .config import SimulationConfig
from .simulation import Simulation, _parse_jammer

_PRECISION = 6

_METRICS: tuple[str, ...] = (
    "concurrent_ownership_duration",
    "abandoned_task_time",
    "dropped_ownership_time",
    "contested_duration",
    "mission_completed",
    "mission_duration",
    "tasks_completed",
    "declared_failed",
    "undetected_failures",
)

CSV_COLUMNS: tuple[str, ...] = (
    "seed",
    *(f"distributed_{name}" for name in _METRICS),
    *(f"centralized_{name}" for name in _METRICS),
    *(f"delta_{name}" for name in _METRICS),
)


def _metric_values(config: SimulationConfig) -> dict[str, float]:
    metrics = Simulation(config).run().metrics
    duration = metrics.simulation_duration
    return {
        "concurrent_ownership_duration": metrics.concurrent_ownership_duration,
        "abandoned_task_time": metrics.abandoned_task_time,
        "dropped_ownership_time": metrics.dropped_ownership_time,
        "contested_duration": metrics.contested_duration,
        "mission_completed": 1.0 if metrics.mission_completed else 0.0,
        "mission_duration": 0.0 if duration is None else duration,
        "tasks_completed": float(metrics.completed_task_count),
        "declared_failed": float(metrics.failed_agent_count),
        "undetected_failures": float(metrics.undetected_failure_count),
    }


def _number(value: float) -> str:
    return f"{value:.{_PRECISION}f}"


def compare_seed(seed: int, base_config: SimulationConfig) -> dict[str, str]:
    """Run one seed under both control modes; return one comparison row."""

    distributed = _metric_values(
        replace(base_config, random_seed=seed, task_control="distributed")
    )
    centralized = _metric_values(
        replace(base_config, random_seed=seed, task_control="centralized")
    )

    row: dict[str, str] = {"seed": str(seed)}
    for name in _METRICS:
        row[f"distributed_{name}"] = _number(distributed[name])
        row[f"centralized_{name}"] = _number(centralized[name])
        row[f"delta_{name}"] = _number(centralized[name] - distributed[name])
    return row


def run_comparison(
    seeds: Sequence[int], base_config: SimulationConfig
) -> Iterator[dict[str, str]]:
    """Yield one comparison row per seed, in order."""

    for seed in seeds:
        yield compare_seed(seed, base_config)


def write_csv(rows: Iterator[dict[str, str]], stream: io.TextIOBase) -> int:
    writer = csv.DictWriter(stream, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    count = 0
    for row in rows:
        writer.writerow(row)
        count += 1
    return count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eudis-swarm-mode-compare",
        description=(
            "Compare distributed vs. centralized control on identical seeds, "
            "on the evaluator's wasted-work metrics."
        ),
    )
    parser.add_argument(
        "--seeds", type=int, default=20, help="number of consecutive seeds"
    )
    parser.add_argument(
        "--seed-base", type=int, default=1, help="first seed in the range"
    )
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument("--communication-range", type=float, default=50.0)
    parser.add_argument(
        "--failure-time",
        type=float,
        default=1000.0,
        help="use a large value to disable the injected physical failure",
    )
    parser.add_argument("--lease-timeout", type=float, default=2.5)
    parser.add_argument(
        "--jammer",
        type=str,
        default=None,
        metavar="X,Y,RADIUS,T_START,T_END",
        help="a circular jamming region, as on the main CLI",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="write CSV here instead of stdout"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.seeds < 1:
        _parser().error("--seeds must be at least 1")

    seeds = tuple(range(arguments.seed_base, arguments.seed_base + arguments.seeds))
    try:
        base_config = SimulationConfig(
            agent_count=arguments.agents,
            task_count=arguments.tasks,
            communication_range=arguments.communication_range,
            failure_time=arguments.failure_time,
            claim_lease_duration=arguments.lease_timeout,
            region_jammer=(
                None if arguments.jammer is None else _parse_jammer(arguments.jammer)
            ),
        )
    except ValueError as error:
        _parser().error(str(error))

    rows = run_comparison(seeds, base_config)
    if arguments.output is None:
        write_csv(rows, sys.stdout)  # type: ignore[arg-type]
        return 0
    buffer = io.StringIO()
    write_csv(rows, buffer)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
