"""Parameter sweep over the evaluator-side coordination metrics.

One row per (parameter value, seed). Everything here is observation: the sweep
runs whole missions and reads the metrics afterwards, so it cannot influence a
decision any UAV makes.

Determinism is a hard requirement -- the same arguments must produce
byte-identical CSV -- so parameter values are generated from exact integer
steps, rows are emitted in sorted order, and every float is formatted to a
fixed precision rather than repr'd.
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
from .simulation import Simulation

SWEEPABLE_PARAMETERS: dict[str, str] = {
    "lease-timeout": "claim_lease_duration",
    "communication-range": "communication_range",
    "failure-timeout": "failure_timeout",
    "peer-state-stale-after": "peer_state_stale_after",
}

CSV_COLUMNS: tuple[str, ...] = (
    "param",
    "param_value",
    "seed",
    "communication_range",
    "claim_lease_duration",
    "tasks_completed",
    "total_tasks",
    "mission_completed",
    "mission_duration",
    "concurrent_ownership_duration",
    "abandoned_task_time",
    "dropped_ownership_time",
    "contested_duration",
    "redundant_completions",
    "physically_lost",
    "declared_failed",
    "undetected_failures",
)

_PRECISION = 6


def parameter_values(start: float, stop: float, steps: int) -> tuple[float, ...]:
    """Return ``steps`` inclusive values from ``start`` to ``stop``.

    The fraction is built from integers so the sequence does not drift with
    repeated addition, which would make the CSV non-reproducible.
    """

    if steps < 1:
        raise ValueError("steps must be at least 1")
    if steps == 1:
        return (round(start, _PRECISION),)
    span = stop - start
    return tuple(
        round(start + span * index / (steps - 1), _PRECISION) for index in range(steps)
    )


def _row(
    parameter: str,
    value: float,
    seed: int,
    config: SimulationConfig,
) -> dict[str, str]:
    metrics = Simulation(config).run().metrics
    duration = metrics.simulation_duration

    def number(item: float) -> str:
        return f"{item:.{_PRECISION}f}"

    return {
        "param": parameter,
        "param_value": number(value),
        "seed": str(seed),
        "communication_range": number(config.communication_range),
        "claim_lease_duration": number(config.claim_lease_duration),
        "tasks_completed": str(metrics.completed_task_count),
        "total_tasks": str(metrics.total_task_count),
        "mission_completed": "1" if metrics.mission_completed else "0",
        "mission_duration": number(0.0 if duration is None else duration),
        "concurrent_ownership_duration": number(metrics.concurrent_ownership_duration),
        "abandoned_task_time": number(metrics.abandoned_task_time),
        "dropped_ownership_time": number(metrics.dropped_ownership_time),
        "contested_duration": number(metrics.contested_duration),
        "redundant_completions": str(metrics.redundant_completion_count),
        "physically_lost": str(metrics.physically_failed_count),
        "declared_failed": str(metrics.failed_agent_count),
        "undetected_failures": str(metrics.undetected_failure_count),
    }


def run_sweep(
    parameter: str,
    values: Sequence[float],
    seeds: Sequence[int],
    base_config: SimulationConfig,
) -> Iterator[dict[str, str]]:
    """Yield one row per (value, seed) in a fixed, reproducible order."""

    try:
        field_name = SWEEPABLE_PARAMETERS[parameter]
    except KeyError as error:
        known = ", ".join(sorted(SWEEPABLE_PARAMETERS))
        raise ValueError(f"unknown sweep parameter {parameter!r}; try {known}") from (
            error
        )

    for value in values:
        for seed in seeds:
            config = replace(base_config, random_seed=seed, **{field_name: value})
            yield _row(parameter, value, seed, config)


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
        prog="eudis-swarm-sweep",
        description=(
            "Sweep one configuration parameter and emit evaluator-side "
            "coordination metrics as CSV."
        ),
    )
    parser.add_argument(
        "--param",
        default="lease-timeout",
        choices=sorted(SWEEPABLE_PARAMETERS),
        help="configuration parameter to sweep",
    )
    parser.add_argument("--from", dest="start", type=float, required=True)
    parser.add_argument("--to", dest="stop", type=float, required=True)
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument(
        "--seeds", type=int, default=20, help="number of consecutive seeds per value"
    )
    parser.add_argument(
        "--seed-base", type=int, default=1, help="first seed in the range"
    )
    parser.add_argument(
        "--communication-range",
        type=float,
        default=50.0,
        help="held fixed unless it is the swept parameter",
    )
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument(
        "--failure-time",
        type=float,
        default=4.0,
        help="use a large value to disable the injected physical failure",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="write CSV here instead of stdout"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.steps < 1 or arguments.seeds < 1:
        _parser().error("--steps and --seeds must be at least 1")

    values = parameter_values(arguments.start, arguments.stop, arguments.steps)
    seeds = tuple(range(arguments.seed_base, arguments.seed_base + arguments.seeds))
    base_config = SimulationConfig(
        agent_count=arguments.agents,
        task_count=arguments.tasks,
        communication_range=arguments.communication_range,
        failure_time=arguments.failure_time,
    )

    rows = run_sweep(arguments.param, values, seeds, base_config)
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
