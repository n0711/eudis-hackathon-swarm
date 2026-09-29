"""Paired, deterministic comparison across fleet sizes and link conditions.

This is an evaluator-only experiment. It never changes a UAV's decisions and
does not use SITL observations or the analytical radio model.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from pathlib import Path
from typing import Iterator, Sequence, TextIO

from .communication import RegionJammer
from .config import SimulationConfig
from .simulation import Simulation

CSV_COLUMNS: tuple[str, ...] = (
    "agents",
    "tasks",
    "area_side",
    "communication_range",
    "topology",
    "seed",
    "task_control",
    "mission_completed",
    "tasks_completed",
    "mission_duration",
    "dropped_ownership_time",
    "concurrent_ownership_duration",
    "undetected_failures",
    "maximum_components",
)


def run_generalization(seeds: Sequence[int]) -> Iterator[dict[str, str]]:
    """Yield paired rows for 4/6 UAVs across three network regimes.

    Two tasks per UAV and area proportional to fleet size hold task and initial
    spatial density roughly constant. The 6-UAV initial layout differs from
    the 4-UAV corner layout, so fleet effects are not isolated from geometry.
    """

    for agents in (4, 6):
        scale = math.sqrt(agents / 4)
        for topology in ("connected", "sparse", "sparse_jammed"):
            jammer = (
                None
                if topology != "sparse_jammed"
                else RegionJammer(40 * scale, 50 * scale, 15 * scale, 3.0, 8.0)
            )
            communication_range = (130 if topology == "connected" else 50) * scale
            for seed in seeds:
                for task_control in ("distributed", "centralized"):
                    config = SimulationConfig(
                        agent_count=agents,
                        task_count=2 * agents,
                        area_width=100 * scale,
                        area_height=100 * scale,
                        communication_range=communication_range,
                        region_jammer=jammer,
                        task_control=task_control,
                        random_seed=seed,
                        failure_time=4.0,
                        max_simulation_time=30.0,
                        time_step=0.5,
                    )
                    metrics = Simulation(config).run().metrics
                    duration = metrics.simulation_duration
                    yield {
                        "agents": str(agents),
                        "tasks": str(config.task_count),
                        "area_side": f"{config.area_width:.6f}",
                        "communication_range": f"{config.communication_range:.6f}",
                        "topology": topology,
                        "seed": str(seed),
                        "task_control": task_control,
                        "mission_completed": "1" if metrics.mission_completed else "0",
                        "tasks_completed": str(metrics.completed_task_count),
                        "mission_duration": f"{duration:.6f}",
                        "dropped_ownership_time": (
                            f"{metrics.dropped_ownership_time:.6f}"
                        ),
                        "concurrent_ownership_duration": (
                            f"{metrics.concurrent_ownership_duration:.6f}"
                        ),
                        "undetected_failures": str(metrics.undetected_failure_count),
                        "maximum_components": str(
                            metrics.maximum_connected_component_count
                        ),
                    }


def write_csv(rows: Iterator[dict[str, str]], stream: TextIO) -> None:
    writer = csv.DictWriter(stream, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run paired fleet-size and network-condition comparisons."
    )
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--seed-base", type=int, default=1)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.seeds < 1 or args.seed_base < 0:
        parser.error("--seeds must be positive and --seed-base non-negative")
    seeds = tuple(range(args.seed_base, args.seed_base + args.seeds))
    if args.output is None:
        write_csv(run_generalization(seeds), sys.stdout)
    else:
        buffer = io.StringIO()
        write_csv(run_generalization(seeds), buffer)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
