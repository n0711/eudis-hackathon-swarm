"""Paired layout and failure-time checks for the generalization study.

Only evaluator inputs vary. Task locations remain paired by seed and the
simulation's coordination and movement implementations are unchanged.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from pathlib import Path
from typing import Iterator, Sequence, TextIO

from .agent import Agent
from .communication import RegionJammer
from .config import SimulationConfig
from .generalization import CSV_COLUMNS
from .simulation import Simulation

ROBUSTNESS_COLUMNS: tuple[str, ...] = (
    "layout",
    "failure_time",
    *CSV_COLUMNS,
    "failure_injected",
)


def _ring_agents(config: SimulationConfig) -> list[Agent]:
    """Place every UAV on a centered ring, independently of task placement."""

    center = (config.area_width / 2, config.area_height / 2)
    radius = min(config.area_width, config.area_height) * 0.35
    return [
        Agent(
            agent_id=index + 1,
            position=(
                center[0]
                + radius
                * math.cos(math.pi / 4 + 2 * math.pi * index / config.agent_count),
                center[1]
                + radius
                * math.sin(math.pi / 4 + 2 * math.pi * index / config.agent_count),
            ),
            speed=config.agent_speed,
        )
        for index in range(config.agent_count)
    ]


def run_robustness(seeds: Sequence[int]) -> Iterator[dict[str, str]]:
    """Yield matched control runs over two layouts and two fault times."""

    for agents in (4, 6):
        scale = math.sqrt(agents / 4)
        for layout in ("default", "ring"):
            for failure_time in (2.0, 6.0):
                for topology in ("connected", "sparse", "sparse_jammed"):
                    jammer = (
                        None
                        if topology != "sparse_jammed"
                        else RegionJammer(40 * scale, 50 * scale, 15 * scale, 3.0, 8.0)
                    )
                    communication_range = (
                        130 if topology == "connected" else 50
                    ) * scale
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
                                failure_time=failure_time,
                                max_simulation_time=30.0,
                                time_step=0.5,
                            )
                            starting_agents = (
                                None if layout == "default" else _ring_agents(config)
                            )
                            # Simulation builds seeded tasks independently of
                            # these starting positions, so conditions stay paired.
                            result = Simulation(config, agents=starting_agents).run()
                            metrics = result.metrics
                            yield {
                                "layout": layout,
                                "failure_time": f"{failure_time:.6f}",
                                "agents": str(agents),
                                "tasks": str(config.task_count),
                                "area_side": f"{config.area_width:.6f}",
                                "communication_range": (
                                    f"{config.communication_range:.6f}"
                                ),
                                "topology": topology,
                                "seed": str(seed),
                                "task_control": task_control,
                                "mission_completed": "1"
                                if metrics.mission_completed
                                else "0",
                                "tasks_completed": str(metrics.completed_task_count),
                                "mission_duration": f"{metrics.simulation_duration:.6f}",
                                "dropped_ownership_time": (
                                    f"{metrics.dropped_ownership_time:.6f}"
                                ),
                                "concurrent_ownership_duration": (
                                    f"{metrics.concurrent_ownership_duration:.6f}"
                                ),
                                "undetected_failures": str(
                                    metrics.undetected_failure_count
                                ),
                                "maximum_components": str(
                                    metrics.maximum_connected_component_count
                                ),
                                "failure_injected": (
                                    "1" if metrics.physically_failed_count == 1 else "0"
                                ),
                            }


def write_csv(rows: Iterator[dict[str, str]], stream: TextIO) -> None:
    writer = csv.DictWriter(
        stream, fieldnames=list(ROBUSTNESS_COLUMNS), lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run paired layout and fault-time checks."
    )
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--seed-base", type=int, default=1)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.seeds < 1 or args.seed_base < 0:
        parser.error("--seeds must be positive and --seed-base non-negative")
    seeds = tuple(range(args.seed_base, args.seed_base + args.seeds))
    if args.output is None:
        write_csv(run_robustness(seeds), sys.stdout)
    else:
        buffer = io.StringIO()
        write_csv(run_robustness(seeds), buffer)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
