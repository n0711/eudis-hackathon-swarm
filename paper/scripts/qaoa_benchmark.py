"""Benchmark the advisory QAOA planner against the exact optimum (Table VII).

Run from the repository root with the quantum extra installed:

    pip install -e ".[quantum]"
    python paper/scripts/qaoa_benchmark.py

Instances are seeded, so the optimum matches and probabilities reproduce;
wall-clock times depend on the machine.
"""

from __future__ import annotations

import math
import random
import statistics

from eudis_swarm.quantum_planner import plan_assignment

SIZES = ((2, 2), (3, 2), (4, 2), (3, 3), (5, 2), (4, 3))
INSTANCES = 20


def main() -> None:
    print(
        "lms tgts qubits optimal p_opt_qaoa p_uniform p_uniform_valid "
        "mean_s worst_gap_pct"
    )
    for lms_n, targets_n in SIZES:
        rng = random.Random(1000 + 10 * lms_n + targets_n)
        matches, probabilities, seconds, gaps = 0, [], [], []
        for _ in range(INSTANCES):
            # LMs on the launch row south of the area; targets inside the
            # default 240 m x 160 m search area.
            lms = {
                i + 1: (rng.uniform(0, 240), rng.uniform(-20, 0)) for i in range(lms_n)
            }
            targets = {
                10 + j: (rng.uniform(0, 240), rng.uniform(0, 160))
                for j in range(targets_n)
            }
            plan = plan_assignment(lms, targets)
            matches += plan.matches_exact
            probabilities.append(plan.probability_optimal)
            seconds.append(plan.seconds)
            gaps.append(100.0 * (plan.cost - plan.exact_cost) / plan.exact_cost)
        reachable = lms_n**targets_n if lms_n >= targets_n else targets_n**lms_n
        valid = math.perm(max(lms_n, targets_n), min(lms_n, targets_n))
        print(
            lms_n,
            targets_n,
            lms_n * targets_n,
            f"{matches}/{INSTANCES}",
            f"{statistics.mean(probabilities):.3f}",
            f"{1 / reachable:.3f}",
            f"{1 / valid:.3f}",
            f"{statistics.mean(seconds):.2f}",
            f"{max(gaps):.2f}",
        )


if __name__ == "__main__":
    main()
