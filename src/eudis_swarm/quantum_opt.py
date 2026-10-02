"""Assignment QUBO and simulated quantum solvers for receiver-local task choice.

This module is pure: it sees only the agent positions and objectives a caller
hands it, never mission state, the communication graph, or another UAV's
stores. :mod:`eudis_swarm.simulation` builds each problem from one receiver's
own pose, its HEARD peer snapshots, and its own claimable objectives.

**Formulation.** One binary variable ``x[a, t]`` per (agent, objective) pair;
``x[a, t] = 1`` means agent ``a`` takes objective ``t``. With ``A`` agents,
``T`` objectives and ``n = min(A, T)``::

    E(x) = sum_{a,t} c[a,t] x[a,t]                       travel cost (metres)
         + P * sum_{exact side}   (sum x - 1)^2           exactly one
         + P * sum_{at-most side} sum_{pairs} x x'        at most one

When ``A <= T`` every agent takes exactly one objective and every objective at
most one agent; otherwise every objective takes exactly one agent and every
agent at most one objective. ``c[a, t]`` is the straight-line distance, the
same metric :class:`~eudis_swarm.task_utility.ReceiverLocalTaskUtility` uses
with its default weights, plus an optional per-agent lead cost (distance an
agent must still fly before it is free, e.g. to finish its current task).

**Penalty weight.** ``P = n * c_max + 1`` with ``c_max`` the largest cost.
A feasible ``x`` pays no penalty, so its energy is its assignment cost, which
is at most ``n * c_max``. An infeasible ``x`` violates at least one constraint;
every violated term contributes at least ``P`` (``(k-1)^2 >= 1`` for ``k != 1``
and ``C(k, 2) >= 1`` for ``k >= 2``) and costs are non-negative, so its energy
is at least ``P > n * c_max``. Every QUBO minimum is therefore a feasible
assignment of minimum cost, and the two optima are equal.

**Solvers.** All three are deterministic for the same problem and seed; none
reads a clock or the global random generator.

* :func:`solve_brute_force` enumerates feasible assignments (the reference),
  bounded by :data:`MAX_BRUTE_FORCE_ASSIGNMENTS`.
* :func:`solve_qaoa` simulates QAOA on a pure-Python statevector: a diagonal
  cost-phase layer and an RX mixer layer, depth ``p``; angles from a fixed
  grid search plus coordinate refinement on the expected energy; decoding
  evaluates the most probable bitstrings classically and keeps the best
  feasible one. Bounded by :data:`MAX_QAOA_QUBITS`.
* :func:`solve_sqa` runs simulated quantum annealing: path-integral Monte
  Carlo of the transverse-field Ising model with Suzuki-Trotter replicas and
  a linearly annealed transverse field, driven by ``random.Random(seed)``.
  Bounded by :data:`MAX_SQA_QUBITS`.

This is classical simulation of quantum algorithms. Nothing here claims a
quantum speedup; at these sizes brute force is faster than either simulator.

**Hardware export.** :func:`qubo_to_bqm` and :func:`qubo_to_ising` return
plain mappings with string variable labels, in the layout annealing and
gate-model SDKs accept (see ``docs/quantum_optimisation.md``). No such SDK is
a dependency of this package.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import permutations
from typing import TypeAlias

from .task_utility import TaskObjective
from .validation import validate_positive_integer

Position: TypeAlias = tuple[float, float]

#: Largest problem the statevector simulator accepts (2**16 amplitudes).
MAX_QAOA_QUBITS = 16
#: Largest problem simulated quantum annealing accepts.
MAX_SQA_QUBITS = 256
#: Largest number of feasible assignments brute force will enumerate.
MAX_BRUTE_FORCE_ASSIGNMENTS = 100_000
#: Largest QUBO :func:`qubo_brute_force_minimum` will enumerate (2**20 states).
MAX_QUBO_ENUMERATION_QUBITS = 20

SOLVER_NAMES = ("brute_force", "qaoa", "sqa")


@dataclass(frozen=True, slots=True)
class Qubo:
    """Energy ``offset + sum l_i x_i + sum_{i<j} q_ij x_i x_j`` over bits."""

    labels: tuple[str, ...]
    linear: tuple[float, ...]
    #: ``(i, j, q_ij)`` with ``i < j``, sorted, no zero couplings.
    quadratic: tuple[tuple[int, int, float], ...]
    offset: float = 0.0

    def __post_init__(self) -> None:
        if len(self.labels) != len(self.linear):
            raise ValueError("every QUBO variable needs one label")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("QUBO labels must be unique")
        previous: tuple[int, int] | None = None
        for i, j, _ in self.quadratic:
            if not 0 <= i < j < len(self.linear):
                raise ValueError("QUBO couplings must satisfy 0 <= i < j < n")
            if previous is not None and (i, j) <= previous:
                raise ValueError("QUBO couplings must be sorted and unique")
            previous = (i, j)

    @property
    def num_variables(self) -> int:
        return len(self.linear)

    def energy(self, bits: Sequence[int]) -> float:
        """Classically evaluate one bit assignment."""

        if len(bits) != self.num_variables:
            raise ValueError("bit vector length must match the QUBO")
        total = self.offset
        for value, bias in zip(bits, self.linear):
            if value:
                total += bias
        for i, j, coupling in self.quadratic:
            if bits[i] and bits[j]:
                total += coupling
        return total


@dataclass(frozen=True, slots=True)
class AssignmentProblem:
    """A bounded agent-to-objective assignment and its QUBO encoding."""

    agent_ids: tuple[int, ...]
    agent_positions: tuple[Position, ...]
    task_ids: tuple[int, ...]
    task_positions: tuple[Position, ...]
    #: ``costs[a][t]``: travel distance from agent ``a`` to objective ``t``.
    costs: tuple[tuple[float, ...], ...]
    penalty: float
    qubo: Qubo

    @property
    def num_variables(self) -> int:
        return self.qubo.num_variables

    @property
    def assignments_required(self) -> int:
        return min(len(self.agent_ids), len(self.task_ids))

    def variable(self, agent_index: int, task_index: int) -> int:
        return agent_index * len(self.task_ids) + task_index

    def decode(self, bits: Sequence[int]) -> tuple[tuple[int, int], ...] | None:
        """Return ``(agent_id, task_id)`` pairs, or ``None`` if infeasible."""

        if len(bits) != self.num_variables:
            raise ValueError("bit vector length must match the problem")
        agent_count = len(self.agent_ids)
        task_count = len(self.task_ids)
        pairs: list[tuple[int, int]] = []
        task_load = [0] * task_count
        for agent_index in range(agent_count):
            chosen = [
                task_index
                for task_index in range(task_count)
                if bits[self.variable(agent_index, task_index)]
            ]
            if len(chosen) > 1 or (agent_count <= task_count and not chosen):
                return None
            for task_index in chosen:
                task_load[task_index] += 1
                pairs.append((self.agent_ids[agent_index], self.task_ids[task_index]))
        if any(load > 1 for load in task_load):
            return None
        if agent_count > task_count and any(load != 1 for load in task_load):
            return None
        return tuple(pairs)

    def assignment_cost(self, pairs: Sequence[tuple[int, int]]) -> float:
        agent_index = {agent_id: i for i, agent_id in enumerate(self.agent_ids)}
        task_index = {task_id: i for i, task_id in enumerate(self.task_ids)}
        return sum(
            self.costs[agent_index[agent_id]][task_index[task_id]]
            for agent_id, task_id in pairs
        )

    def bits_for(self, pairs: Sequence[tuple[int, int]]) -> tuple[int, ...]:
        agent_index = {agent_id: i for i, agent_id in enumerate(self.agent_ids)}
        task_index = {task_id: i for i, task_id in enumerate(self.task_ids)}
        bits = [0] * self.num_variables
        for agent_id, task_id in pairs:
            bits[self.variable(agent_index[agent_id], task_index[task_id])] = 1
        return tuple(bits)


def assignment_qubo(
    agents: Sequence[tuple[int, Position]],
    objectives: Sequence[TaskObjective],
    *,
    lead_costs: Sequence[float] | None = None,
) -> AssignmentProblem:
    """Build the assignment QUBO for the given agents and objectives.

    Variable order is agent-major in the order supplied, so callers control
    determinism by passing stably ordered inputs. ``lead_costs[a]`` is added
    to every cost of agent ``a`` (default zero).
    """

    if not agents or not objectives:
        raise ValueError("an assignment needs at least one agent and one objective")
    agent_ids = tuple(validate_positive_integer(a, name="agent_id") for a, _ in agents)
    if len(set(agent_ids)) != len(agent_ids):
        raise ValueError("agent IDs must be unique")
    if not all(isinstance(item, TaskObjective) for item in objectives):
        raise TypeError("objectives must contain only TaskObjective values")
    task_ids = tuple(objective.task_id for objective in objectives)
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("objective task IDs must be unique")
    agent_positions = tuple(
        (float(position[0]), float(position[1])) for _, position in agents
    )
    if not all(math.isfinite(v) for position in agent_positions for v in position):
        raise ValueError("agent positions must be finite")
    leads = (0.0,) * len(agent_ids) if lead_costs is None else tuple(lead_costs)
    if len(leads) != len(agent_ids) or not all(
        math.isfinite(lead) and lead >= 0.0 for lead in leads
    ):
        raise ValueError("lead_costs must be one finite non-negative value per agent")
    task_positions = tuple(objective.position for objective in objectives)
    costs = tuple(
        tuple(
            lead + math.hypot(task[0] - agent[0], task[1] - agent[1])
            for task in task_positions
        )
        for agent, lead in zip(agent_positions, leads)
    )
    agent_count = len(agent_ids)
    task_count = len(task_ids)
    required = min(agent_count, task_count)
    penalty = required * max(max(row) for row in costs) + 1.0

    def index(agent_index: int, task_index: int) -> int:
        return agent_index * task_count + task_index

    linear = [costs[a][t] for a in range(agent_count) for t in range(task_count)]
    quadratic: dict[tuple[int, int], float] = {}
    offset = 0.0

    def add_pairs(group: Sequence[int], weight: float) -> None:
        for position, i in enumerate(group):
            for j in group[position + 1 :]:
                key = (min(i, j), max(i, j))
                quadratic[key] = quadratic.get(key, 0.0) + weight

    agent_groups = [
        [index(a, t) for t in range(task_count)] for a in range(agent_count)
    ]
    task_groups = [[index(a, t) for a in range(agent_count)] for t in range(task_count)]
    exact_groups, at_most_groups = (
        (agent_groups, task_groups)
        if agent_count <= task_count
        else (task_groups, agent_groups)
    )
    for group in exact_groups:
        # P * (sum x - 1)^2 = P * (1 - sum x + 2 sum_{pairs} x x') for bits.
        offset += penalty
        for i in group:
            linear[i] -= penalty
        add_pairs(group, 2.0 * penalty)
    for group in at_most_groups:
        add_pairs(group, penalty)

    labels = tuple(f"a{a}_t{t}" for a in agent_ids for t in task_ids)
    qubo = Qubo(
        labels=labels,
        linear=tuple(linear),
        quadratic=tuple(
            (i, j, value) for (i, j), value in sorted(quadratic.items()) if value != 0.0
        ),
        offset=offset,
    )
    return AssignmentProblem(
        agent_ids=agent_ids,
        agent_positions=agent_positions,
        task_ids=task_ids,
        task_positions=task_positions,
        costs=costs,
        penalty=penalty,
        qubo=qubo,
    )


@dataclass(frozen=True, slots=True)
class AssignmentSolution:
    """One solver's answer. ``assignment`` is empty when nothing feasible."""

    solver: str
    assignment: tuple[tuple[int, int], ...]
    feasible: bool
    cost: float | None
    bits: tuple[int, ...]
    #: Objective evaluations (QAOA expectation values, SQA spin-flip attempts,
    #: or enumerated assignments). Deterministic; a size measure, not a time.
    evaluations: int
    #: QAOA only: final-state probability of the returned bitstring.
    probability: float | None = None
    #: QAOA only: final-state probability mass on feasible bitstrings.
    feasible_probability: float | None = None

    def task_for(self, agent_id: int) -> int | None:
        for assigned_agent_id, task_id in self.assignment:
            if assigned_agent_id == agent_id:
                return task_id
        return None


def _solution(
    problem: AssignmentProblem,
    solver: str,
    bits: tuple[int, ...] | None,
    evaluations: int,
    *,
    probability: float | None = None,
    feasible_probability: float | None = None,
) -> AssignmentSolution:
    pairs = None if bits is None else problem.decode(bits)
    if pairs is None:
        return AssignmentSolution(
            solver=solver,
            assignment=(),
            feasible=False,
            cost=None,
            bits=bits if bits is not None else (0,) * problem.num_variables,
            evaluations=evaluations,
            probability=probability,
            feasible_probability=feasible_probability,
        )
    assert bits is not None
    return AssignmentSolution(
        solver=solver,
        assignment=pairs,
        feasible=True,
        cost=problem.assignment_cost(pairs),
        bits=bits,
        evaluations=evaluations,
        probability=probability,
        feasible_probability=feasible_probability,
    )


def feasible_assignment_count(problem: AssignmentProblem) -> int:
    agent_count = len(problem.agent_ids)
    task_count = len(problem.task_ids)
    return math.perm(max(agent_count, task_count), min(agent_count, task_count))


def solve_brute_force(problem: AssignmentProblem) -> AssignmentSolution:
    """Exact reference: enumerate every feasible assignment."""

    count = feasible_assignment_count(problem)
    if count > MAX_BRUTE_FORCE_ASSIGNMENTS:
        raise ValueError(
            f"brute force is limited to {MAX_BRUTE_FORCE_ASSIGNMENTS} assignments"
        )
    agent_count = len(problem.agent_ids)
    task_count = len(problem.task_ids)
    best_pairs: tuple[tuple[int, int], ...] | None = None
    best_cost = math.inf
    if agent_count <= task_count:
        for chosen in permutations(range(task_count), agent_count):
            cost = sum(problem.costs[a][t] for a, t in enumerate(chosen))
            if cost < best_cost:
                best_cost = cost
                best_pairs = tuple(
                    (problem.agent_ids[a], problem.task_ids[t])
                    for a, t in enumerate(chosen)
                )
    else:
        for chosen in permutations(range(agent_count), task_count):
            cost = sum(problem.costs[a][t] for t, a in enumerate(chosen))
            if cost < best_cost:
                best_cost = cost
                best_pairs = tuple(
                    sorted(
                        (problem.agent_ids[a], problem.task_ids[t])
                        for t, a in enumerate(chosen)
                    )
                )
    assert best_pairs is not None
    return _solution(problem, "brute_force", problem.bits_for(best_pairs), count)


def qubo_brute_force_minimum(qubo: Qubo) -> tuple[float, tuple[int, ...]]:
    """Enumerate all ``2**n`` bit vectors; lowest energy, lowest index on ties."""

    n = qubo.num_variables
    if n > MAX_QUBO_ENUMERATION_QUBITS:
        raise ValueError(
            f"QUBO enumeration is limited to {MAX_QUBO_ENUMERATION_QUBITS} variables"
        )
    energies = _diagonal_energies(qubo)
    best_index = min(range(len(energies)), key=lambda k: (energies[k], k))
    return energies[best_index], _bits_of(best_index, n)


def _bits_of(index: int, n: int) -> tuple[int, ...]:
    return tuple((index >> bit) & 1 for bit in range(n))


def _diagonal_energies(qubo: Qubo) -> list[float]:
    """Energy of every basis state; bit ``i`` of the index is variable ``i``."""

    energies = [qubo.offset]
    for i, bias in enumerate(qubo.linear):
        couplings = [(j, value) for j, k, value in qubo.quadratic if k == i]
        extended: list[float] = []
        for index, base in enumerate(energies):
            delta = bias + sum(value for j, value in couplings if (index >> j) & 1)
            extended.append(base + delta)
        energies = energies + extended
    return energies


# --- QAOA on a statevector ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class QaoaSettings:
    """Fixed, documented angle-search budget; no randomness involved."""

    #: Layers. Depth 2 is the default: on 60 random instances of up to 12
    #: qubits it decoded the exact optimum every time, depth 1 in 33 of 60.
    depth: int = 2
    #: Grid points per angle when a layer is introduced.
    grid_points: int = 8
    #: Coordinate-refinement passes; the step halves after each pass.
    refinement_rounds: int = 4
    #: Most probable bitstrings evaluated classically during decoding.
    decode_candidates: int = 8
    #: Upper end of the gamma grid for energies normalised to [0, 1].
    gamma_max: float = 4.0 * math.pi

    def __post_init__(self) -> None:
        if not isinstance(self.depth, int) or self.depth < 0:
            raise ValueError("QAOA depth must be a non-negative integer")
        validate_positive_integer(self.grid_points, name="grid_points")
        if not isinstance(self.refinement_rounds, int) or self.refinement_rounds < 0:
            raise ValueError("refinement_rounds must be a non-negative integer")
        validate_positive_integer(self.decode_candidates, name="decode_candidates")
        if not self.gamma_max > 0.0:
            raise ValueError("gamma_max must be positive")


def qaoa_statevector(
    energies: Sequence[float],
    gammas: Sequence[float],
    betas: Sequence[float],
) -> tuple[list[float], list[float]]:
    """Return ``(real, imag)`` amplitudes after ``len(gammas)`` QAOA layers.

    Starts in ``|+>^n``; each layer applies ``exp(-i gamma H_C)`` (diagonal in
    the computational basis) then ``exp(-i beta X)`` on every qubit.
    """

    size = len(energies)
    n = size.bit_length() - 1
    if size < 1 or 1 << n != size:
        raise ValueError("energies must have a power-of-two length")
    if len(gammas) != len(betas):
        raise ValueError("gammas and betas must have equal length")
    amplitude = 1.0 / math.sqrt(size)
    real = [amplitude] * size
    imag = [0.0] * size
    for gamma, beta in zip(gammas, betas):
        for k in range(size):
            phase = gamma * energies[k]
            c = math.cos(phase)
            s = math.sin(phase)
            re = real[k]
            im = imag[k]
            real[k] = re * c + im * s
            imag[k] = im * c - re * s
        c = math.cos(beta)
        s = math.sin(beta)
        for qubit in range(n):
            mask = 1 << qubit
            for block in range(0, size, mask << 1):
                for k0 in range(block, block + mask):
                    k1 = k0 | mask
                    a_re = real[k0]
                    a_im = imag[k0]
                    b_re = real[k1]
                    b_im = imag[k1]
                    real[k0] = c * a_re + s * b_im
                    imag[k0] = c * a_im - s * b_re
                    real[k1] = c * b_re + s * a_im
                    imag[k1] = c * b_im - s * a_re
    return real, imag


def _expected_energy(
    energies: Sequence[float], gammas: Sequence[float], betas: Sequence[float]
) -> float:
    real, imag = qaoa_statevector(energies, gammas, betas)
    return sum(
        energy * (re * re + im * im) for energy, re, im in zip(energies, real, imag)
    )


def solve_qaoa(
    problem: AssignmentProblem,
    settings: QaoaSettings | None = None,
) -> AssignmentSolution:
    """Simulate depth-``p`` QAOA and decode the best feasible likely bitstring."""

    settings = settings or QaoaSettings()
    n = problem.num_variables
    if n > MAX_QAOA_QUBITS:
        raise ValueError(f"QAOA simulation is limited to {MAX_QAOA_QUBITS} qubits")
    raw = _diagonal_energies(problem.qubo)
    low = min(raw)
    span = max(raw) - low
    # Normalising the diagonal to [0, 1] makes one angle grid fit every
    # instance; it rescales H_C and changes no ordering.
    energies = [(value - low) / span if span > 0 else 0.0 for value in raw]
    evaluations = 0

    def evaluate(gammas: Sequence[float], betas: Sequence[float]) -> float:
        nonlocal evaluations
        evaluations += 1
        return _expected_energy(energies, gammas, betas)

    gammas: list[float] = []
    betas: list[float] = []
    points = settings.grid_points
    gamma_grid = [settings.gamma_max * (k + 1) / points for k in range(points)]
    beta_grid = [(math.pi / 2) * (k + 1) / (points + 1) for k in range(points)]
    for _ in range(settings.depth):
        best: tuple[float, float, float] | None = None
        for gamma in gamma_grid:
            for beta in beta_grid:
                value = evaluate([*gammas, gamma], [*betas, beta])
                if best is None or value < best[0]:
                    best = (value, gamma, beta)
        assert best is not None
        gammas.append(best[1])
        betas.append(best[2])
        current = best[0]
        gamma_step = settings.gamma_max / points / 2
        beta_step = (math.pi / 2) / (points + 1) / 2
        for _ in range(settings.refinement_rounds):
            for layer in range(len(gammas)):
                for angles, step in ((gammas, gamma_step), (betas, beta_step)):
                    for direction in (1.0, -1.0):
                        trial = list(angles)
                        trial[layer] += direction * step
                        candidate = (
                            evaluate(trial, betas)
                            if angles is gammas
                            else evaluate(gammas, trial)
                        )
                        if candidate < current:
                            current = candidate
                            angles[layer] = trial[layer]
                            break
            gamma_step /= 2
            beta_step /= 2

    real, imag = qaoa_statevector(energies, gammas, betas)
    probabilities = [re * re + im * im for re, im in zip(real, imag)]
    feasible_mass = 0.0
    for index, probability in enumerate(probabilities):
        if problem.decode(_bits_of(index, n)) is not None:
            feasible_mass += probability
    ranked = sorted(range(len(probabilities)), key=lambda k: (-probabilities[k], k))
    best_bits: tuple[int, ...] | None = None
    best_key: tuple[float, float, int] | None = None
    for index in ranked[: settings.decode_candidates]:
        bits = _bits_of(index, n)
        pairs = problem.decode(bits)
        if pairs is None:
            continue
        key = (problem.assignment_cost(pairs), -probabilities[index], index)
        if best_key is None or key < best_key:
            best_key = key
            best_bits = bits
    chosen_probability = None
    if best_bits is not None:
        chosen_index = sum(bit << i for i, bit in enumerate(best_bits))
        chosen_probability = probabilities[chosen_index]
    return _solution(
        problem,
        "qaoa",
        best_bits,
        evaluations,
        probability=chosen_probability,
        feasible_probability=feasible_mass,
    )


# --- simulated quantum annealing ---------------------------------------------


@dataclass(frozen=True, slots=True)
class SqaSettings:
    """Path-integral Monte Carlo schedule in units of the largest coefficient."""

    trotter_slices: int = 16
    sweeps: int = 200
    initial_field: float = 3.0
    final_field: float = 0.01
    temperature: float = 0.05

    def __post_init__(self) -> None:
        validate_positive_integer(self.trotter_slices, name="trotter_slices")
        validate_positive_integer(self.sweeps, name="sweeps")
        for name in ("initial_field", "final_field", "temperature"):
            value = getattr(self, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value)):
                raise ValueError(f"{name} must be finite")
            if value <= 0.0:
                raise ValueError(f"{name} must be positive")
        if self.final_field > self.initial_field:
            raise ValueError("final_field must not exceed initial_field")


@dataclass(frozen=True, slots=True)
class IsingModel:
    """``offset + sum h_i s_i + sum_{i<j} J_ij s_i s_j`` over ``s in {-1,+1}``."""

    labels: tuple[str, ...]
    h: tuple[float, ...]
    j: tuple[tuple[int, int, float], ...]
    offset: float


def qubo_to_ising_model(qubo: Qubo) -> IsingModel:
    """Substitute ``x = (1 + s) / 2``; energies agree on every state."""

    h = [bias / 2.0 for bias in qubo.linear]
    offset = qubo.offset + sum(qubo.linear) / 2.0
    couplings: list[tuple[int, int, float]] = []
    for i, k, value in qubo.quadratic:
        h[i] += value / 4.0
        h[k] += value / 4.0
        offset += value / 4.0
        couplings.append((i, k, value / 4.0))
    return IsingModel(qubo.labels, tuple(h), tuple(couplings), offset)


def derive_seed(problem: AssignmentProblem, salt: int = 0) -> int:
    """A seed fixed by the problem's coefficients, independent of hash salting."""

    material = repr(
        (problem.qubo.linear, problem.qubo.quadratic, problem.qubo.offset, salt)
    )
    return int.from_bytes(hashlib.sha256(material.encode()).digest()[:8], "big")


def solve_sqa(
    problem: AssignmentProblem,
    settings: SqaSettings | None = None,
    *,
    seed: int | None = None,
) -> AssignmentSolution:
    """Simulated quantum annealing; returns the best feasible replica seen."""

    settings = settings or SqaSettings()
    n = problem.num_variables
    if n > MAX_SQA_QUBITS:
        raise ValueError(f"SQA is limited to {MAX_SQA_QUBITS} qubits")
    rng = random.Random(derive_seed(problem) if seed is None else seed)
    ising = qubo_to_ising_model(problem.qubo)
    scale = max(
        [abs(value) for value in ising.h] + [abs(value) for _, _, value in ising.j]
    )
    scale = scale if scale > 0 else 1.0
    h = [value / scale for value in ising.h]
    neighbours: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i, k, value in ising.j:
        neighbours[i].append((k, value / scale))
        neighbours[k].append((i, value / scale))

    slices = settings.trotter_slices
    beta = 1.0 / settings.temperature
    spins = [[rng.choice((-1, 1)) for _ in range(n)] for _ in range(slices)]
    best_bits: tuple[int, ...] | None = None
    best_cost = math.inf
    evaluations = 0
    for sweep in range(settings.sweeps):
        fraction = sweep / max(1, settings.sweeps - 1)
        field = settings.initial_field + fraction * (
            settings.final_field - settings.initial_field
        )
        # Inter-replica coupling from the Suzuki-Trotter mapping:
        # K = (1/2) ln coth(beta * Gamma / P).
        coupling = 0.5 * math.log(1.0 / math.tanh(beta * field / slices))
        for replica in range(slices):
            state = spins[replica]
            before = spins[replica - 1]
            after = spins[(replica + 1) % slices]
            for i in range(n):
                local = h[i] + sum(value * state[k] for k, value in neighbours[i])
                delta_classical = -2.0 * state[i] * local
                delta = (beta / slices) * delta_classical + 2.0 * coupling * state[
                    i
                ] * (before[i] + after[i])
                evaluations += 1
                if delta <= 0.0 or rng.random() < math.exp(-delta):
                    state[i] = -state[i]
        for replica in range(slices):
            bits = tuple(1 if value > 0 else 0 for value in spins[replica])
            pairs = problem.decode(bits)
            if pairs is None:
                continue
            cost = problem.assignment_cost(pairs)
            if cost < best_cost:
                best_cost = cost
                best_bits = bits
    return _solution(problem, "sqa", best_bits, evaluations)


def solve(
    problem: AssignmentProblem,
    solver: str,
    *,
    qaoa: QaoaSettings | None = None,
    sqa: SqaSettings | None = None,
    seed: int | None = None,
) -> AssignmentSolution:
    """Dispatch by solver name: ``brute_force``, ``qaoa`` or ``sqa``."""

    if solver == "brute_force":
        return solve_brute_force(problem)
    if solver == "qaoa":
        return solve_qaoa(problem, qaoa)
    if solver == "sqa":
        return solve_sqa(problem, sqa, seed=seed)
    raise ValueError(f"unknown solver {solver!r}; expected one of {SOLVER_NAMES}")


# --- export ------------------------------------------------------------------


def qubo_to_bqm(qubo: Qubo) -> dict[str, object]:
    """Plain BINARY quadratic model keyed by variable label.

    ``linear`` maps label -> bias, ``quadratic`` maps ``(label, label)`` ->
    coupling, and ``offset``/``vartype`` complete the model, matching the
    constructor arguments of common annealing SDK model classes.
    """

    return {
        "vartype": "BINARY",
        "linear": {label: bias for label, bias in zip(qubo.labels, qubo.linear)},
        "quadratic": {
            (qubo.labels[i], qubo.labels[j]): value for i, j, value in qubo.quadratic
        },
        "offset": qubo.offset,
    }


def qubo_to_ising(qubo: Qubo) -> dict[str, object]:
    """Plain SPIN model (``h``, ``J``, ``offset``) keyed by variable label."""

    model = qubo_to_ising_model(qubo)
    return {
        "vartype": "SPIN",
        "h": {label: bias for label, bias in zip(model.labels, model.h)},
        "J": {(model.labels[i], model.labels[k]): value for i, k, value in model.j},
        "offset": model.offset,
    }


__all__ = [
    "MAX_BRUTE_FORCE_ASSIGNMENTS",
    "MAX_QAOA_QUBITS",
    "MAX_QUBO_ENUMERATION_QUBITS",
    "MAX_SQA_QUBITS",
    "SOLVER_NAMES",
    "AssignmentProblem",
    "AssignmentSolution",
    "IsingModel",
    "QaoaSettings",
    "Qubo",
    "SqaSettings",
    "assignment_qubo",
    "derive_seed",
    "feasible_assignment_count",
    "qaoa_statevector",
    "qubo_brute_force_minimum",
    "qubo_to_bqm",
    "qubo_to_ising",
    "qubo_to_ising_model",
    "solve",
    "solve_brute_force",
    "solve_qaoa",
    "solve_sqa",
]
