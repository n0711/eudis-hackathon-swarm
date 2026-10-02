"""Paired comparison of the distance, connectivity and quantum policies.

Every policy runs on the same seeds, fleet sizes and network regimes as the
generalization study (connected, sparse, and sparse with a region jammer that
partitions the swarm for 3 s <= t < 8 s). Only ``allocation_policy`` and the
quantum solver differ between paired rows.

Three outputs:

* run rows (:data:`RUN_COLUMNS`): mission and coordination metrics per run;
* solve rows (:data:`SOLVE_COLUMNS`): one per receiver-local quantum solve,
  with its optimality gap against exact brute force on the same problem;
* optional timing rows (:data:`TIMING_COLUMNS`): wall-clock time to re-solve
  each recorded problem with each solver. Timing is measured here, outside
  the simulator, and is the only non-deterministic output; nothing pins it.

All other numbers are deterministic for the same command line.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .communication import RegionJammer
from .config import SimulationConfig
from .quantum_opt import solve, solve_brute_force
from .simulation import Simulation, SimulationResult

#: (label, allocation_policy, quantum_solver)
POLICIES: tuple[tuple[str, str, str], ...] = (
    ("distance", "distance", "qaoa"),
    ("connectivity", "connectivity", "qaoa"),
    ("quantum_qaoa", "quantum", "qaoa"),
    ("quantum_sqa", "quantum", "sqa"),
)
TOPOLOGIES: tuple[str, ...] = ("connected", "sparse", "sparse_jammed")
FLEETS: tuple[int, ...] = (4, 6)

RUN_COLUMNS: tuple[str, ...] = (
    "agents",
    "tasks",
    "topology",
    "seed",
    "policy",
    "mission_completed",
    "tasks_completed",
    "mission_duration",
    "flight_distance",
    "concurrent_ownership_duration",
    "contested_duration",
    "claims_refused",
    "contested_yields",
    "quantum_solves",
    "quantum_changed_choices",
    "quantum_suboptimal_solves",
)
SOLVE_COLUMNS: tuple[str, ...] = (
    "agents",
    "tasks",
    "topology",
    "seed",
    "policy",
    "agent_id",
    "timestamp",
    "formulation_agents",
    "formulation_objectives",
    "qubits",
    "feasible",
    "solver_cost",
    "optimal_cost",
    "optimality_gap",
    "evaluations",
    "returned_probability",
    "feasible_probability",
    "utility_task_id",
    "proposed_task_id",
)
TIMING_COLUMNS: tuple[str, ...] = (
    "qubits",
    "solver",
    "solves",
    "mean_seconds",
    "max_seconds",
)


@dataclass(frozen=True, slots=True)
class StudyCase:
    agents: int
    topology: str
    seed: int
    policy: str
    config: SimulationConfig


def study_cases(
    seeds: Sequence[int],
    *,
    fleets: Sequence[int] = FLEETS,
    topologies: Sequence[str] = TOPOLOGIES,
    policies: Sequence[tuple[str, str, str]] = POLICIES,
) -> Iterator[StudyCase]:
    """Yield paired configurations in a fixed order."""

    for agents in fleets:
        scale = math.sqrt(agents / 4)
        for topology in topologies:
            jammer = (
                RegionJammer(40 * scale, 50 * scale, 15 * scale, 3.0, 8.0)
                if topology == "sparse_jammed"
                else None
            )
            communication_range = (130 if topology == "connected" else 50) * scale
            for seed in seeds:
                for label, allocation_policy, solver in policies:
                    yield StudyCase(
                        agents=agents,
                        topology=topology,
                        seed=seed,
                        policy=label,
                        config=SimulationConfig(
                            agent_count=agents,
                            task_count=2 * agents,
                            area_width=100 * scale,
                            area_height=100 * scale,
                            communication_range=communication_range,
                            region_jammer=jammer,
                            random_seed=seed,
                            failure_time=6.0,
                            max_simulation_time=30.0,
                            time_step=0.5,
                            allocation_policy=allocation_policy,
                            quantum_solver=solver,
                        ),
                    )


def flight_distance(result: SimulationResult) -> float:
    """Total path length of every UAV, from the recorded position history."""

    return sum(
        math.dist(earlier[1], later[1])
        for history in result.position_history.values()
        for earlier, later in zip(history, history[1:])
    )


def _gap(solver_cost: float | None, optimal_cost: float) -> float | None:
    if solver_cost is None:
        return None
    if optimal_cost == 0.0:
        return 0.0 if solver_cost == 0.0 else math.inf
    return (solver_cost - optimal_cost) / optimal_cost


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    case: StudyCase
    run_row: dict[str, str]
    solve_rows: tuple[dict[str, str], ...]
    result: SimulationResult


def run_case(case: StudyCase) -> CaseOutcome:
    """Run one configuration and evaluate its solves against brute force."""

    result = Simulation(case.config).run()
    metrics = result.metrics
    common = {
        "agents": str(case.agents),
        "tasks": str(case.config.task_count),
        "topology": case.topology,
        "seed": str(case.seed),
        "policy": case.policy,
    }
    solve_rows: list[dict[str, str]] = []
    suboptimal = 0
    for record in result.quantum_solves:
        proposal = record.proposal
        problem = proposal.problem
        solution = proposal.solution
        optimal = solve_brute_force(problem)
        assert optimal.cost is not None
        gap = _gap(solution.cost, optimal.cost)
        if gap is None or gap > 1e-9:
            suboptimal += 1
        solve_rows.append(
            {
                **common,
                "agent_id": str(record.agent_id),
                "timestamp": f"{record.timestamp:.6f}",
                "formulation_agents": str(len(problem.agent_ids)),
                "formulation_objectives": str(len(problem.task_ids)),
                "qubits": str(problem.num_variables),
                "feasible": "1" if solution.feasible else "0",
                "solver_cost": _fmt(solution.cost),
                "optimal_cost": _fmt(optimal.cost),
                "optimality_gap": _fmt(gap),
                "evaluations": str(solution.evaluations),
                "returned_probability": _fmt(solution.probability),
                "feasible_probability": _fmt(solution.feasible_probability),
                "utility_task_id": str(proposal.utility_task_id),
                "proposed_task_id": str(proposal.proposed_task_id),
            }
        )
    run_row = {
        **common,
        "mission_completed": "1" if metrics.mission_completed else "0",
        "tasks_completed": str(metrics.completed_task_count),
        "mission_duration": f"{metrics.simulation_duration:.6f}",
        "flight_distance": f"{flight_distance(result):.6f}",
        "concurrent_ownership_duration": (
            f"{metrics.concurrent_ownership_duration:.6f}"
        ),
        "contested_duration": f"{metrics.contested_duration:.6f}",
        "claims_refused": str(metrics.claim_refused_allocation_count),
        "contested_yields": str(metrics.contested_task_yield_count),
        "quantum_solves": str(len(result.quantum_solves)),
        "quantum_changed_choices": str(
            sum(record.proposal.changed_choice for record in result.quantum_solves)
        ),
        "quantum_suboptimal_solves": str(suboptimal),
    }
    return CaseOutcome(case, run_row, tuple(solve_rows), result)


def time_solvers(
    outcomes: Iterable[CaseOutcome],
    solvers: Sequence[str] = ("brute_force", "qaoa", "sqa"),
) -> list[dict[str, str]]:
    """Wall-clock re-solve of every recorded problem; non-deterministic."""

    samples: dict[tuple[int, str], list[float]] = {}
    for outcome in outcomes:
        for record in outcome.result.quantum_solves:
            problem = record.proposal.problem
            for solver in solvers:
                started = time.perf_counter()
                solve(problem, solver)
                elapsed = time.perf_counter() - started
                samples.setdefault((problem.num_variables, solver), []).append(elapsed)
    return [
        {
            "qubits": str(qubits),
            "solver": solver,
            "solves": str(len(values)),
            "mean_seconds": f"{sum(values) / len(values):.6f}",
            "max_seconds": f"{max(values):.6f}",
        }
        for (qubits, solver), values in sorted(samples.items())
    ]


def summarize(run_rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    """Per (agents, topology, policy): means over seeds, plus paired deltas.

    Deltas are against the distance policy on the same seed.
    """

    baseline = {
        (row["agents"], row["topology"], row["seed"]): row
        for row in run_rows
        if row["policy"] == "distance"
    }
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in run_rows:
        groups.setdefault((row["agents"], row["topology"], row["policy"]), []).append(
            row
        )
    summary: list[dict[str, str]] = []
    for (agents, topology, policy), rows in groups.items():
        count = len(rows)

        def mean(column: str, rows: list[dict[str, str]] = rows) -> float:
            return sum(float(row[column]) for row in rows) / len(rows)

        better = worse = 0
        for row in rows:
            reference = baseline[(agents, topology, row["seed"])]
            delta = float(row["mission_duration"]) - float(
                reference["mission_duration"]
            )
            better += delta < 0
            worse += delta > 0
        summary.append(
            {
                "agents": agents,
                "topology": topology,
                "policy": policy,
                "runs": str(count),
                "completed": str(sum(int(row["mission_completed"]) for row in rows)),
                "mean_duration": f"{mean('mission_duration'):.3f}",
                "mean_distance": f"{mean('flight_distance'):.1f}",
                "mean_concurrent": f"{mean('concurrent_ownership_duration'):.3f}",
                "mean_contested": f"{mean('contested_duration'):.3f}",
                "claims_refused": str(sum(int(row["claims_refused"]) for row in rows)),
                "contested_yields": str(
                    sum(int(row["contested_yields"]) for row in rows)
                ),
                "faster_than_distance": str(better),
                "slower_than_distance": str(worse),
                "quantum_solves": str(sum(int(row["quantum_solves"]) for row in rows)),
                "changed_choices": str(
                    sum(int(row["quantum_changed_choices"]) for row in rows)
                ),
                "suboptimal_solves": str(
                    sum(int(row["quantum_suboptimal_solves"]) for row in rows)
                ),
            }
        )
    return summary


def write_csv(
    rows: Iterable[dict[str, str]], columns: Sequence[str], stream: TextIO
) -> None:
    writer = csv.DictWriter(stream, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)


def _emit(
    rows: Iterable[dict[str, str]], columns: Sequence[str], path: Path | None
) -> None:
    if path is None:
        write_csv(rows, columns, sys.stdout)
        return
    buffer = io.StringIO()
    write_csv(rows, columns, buffer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Paired distance / connectivity / quantum policy study."
    )
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--seed-base", type=int, default=1)
    parser.add_argument(
        "--fleets", type=int, nargs="+", default=list(FLEETS), choices=FLEETS
    )
    parser.add_argument(
        "--topologies",
        nargs="+",
        default=list(TOPOLOGIES),
        choices=TOPOLOGIES,
    )
    parser.add_argument("--output", type=Path, default=None, help="run-level CSV")
    parser.add_argument("--solves-output", type=Path, default=None)
    parser.add_argument("--summary-output", type=Path, default=None)
    parser.add_argument(
        "--timing-output",
        type=Path,
        default=None,
        help="also re-solve every recorded problem with each solver and write "
        "wall-clock timings (non-deterministic) here",
    )
    args = parser.parse_args(argv)
    if args.seeds < 1 or args.seed_base < 0:
        parser.error("--seeds must be positive and --seed-base non-negative")
    seeds = tuple(range(args.seed_base, args.seed_base + args.seeds))
    outcomes = [
        run_case(case)
        for case in study_cases(seeds, fleets=args.fleets, topologies=args.topologies)
    ]
    run_rows = [outcome.run_row for outcome in outcomes]
    _emit(run_rows, RUN_COLUMNS, args.output)
    if args.solves_output is not None:
        _emit(
            (row for outcome in outcomes for row in outcome.solve_rows),
            SOLVE_COLUMNS,
            args.solves_output,
        )
    if args.summary_output is not None:
        summary = summarize(run_rows)
        _emit(summary, tuple(summary[0]), args.summary_output)
    if args.timing_output is not None:
        _emit(time_solvers(outcomes), TIMING_COLUMNS, args.timing_output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
