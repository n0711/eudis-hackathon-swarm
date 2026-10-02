"""Coordinated area search as distributed allocation of search sectors.

The mission area is cut into square sectors. Each sector is an ordinary task
whose objective is the sector centre: a UAV that reaches it is taken to have
searched the sector (its sensor footprint is assumed to cover one sector).
Nothing in the coordination layer changes. The same receiver-local utility,
claims, leases and reconciliation that allocate any other task decide which UAV
searches which sector, and a lost UAV's sectors are re-searched by the others
only after their own copies of its leases expire.

This module is scenario construction plus observer measurement. It builds the
sector catalogue and reads coverage from the finished run's mission events; it
never feeds anything back into a UAV's decisions.

Run it:

    python -m eudis_swarm.search_mission                     # one run, both modes
    python -m eudis_swarm.search_mission --study --csv out.csv   # paired study
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .communication import RegionJammer
from .config import SimulationConfig
from .mission import MissionEventKind
from .simulation import Simulation, SimulationResult
from .task import Task

#: Coverage fractions reported as "time to reach".
COVERAGE_MILESTONES = (0.5, 0.9, 1.0)

#: A failure time beyond any mission length disables the injected failure.
NO_FAILURE_TIME = 1.0e9


def sector_tasks(
    area_width: float, area_height: float, sector_size: float
) -> list[Task]:
    """Return one task per sector centre, in boustrophedon (lawnmower) order.

    Edge sectors are clipped to the area, and their centres move with them, so
    every centre lies inside the area. Task IDs run 1..N in the order a single
    vehicle would sweep the area row by row.
    """

    for name, value in (
        ("area_width", area_width),
        ("area_height", area_height),
        ("sector_size", sector_size),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be positive and finite")
    columns = math.ceil(area_width / sector_size)
    rows = math.ceil(area_height / sector_size)
    tasks: list[Task] = []
    for row in range(rows):
        y_low = row * sector_size
        y_high = min((row + 1) * sector_size, area_height)
        order = range(columns) if row % 2 == 0 else range(columns - 1, -1, -1)
        for column in order:
            x_low = column * sector_size
            x_high = min((column + 1) * sector_size, area_width)
            tasks.append(
                Task(
                    task_id=len(tasks) + 1,
                    position=((x_low + x_high) / 2.0, (y_low + y_high) / 2.0),
                )
            )
    return tasks


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Observer-computed search coverage for one finished run."""

    sector_count: int
    first_searched_at: dict[int, float]
    searched_by: dict[int, int]
    redundant_searches: int
    mission_completed: bool
    duration: float

    @property
    def searched_count(self) -> int:
        return len(self.first_searched_at)

    @property
    def coverage(self) -> float:
        return self.searched_count / self.sector_count

    def time_to_coverage(self, fraction: float) -> float | None:
        """Logical time at which ``fraction`` of the sectors had been searched."""

        if not 0.0 < fraction <= 1.0:
            raise ValueError("fraction must be in (0, 1]")
        needed = math.ceil(fraction * self.sector_count - 1e-9)
        times = sorted(self.first_searched_at.values())
        if len(times) < needed:
            return None
        return times[needed - 1]

    def curve(self, step: float) -> list[tuple[float, float]]:
        """Coverage fraction sampled every ``step`` logical seconds."""

        if not math.isfinite(step) or step <= 0.0:
            raise ValueError("step must be positive")
        times = sorted(self.first_searched_at.values())
        samples: list[tuple[float, float]] = []
        index = 0
        searched = 0
        count = math.floor(self.duration / step + 1e-9)
        for k in range(count + 1):
            t = round(k * step, 9)
            while index < len(times) and times[index] <= t:
                searched += 1
                index += 1
            samples.append((t, searched / self.sector_count))
        if samples[-1][0] < self.duration:
            final = sum(1 for time in times if time <= self.duration)
            samples.append((self.duration, final / self.sector_count))
        return samples


def coverage_report(result: SimulationResult) -> CoverageReport:
    """Read sector coverage from a finished run's mission events."""

    first: dict[int, float] = {}
    by: dict[int, int] = {}
    redundant = 0
    for event in result.mission.events:
        if event.kind is MissionEventKind.TASK_COMPLETED and event.task_id is not None:
            if event.task_id not in first:
                first[event.task_id] = event.timestamp
                if event.agent_id is not None:
                    by[event.task_id] = event.agent_id
        elif event.kind is MissionEventKind.TASK_DUPLICATED:
            redundant += 1
    return CoverageReport(
        sector_count=len(result.mission.tasks),
        first_searched_at=first,
        searched_by=by,
        redundant_searches=redundant,
        mission_completed=result.metrics.mission_completed,
        duration=result.metrics.simulation_duration,
    )


def search_config(
    *,
    sector_count: int,
    agents: int = 4,
    area: float = 100.0,
    task_control: str = "distributed",
    failure_agent: int = 2,
    failure_time: float = NO_FAILURE_TIME,
    jammer: RegionJammer | None = None,
    max_time: float = 300.0,
) -> SimulationConfig:
    """The base configuration for a search run over ``sector_count`` sectors."""

    return replace(
        SimulationConfig(),
        agent_count=agents,
        task_count=sector_count,
        area_width=area,
        area_height=area,
        task_control=task_control,
        failure_agent_id=failure_agent,
        failure_time=failure_time,
        region_jammer=jammer,
        max_simulation_time=max_time,
    )


def run_search(config: SimulationConfig, sector_size: float) -> CoverageReport:
    """Run one search mission and report its coverage."""

    tasks = sector_tasks(config.area_width, config.area_height, sector_size)
    if len(tasks) != config.task_count:
        raise ValueError("config.task_count must equal the number of sectors")
    return coverage_report(Simulation(config, tasks=tasks).run())


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


STUDY_FIELDS: tuple[str, ...] = (
    "task_control",
    "failure_agent",
    "failure_time",
    "jammer",
    "sectors",
    "searched",
    "mission_completed",
    "t50",
    "t90",
    "t100",
    "redundant_searches",
    "duration",
)


def study_rows(
    *,
    sector_size: float,
    agents: int,
    area: float,
    failure_times: Sequence[float],
    jammer: RegionJammer,
) -> list[dict[str, object]]:
    """Paired runs: every fault case under both control modes, same sectors."""

    sector_count = len(sector_tasks(area, area, sector_size))
    rows: list[dict[str, object]] = []
    for jammed in (False, True):
        for failure_agent in range(1, agents + 1):
            for failure_time in failure_times:
                for task_control in ("distributed", "centralized"):
                    config = search_config(
                        sector_count=sector_count,
                        agents=agents,
                        area=area,
                        task_control=task_control,
                        failure_agent=failure_agent,
                        failure_time=failure_time,
                        jammer=jammer if jammed else None,
                    )
                    report = run_search(config, sector_size)
                    rows.append(
                        {
                            "task_control": task_control,
                            "failure_agent": failure_agent,
                            "failure_time": failure_time,
                            "jammer": jammed,
                            "sectors": report.sector_count,
                            "searched": report.searched_count,
                            "mission_completed": report.mission_completed,
                            "t50": report.time_to_coverage(0.5),
                            "t90": report.time_to_coverage(0.9),
                            "t100": report.time_to_coverage(1.0),
                            "redundant_searches": report.redundant_searches,
                            "duration": report.duration,
                        }
                    )
    return rows


def summarise(rows: Sequence[dict[str, object]]) -> list[str]:
    """One line per control mode and jammer case: completions, times, redundancy."""

    lines = []
    for jammed in (False, True):
        for mode in ("distributed", "centralized"):
            subset = [
                row
                for row in rows
                if row["task_control"] == mode and row["jammer"] is jammed
            ]
            if not subset:
                continue
            completed = sum(1 for row in subset if row["mission_completed"] is True)
            full = [row["t100"] for row in subset]
            full_times = [value for value in full if isinstance(value, float)]
            redundant = sum(
                value
                for value in (row["redundant_searches"] for row in subset)
                if isinstance(value, int)
            )
            mean_full = sum(full_times) / len(full_times) if full_times else None
            lines.append(
                f"jammer={'on ' if jammed else 'off'} {mode:12s} runs "
                f"{len(subset):3d}  full coverage {completed:3d}/{len(subset)}  "
                f"mean t100 {_fmt(mean_full)}  redundant searches {redundant}"
            )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coordinated sector search.")
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--area", type=float, default=100.0)
    parser.add_argument("--sector", type=float, default=20.0)
    parser.add_argument("--failure-agent", type=int, default=2)
    parser.add_argument(
        "--failure-time",
        type=float,
        default=8.0,
        help="logical time of the physical failure (negative disables it)",
    )
    parser.add_argument(
        "--jammer",
        default=None,
        help="x,y,radius,start,end of a circular jamming region",
    )
    parser.add_argument("--study", action="store_true", help="run the paired study")
    parser.add_argument("--csv", type=Path, default=None, help="study CSV output")
    arguments = parser.parse_args(argv)

    jammer = None
    if arguments.jammer is not None:
        parts = [float(part) for part in arguments.jammer.split(",")]
        if len(parts) != 5:
            parser.error("--jammer needs x,y,radius,start,end")
        jammer = RegionJammer(*parts)

    if arguments.study:
        rows = study_rows(
            sector_size=arguments.sector,
            agents=arguments.agents,
            area=arguments.area,
            failure_times=(4.0, 8.0, 12.0, 16.0),
            jammer=jammer
            or RegionJammer(arguments.area / 2, arguments.area / 2, 25.0, 3.0, 15.0),
        )
        if arguments.csv is not None:
            with arguments.csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=STUDY_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
        for line in summarise(rows):
            print(line)
        return 0

    failure_time = (
        NO_FAILURE_TIME if arguments.failure_time < 0 else arguments.failure_time
    )
    sector_count = len(sector_tasks(arguments.area, arguments.area, arguments.sector))
    print(
        f"search: {sector_count} sectors of {arguments.sector:g} m, "
        f"{arguments.agents} UAVs, failure of UAV {arguments.failure_agent} at "
        f"{'never' if failure_time == NO_FAILURE_TIME else f'{failure_time:g} s'}"
    )
    print(
        f"{'mode':12s} {'searched':>9s} {'t50':>7s} {'t90':>7s} {'t100':>7s} "
        f"{'redundant':>9s}"
    )
    for task_control in ("distributed", "centralized"):
        config = search_config(
            sector_count=sector_count,
            agents=arguments.agents,
            area=arguments.area,
            task_control=task_control,
            failure_agent=arguments.failure_agent,
            failure_time=failure_time,
            jammer=jammer,
        )
        report = run_search(config, arguments.sector)
        print(
            f"{task_control:12s} {report.searched_count:4d}/{report.sector_count:<4d} "
            f"{_fmt(report.time_to_coverage(0.5)):>7s} "
            f"{_fmt(report.time_to_coverage(0.9)):>7s} "
            f"{_fmt(report.time_to_coverage(1.0)):>7s} "
            f"{report.redundant_searches:9d}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
