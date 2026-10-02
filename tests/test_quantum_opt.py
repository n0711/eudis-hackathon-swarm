"""The assignment QUBO and its three solvers."""

from __future__ import annotations

import ast
import math
import random
from itertools import product
from pathlib import Path

import pytest

from eudis_swarm.quantum_opt import (
    MAX_QAOA_QUBITS,
    AssignmentProblem,
    QaoaSettings,
    Qubo,
    SqaSettings,
    assignment_qubo,
    derive_seed,
    feasible_assignment_count,
    qaoa_statevector,
    qubo_brute_force_minimum,
    qubo_to_bqm,
    qubo_to_ising,
    qubo_to_ising_model,
    solve,
    solve_brute_force,
    solve_qaoa,
    solve_sqa,
)
from eudis_swarm.task_utility import TaskObjective

SHAPES = ((1, 1), (1, 3), (3, 1), (2, 2), (2, 3), (3, 2), (3, 3), (2, 4), (4, 3))


def _random_problem(
    rng: random.Random, agents: int, tasks: int, *, leads: bool = False
) -> AssignmentProblem:
    return assignment_qubo(
        [(i + 1, (rng.uniform(0, 100), rng.uniform(0, 100))) for i in range(agents)],
        [
            TaskObjective(t + 1, (rng.uniform(0, 100), rng.uniform(0, 100)))
            for t in range(tasks)
        ],
        lead_costs=([rng.uniform(0, 50) for _ in range(agents)] if leads else None),
    )


def _all_bits(n: int) -> list[tuple[int, ...]]:
    return [tuple(bits) for bits in product((0, 1), repeat=n)]


@pytest.mark.parametrize("shape", SHAPES)
def test_qubo_minimum_equals_exact_assignment_optimum(shape: tuple[int, int]) -> None:
    rng = random.Random(hash(shape) & 0xFFFF)
    for trial in range(4):
        problem = _random_problem(rng, *shape, leads=trial % 2 == 1)
        exact = solve_brute_force(problem)
        energy, bits = qubo_brute_force_minimum(problem.qubo)
        assert exact.cost is not None
        assert energy == pytest.approx(exact.cost, abs=1e-9)
        assert problem.decode(bits) is not None
        assert problem.assignment_cost(problem.decode(bits) or ()) == pytest.approx(
            exact.cost, abs=1e-9
        )


def test_penalty_dominates_every_infeasible_state() -> None:
    problem = _random_problem(random.Random(3), 3, 3)
    exact = solve_brute_force(problem)
    assert exact.cost is not None
    bound = problem.assignments_required * max(max(row) for row in problem.costs)
    assert problem.penalty == pytest.approx(bound + 1.0)
    for bits in _all_bits(problem.num_variables):
        pairs = problem.decode(bits)
        energy = problem.qubo.energy(bits)
        if pairs is None:
            assert energy >= problem.penalty > bound >= exact.cost
        else:
            assert energy == pytest.approx(problem.assignment_cost(pairs), abs=1e-9)


def test_feasibility_follows_the_smaller_side() -> None:
    wide = _random_problem(random.Random(1), 2, 3)
    assert wide.decode((1, 0, 0, 0, 1, 0)) == ((1, 1), (2, 2))
    assert wide.decode((1, 0, 0, 0, 0, 0)) is None  # agent 2 idle
    assert wide.decode((1, 0, 0, 1, 0, 0)) is None  # objective shared
    tall = _random_problem(random.Random(1), 3, 2)
    assert tall.decode((1, 0, 0, 0, 0, 1)) == ((1, 1), (3, 2))
    assert tall.decode((1, 0, 0, 0, 0, 0)) is None  # objective 2 uncovered
    assert tall.decode((1, 1, 0, 0, 0, 0)) is None  # agent 1 doubled
    with pytest.raises(ValueError):
        tall.decode((1, 0))
    assert feasible_assignment_count(wide) == 6
    assert feasible_assignment_count(tall) == 6


def test_assignment_qubo_rejects_bad_inputs() -> None:
    objective = TaskObjective(1, (0.0, 0.0))
    with pytest.raises(ValueError):
        assignment_qubo([], [objective])
    with pytest.raises(ValueError):
        assignment_qubo([(1, (0.0, 0.0)), (1, (1.0, 1.0))], [objective])
    with pytest.raises(ValueError):
        assignment_qubo([(1, (0.0, 0.0))], [objective, objective])
    with pytest.raises(TypeError):
        assignment_qubo([(1, (0.0, 0.0))], [object()])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        assignment_qubo([(1, (math.inf, 0.0))], [objective])
    with pytest.raises(ValueError):
        assignment_qubo([(1, (0.0, 0.0))], [objective], lead_costs=[-1.0])
    with pytest.raises(ValueError):
        assignment_qubo([(1, (0.0, 0.0))], [objective], lead_costs=[1.0, 2.0])


def test_qubo_validates_its_shape() -> None:
    with pytest.raises(ValueError):
        Qubo(labels=("a",), linear=(1.0, 2.0), quadratic=())
    with pytest.raises(ValueError):
        Qubo(labels=("a", "a"), linear=(1.0, 2.0), quadratic=())
    with pytest.raises(ValueError):
        Qubo(labels=("a", "b"), linear=(1.0, 2.0), quadratic=((1, 0, 1.0),))
    with pytest.raises(ValueError):
        Qubo(
            labels=("a", "b", "c"),
            linear=(0.0, 0.0, 0.0),
            quadratic=((0, 2, 1.0), (0, 1, 1.0)),
        )
    qubo = Qubo(labels=("a", "b"), linear=(1.0, 2.0), quadratic=((0, 1, -4.0),))
    assert qubo.energy((1, 1)) == -1.0
    with pytest.raises(ValueError):
        qubo.energy((1,))


def test_qubo_enumeration_is_bounded() -> None:
    labels = tuple(f"v{i}" for i in range(21))
    with pytest.raises(ValueError):
        qubo_brute_force_minimum(Qubo(labels, (0.0,) * 21, ()))


def test_brute_force_is_bounded() -> None:
    problem = _random_problem(random.Random(2), 3, 9)  # 504 assignments
    assert solve_brute_force(problem).feasible
    big = _random_problem(random.Random(2), 7, 9)  # 181440 assignments
    with pytest.raises(ValueError):
        solve_brute_force(big)


def test_qaoa_statevector_preserves_norm() -> None:
    rng = random.Random(9)
    energies = [rng.uniform(0, 1) for _ in range(16)]
    for depth in range(4):
        gammas = [rng.uniform(0, 6) for _ in range(depth)]
        betas = [rng.uniform(0, 3) for _ in range(depth)]
        real, imag = qaoa_statevector(energies, gammas, betas)
        norm = sum(re * re + im * im for re, im in zip(real, imag))
        assert norm == pytest.approx(1.0, abs=1e-12)


def test_qaoa_with_no_layers_is_uniform() -> None:
    real, imag = qaoa_statevector([0.3, 0.1, 0.9, 0.2, 0.5, 0.0, 0.7, 0.4], [], [])
    assert [re * re + im * im for re, im in zip(real, imag)] == pytest.approx(
        [1 / 8] * 8
    )


def test_qaoa_statevector_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        qaoa_statevector([0.0, 1.0, 2.0], [], [])
    with pytest.raises(ValueError):
        qaoa_statevector([0.0, 1.0], [0.1], [])


def test_single_qubit_mixer_matches_closed_form() -> None:
    # The phase layer maps |+> to (|0> - i|1>)/sqrt(2); exp(-i pi/4 X) is
    # (I - iX)/sqrt(2), which maps that state to -i|1>.
    real, imag = qaoa_statevector([0.0, 1.0], [math.pi / 2], [math.pi / 4])
    probabilities = [re * re + im * im for re, im in zip(real, imag)]
    assert probabilities == pytest.approx([0.0, 1.0], abs=1e-12)
    assert imag[1] == pytest.approx(-1.0)
    # Without a phase difference the mixer leaves |+> unchanged up to phase.
    real, imag = qaoa_statevector([0.0, 0.0], [1.0], [0.7])
    assert [re * re + im * im for re, im in zip(real, imag)] == pytest.approx(
        [0.5, 0.5]
    )


def test_qaoa_solves_a_known_tiny_instance() -> None:
    # Two UAVs on a line, two objectives: crossing costs 160 m, the parallel
    # assignment 20 m, so the optimum is unambiguous.
    problem = assignment_qubo(
        [(1, (0.0, 0.0)), (2, (100.0, 0.0))],
        [TaskObjective(7, (10.0, 0.0)), TaskObjective(8, (90.0, 0.0))],
    )
    solution = solve_qaoa(problem)
    assert solution.feasible
    assert solution.assignment == ((1, 7), (2, 8))
    assert solution.cost == pytest.approx(20.0)
    assert solution.task_for(2) == 8
    assert solution.task_for(3) is None
    assert solution.probability is not None and solution.probability > 1 / 16
    assert solution.feasible_probability is not None
    assert solution.feasible_probability > 2 / 16


def test_qaoa_depth_two_matches_brute_force_on_small_random_instances() -> None:
    rng = random.Random(5)
    for shape in ((2, 2), (2, 3), (3, 2), (3, 3)):
        for _ in range(3):
            problem = _random_problem(rng, *shape)
            assert solve_qaoa(problem).cost == pytest.approx(
                solve_brute_force(problem).cost
            )


def test_qaoa_is_deterministic_and_bounded() -> None:
    problem = _random_problem(random.Random(4), 3, 3)
    assert solve_qaoa(problem) == solve_qaoa(problem)
    big = _random_problem(random.Random(4), 3, 6)
    assert big.num_variables > MAX_QAOA_QUBITS
    with pytest.raises(ValueError):
        solve_qaoa(big)


def test_qaoa_reports_infeasible_when_nothing_decodes() -> None:
    problem = _random_problem(random.Random(4), 3, 3)
    shallow = solve_qaoa(problem, QaoaSettings(depth=0, decode_candidates=1))
    # depth 0 is uniform: the lowest-index bitstring (all zeros) is decoded.
    assert not shallow.feasible
    assert shallow.assignment == ()
    assert shallow.cost is None
    assert shallow.probability is None


def test_settings_validate() -> None:
    with pytest.raises(ValueError):
        QaoaSettings(depth=-1)
    with pytest.raises(ValueError):
        QaoaSettings(refinement_rounds=-1)
    with pytest.raises(ValueError):
        QaoaSettings(gamma_max=0.0)
    with pytest.raises(ValueError):
        SqaSettings(temperature=0.0)
    with pytest.raises(ValueError):
        SqaSettings(final_field=math.nan)
    with pytest.raises(ValueError):
        SqaSettings(initial_field=1.0, final_field=2.0)


def test_sqa_is_deterministic_for_a_fixed_seed() -> None:
    problem = _random_problem(random.Random(6), 3, 4)
    first = solve_sqa(problem, seed=12345)
    second = solve_sqa(problem, seed=12345)
    assert first == second
    assert first.evaluations == 200 * 16 * 12
    # Without an explicit seed, the seed is derived from the coefficients.
    assert solve_sqa(problem) == solve_sqa(problem, seed=derive_seed(problem))
    assert derive_seed(problem) == derive_seed(problem)
    assert derive_seed(problem, salt=1) != derive_seed(problem)


def test_sqa_matches_brute_force_on_small_random_instances() -> None:
    rng = random.Random(8)
    for shape in SHAPES:
        problem = _random_problem(rng, *shape, leads=True)
        assert solve_sqa(problem).cost == pytest.approx(solve_brute_force(problem).cost)


def test_sqa_reports_infeasible_when_no_replica_is_feasible() -> None:
    problem = _random_problem(random.Random(6), 3, 3)
    hot = solve_sqa(
        problem,
        SqaSettings(trotter_slices=1, sweeps=1, temperature=1000.0),
        seed=1,
    )
    assert hot.feasible == (problem.decode(hot.bits) is not None)


def test_sqa_is_bounded() -> None:
    labels = tuple(f"v{i}" for i in range(257))
    big = AssignmentProblem(
        agent_ids=(1,),
        agent_positions=((0.0, 0.0),),
        task_ids=tuple(range(1, 258)),
        task_positions=((0.0, 0.0),) * 257,
        costs=((0.0,) * 257,),
        penalty=1.0,
        qubo=Qubo(labels, (0.0,) * 257, ()),
    )
    with pytest.raises(ValueError):
        solve_sqa(big)


def test_solve_dispatches_by_name() -> None:
    problem = _random_problem(random.Random(7), 2, 2)
    assert solve(problem, "brute_force").solver == "brute_force"
    assert solve(problem, "qaoa").solver == "qaoa"
    assert solve(problem, "sqa", seed=3).solver == "sqa"
    with pytest.raises(ValueError):
        solve(problem, "annealer")


def test_ising_conversion_preserves_every_energy() -> None:
    problem = _random_problem(random.Random(10), 2, 3)
    model = qubo_to_ising_model(problem.qubo)
    for bits in _all_bits(problem.num_variables):
        spins = [2 * bit - 1 for bit in bits]
        energy = model.offset + sum(h * s for h, s in zip(model.h, spins))
        energy += sum(value * spins[i] * spins[j] for i, j, value in model.j)
        assert energy == pytest.approx(problem.qubo.energy(bits), abs=1e-9)


def test_exported_models_reproduce_qubo_energies() -> None:
    problem = _random_problem(random.Random(11), 2, 2)
    bqm = qubo_to_bqm(problem.qubo)
    ising = qubo_to_ising(problem.qubo)
    assert bqm["vartype"] == "BINARY" and ising["vartype"] == "SPIN"
    linear = bqm["linear"]
    quadratic = bqm["quadratic"]
    h = ising["h"]
    j = ising["J"]
    assert isinstance(linear, dict) and isinstance(quadratic, dict)
    assert isinstance(h, dict) and isinstance(j, dict)
    assert list(linear) == ["a1_t1", "a1_t2", "a2_t1", "a2_t2"]
    for bits in _all_bits(problem.num_variables):
        value = dict(zip(problem.qubo.labels, bits))
        spin = {label: 2 * bit - 1 for label, bit in value.items()}
        binary_energy = float(bqm["offset"])  # type: ignore[arg-type]
        binary_energy += sum(bias * value[label] for label, bias in linear.items())
        binary_energy += sum(
            bias * value[u] * value[v] for (u, v), bias in quadratic.items()
        )
        spin_energy = float(ising["offset"])  # type: ignore[arg-type]
        spin_energy += sum(bias * spin[label] for label, bias in h.items())
        spin_energy += sum(bias * spin[u] * spin[v] for (u, v), bias in j.items())
        assert binary_energy == pytest.approx(problem.qubo.energy(bits))
        assert spin_energy == pytest.approx(problem.qubo.energy(bits))


def test_optimiser_modules_import_no_world_or_store_state() -> None:
    root = Path(__file__).parents[1] / "src/eudis_swarm"
    prohibited = {
        "agent",
        "communication",
        "mission",
        "peer_state",
        "simulation",
        "task",
        "task_claims",
        "messaging",
        "metrics",
        "evaluation",
    }
    for name in ("quantum_opt.py", "quantum_policy.py"):
        syntax = ast.parse((root / name).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(syntax)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        plain = {
            alias.name
            for node in ast.walk(syntax)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert imported.isdisjoint(prohibited), name
        assert not any(module.startswith("eudis_swarm") for module in plain), name
        # Standard library only; no numerical or quantum SDK in the core.
        assert plain <= {"hashlib", "math", "random"}, name
