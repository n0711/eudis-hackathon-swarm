# Quantum-simulated task optimisation

## The requirement

EUDIS Defence Hackathon 2026, Challenge 2:

> Develop a multi-agent drone or robot swarm that can collaborate to complete
> a mission (e.g. search and rescue, mapping or target tracking) with minimal
> human control and include optimisation using quantum simulation.

The Cyprus edition adds that the technical challenge spans "resilient
communication between agents, distributed task allocation, coordinated motion
and deconfliction, shared situational awareness, and mission behaviours", and
that the best solutions treat the swarm as vehicle-agnostic.

This document describes the one component of the project that answers the
"optimisation using quantum simulation" clause: a receiver-local allocation
policy that solves a small task-assignment problem with simulated quantum
algorithms, and a paired study that measures what it changes.

## What was built

| Piece | Where |
| --- | --- |
| Assignment QUBO, three solvers (QAOA with three ansatzes), hardware-neutral export | [`quantum_opt.py`](../src/eudis_swarm/quantum_opt.py) |
| Receiver-local formulation and preference ordering | [`quantum_policy.py`](../src/eudis_swarm/quantum_policy.py) |
| Simulator wiring: `--allocation-policy quantum`, `--quantum-solver qaoa\|sqa`, `--qaoa-mixer`, `--qaoa-depth` | [`simulation.py`](../src/eudis_swarm/simulation.py), [`config.py`](../src/eudis_swarm/config.py) |
| Paired study CLI (`eudis-swarm-quantum-study`) | [`quantum_study.py`](../src/eudis_swarm/quantum_study.py) |
| Raw data | [`data/quantum_*_2026-10-15.csv`](data/) |

Everything is standard-library Python. No quantum SDK, numerical library or
other runtime dependency was added to the core package.

```bash
eudis-swarm --allocation-policy quantum --quantum-solver qaoa
eudis-swarm --allocation-policy quantum --quantum-solver sqa
```

## Formulation

One binary variable `x[a,t]` per (agent, objective) pair; `x[a,t] = 1` means
agent `a` takes objective `t`. With `A` agents, `T` objectives and
`n = min(A, T)`:

```
E(x) = Σ c[a,t]·x[a,t]                         travel cost, metres
     + P · Σ_{exact side}   (Σ x − 1)²          exactly one
     + P · Σ_{at-most side} Σ_{pairs} x·x'      at most one
```

If `A ≤ T`, every agent takes exactly one objective and every objective at
most one agent; otherwise every objective takes exactly one agent and every
agent at most one objective.

`c[a,t]` is the straight-line distance from the agent's position to the
objective — the metric the default distance utility already uses — plus an
optional per-agent lead cost (below).

**Penalty weight.** `P = n · c_max + 1`, with `c_max` the largest cost in the
instance. A feasible `x` pays no penalty, so its energy is its assignment cost,
at most `n · c_max`. An infeasible `x` violates at least one constraint, each
violated term contributes at least `P`, and costs are non-negative, so its
energy is at least `P > n · c_max`. Every QUBO minimum is therefore a
minimum-cost feasible assignment. `tests/test_quantum_opt.py` checks this by
exhaustive enumeration on nine instance shapes, with and without lead costs.

## Solvers

All three are deterministic for the same problem and seed; none reads a clock
or Python's global random generator.

| Solver | Method | Bound |
| --- | --- | --- |
| `brute_force` | enumerate feasible assignments; the reference | ≤ 100 000 assignments |
| `qaoa` | QAOA simulated on a pure-Python statevector, depth `p` = 4 by default, with a choice of three ansatzes (below). Angles: a 12×12 grid per layer (γ up to 8π on energies normalised to [0, 1], β up to π), then four rounds of coordinate refinement on the expected energy, step halved each round. Decode: draw 64 bitstrings from the final state with a seeded generator, as hardware returns samples, and keep the best feasible one. | textbook ansatz ≤ 16 qubits; constrained ansatzes ≤ 5 040 reachable states |
| `sqa` | simulated quantum annealing: path-integral Monte Carlo of the transverse-field Ising model, 16 Suzuki–Trotter replicas, 200 sweeps, transverse field annealed linearly from 3.0 to 0.01 at temperature 0.05 (units of the largest Ising coefficient). `random.Random(seed)`, seed derived from a SHA-256 of the coefficients. Returns the best feasible replica seen. | ≤ 256 qubits |

### QAOA ansatzes

| `--qaoa-mixer` | Initial state | Mixer | Cost Hamiltonian | States simulated |
| --- | --- | --- | --- | --- |
| `assignment` (default) | uniform superposition of the feasible assignments | **swap** terms (two UAVs exchange objectives) and **move** terms (one UAV hops to an objective no other UAV holds) | travel cost only; no penalty | feasible assignments only (24 for 3 × 4) |
| `xy` | one W state per UAV register | ring of XY (hopping) gates inside each register | travel cost + `(c_max + 1)` per objective collision | one-hot product space (64 for 3 × 4) |
| `x` | `\|+⟩^n` | RX on every qubit | the full QUBO above | all `2^n` bitstrings (4 096 for 3 × 4) |

The `assignment` ansatz follows the constraint-preserving
("alternating operator") construction of Hadfield et al., *Algorithms* 12(2),
2019: every mixer term maps feasible assignments to feasible assignments, so
the state never leaves them and every shot decodes. Each term is a sum of
disjoint two-level couplings, so its exponential is an exact set of 2 × 2
rotations; the constrained ansatzes are therefore simulated exactly in the
states they can reach, which is why they are much faster than the textbook
form. `tests/test_quantum_opt.py` checks the XY subspace simulation against an
independent full `2^n` statevector with explicit gates, and checks that every
assignment-ansatz layer is a set of disjoint pairs that preserves the norm.

**Smaller penalty inside the one-hot subspace.** For `xy`, the
exactly-one constraints are enforced by the circuit, so only collisions need a
penalty, and `c_max + 1` provably suffices: in a colliding state, moving one
colliding UAV to an unused objective adds at most `c_max` of cost and removes
at least one penalty, so the energy strictly drops; repeating reaches a
feasible assignment. The test checks this exhaustively on nine shapes. The
full QUBO keeps `n·c_max + 1` because a state there may also violate
exactly-one constraints.

**How the defaults were chosen.** On 70 random instances generated for this
purpose (10 per shape, seed 77, not taken from the study), with 64-shot
decoding and the final angle grid: mean single-shot probability of the
optimum, and in brackets how many of 10 instances returned the optimum.

| shape | textbook `x`, p = 2 | `xy`, p = 4 | `assignment`, p = 4 | random feasible assignment |
| --- | ---: | ---: | ---: | ---: |
| 3 × 3 | 0.022 (9) | 0.252 (10) | 0.968 (10) | 0.167 |
| 3 × 4 | 0.000 (0) | 0.086 (9) | 0.556 (10) | 0.042 |
| 4 × 4 | – (65 536 states) | 0.023 (6) | 0.397 (10) | 0.042 |

The `xy` ansatz barely beats uniform sampling of feasible assignments from 9
qubits up, and falls below it at 4 × 4, because much of its amplitude stays
on collisions; deeper `xy` circuits improve slowly (3 × 3: 0.26 at depth 12
with the earlier grid). The `assignment` ansatz returned the optimum on all
70 instances (all seven shapes, 2 × 2 to 4 × 4); it is the default, at depth
4. The γ range was then widened from 4π to
8π and the grid from 8 to 12 points **after** three 2 × 4 problems from the
reported study (seeds 1–20) were seen to fail: their optimum was nearly
degenerate with the runner-up and the narrower grid settled on the runner-up.
That change was checked on separate random instances (mean single-shot
probability 0.59 → 0.67, none below 0.05), but it was prompted by the reported
data, and that is stated here.

**Why decoding changed.** The 2 October version decoded by classically
evaluating the eight most probable bitstrings of the final state. No hardware
can read those probabilities; it returns samples. Re-checked with 64 samples
instead and otherwise the 2 October settings (γ up to 4π, 8-point grid), the
textbook ansatz at depth 2 found the optimum on only 1 of 10 random 3 × 3
instances (9 of 10 with the final grid, at 0.022 probability per shot), so the earlier "always optimal" figure (below) owed
more to that classical post-selection than to the quantum state.

## Why the optimiser stays receiver-local

The project's load-bearing rule is that a UAV acts only on evidence it has
received. A central planner that sees every UAV and every task would violate
that and would fail exactly when the network partitions — the case the project
exists to handle. So each UAV builds its own small problem:

- **agents**: itself, plus up to `quantum_max_agents − 1` peers (default 2)
  that are `HEARD` in its own `PeerStateStore` — the same evidence the
  connectivity policy uses — nearest first. An idle peer enters at the
  position in its last delivered heartbeat. A peer whose heartbeat reported a
  current task enters at that task's briefed catalogue position, with the
  distance it still had to fly as a lead cost; if this UAV does not know that
  objective, the peer is left out;
- **objectives**: up to `quantum_max_objectives` (default 4) of the objectives
  this UAV may itself claim, chosen round-robin — this UAV's nearest, then
  each included peer's nearest not yet chosen, and so on — so that no peer is
  forced onto objectives that only suit this UAV.

Defaults therefore give at most 12 qubits (24 feasible assignments). The
default was 3 objectives (9 qubits) in the 2 October version, when the
textbook simulator made 12 qubits too slow; 4 was chosen on tuning seeds
21–40: mean mission-time change against distance −0.26 s with 4 objectives
(38 seeds faster, 26 slower; sign test p ≈ 0.17) and −0.02 s with 3. Adding
an onward-leg term to the cost (distance from each objective to its nearest
neighbour, either preferring or avoiding clustered objectives) moved the mean
by less than 0.07 s on the same seeds and was not adopted.

A problem with one agent or one objective has the UAV's own nearest objective
as its exact optimum and is not solved.

The solved plan **reorders this UAV's own preference list**: the objective
assigned to itself first, then the rest in utility order, with objectives the
plan gives to a peer moved to the end. The UAV then follows the normal path —
create a claim, gossip, reconcile, bind and execute only while `owns_task()`.
The optimiser never reads `Task.assigned_agent`, the `CommunicationGraph`,
another UAV's `Agent` object, or another UAV's private stores;
`quantum_opt.py` and `quantum_policy.py` import none of those modules, which a
test checks.

**When two local plans disagree.** Two UAVs with different evidence can
produce different plans and both prefer the same objective. Nothing special
happens: both create claims, the claims flood across the connected component,
and reconciliation keeps the lowest validated owner ID, exactly as for the
distance policy. During a partition the plans cannot be compared at all, and
overlapping claims are resolved when the components merge. The study counts
how often this costs contested time.

Tests in `tests/test_quantum_policy.py` check the boundary with negative
assertions: on a chain topology no formulation contains a UAV two hops away; a
UAV that is never heard is never formulated; a UAV that is comms-isolated
leaves every other formulation once its heartbeat ages out; and with every link
blocked the quantum policy produces exactly the distance policy's trajectory.

## Measured results (15 October 2026)

### Reproduce

```bash
eudis-swarm-quantum-study --seeds 20 --seed-base 1 \
  --output docs/data/quantum_runs_2026-10-15.csv \
  --solves-output docs/data/quantum_solves_2026-10-15.csv \
  --summary-output docs/data/quantum_summary_2026-10-15.csv \
  --ansatz-output docs/data/quantum_ansatz_2026-10-15.csv \
  --ansatz-summary-output docs/data/quantum_ansatz_summary_2026-10-15.csv \
  --timing-output docs/data/quantum_timing_2026-10-15.csv
```

About 30 minutes on one core, most of it timing the textbook ansatz. Every
file except the timing file is byte-identical between runs; SHA-256 of the
committed copies:

| File | SHA-256 |
| --- | --- |
| `quantum_runs_2026-10-15.csv` | `d8a591c22c3b2dcd6051b6bd23746258d5bc26695cbb555cb8f8c7068ef4ca1a` |
| `quantum_solves_2026-10-15.csv` | `918f06e4de106ddbf97449a2b2089ffa733c09450a5e0064d318843c78b1e654` |
| `quantum_summary_2026-10-15.csv` | `2522671e6cd5446b666acec84caca24d2cafa1eb9e835d58a8c3145d03155cc7` |
| `quantum_ansatz_2026-10-15.csv` | `176449dcf2e6e2a6991216af5d30b7b3bb660367f90997ad4a070a8db567c44b` |
| `quantum_ansatz_summary_2026-10-15.csv` | `78af545d0bc1aea3e1313c75e27cfc9263b0e2d8f6a0537d1a07bbf293b84a1e` |

### Design

Same grid as the [generalization study](generalization_study.md): fleets of 4
and 6 UAVs, two tasks per UAV, area side `100 × sqrt(agents/4)`; regimes
`connected` (range `130 × scale`), `sparse` (range `50 × scale`) and
`sparse_jammed` (sparse plus a jammer at `(40, 50) × scale`, radius
`15 × scale`, active for 3 ≤ t < 8, which partitions the swarm); seeds 1–20;
UAV 2 physically fails at t = 6.0; 0.5 s motion step; 30 s cap; distributed
control. Four policies per seed: `distance`, `connectivity`, `quantum_qaoa`,
`quantum_sqa`, 480 runs. Unlike the generalization study, the failure is at
6.0 rather than 4.0, so it falls inside the jammer window.

### Solver quality inside the missions

Each receiver-local solve, compared with brute force on the same problem:

| shape (UAVs × objectives) | qubits | QAOA solves | single-shot P(optimum), mean (min) | random feasible assignment | QAOA not optimal |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2 × 2 | 4 | 20 | 1.000 (1.000) | 0.500 | 0 |
| 2 × 3, 3 × 2 | 6 | 99 | 0.919 (0.215) | 0.167 | 0 |
| 2 × 4 | 8 | 241 | 0.474 (0.003) | 0.083 | 8 |
| 3 × 3 | 9 | 71 | 0.807 (0.056) | 0.167 | 0 |
| 3 × 4 | 12 | 492 | 0.606 (0.133) | 0.042 | 0 |

QAOA returned the optimum in 915 of 923 solves; SQA in all 925. The 8 QAOA
misses are 2 × 4 problems whose best and second-best assignments are nearly
degenerate; there the angle search can settle on a state concentrated on the
runner-up, and 64 shots never draw the optimum. The worst returned plan was
7.9 % longer than optimal.

"Single-shot P(optimum)" is a simulator diagnostic: the probability the final
state assigns to an optimal assignment. It is never used for decisions. Next
to it, "random feasible assignment" is what drawing one feasible assignment
uniformly at random would give. At 12 qubits the default ansatz puts 14 times
that probability on the optimum.

### Ansatz comparison

Every tenth problem recorded by the QAOA runs (93 problems), re-solved with
each ansatz at depth 4 and 64 shots:

| qubits | problems | `assignment`: optimal / P(opt) | `xy`: optimal / P(opt) | textbook `x`: optimal / P(opt) | random feasible |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 3 | 3 / 1.000 | 3 / 0.552 | 3 / 0.397 | 0.500 |
| 6 | 12 | 12 / 0.947 | 12 / 0.765 | 12 / 0.057 | 0.167 |
| 8 | 29 | 29 / 0.571 | 29 / 0.395 | 14 / 0.012 | 0.083 |
| 9 | 3 | 3 / 0.799 | 3 / 0.225 | 3 / 0.028 | 0.167 |
| 12 | 46 | 46 / 0.614 | 43 / 0.112 | 8 / 0.002 | 0.042 |

The textbook ansatz at 12 qubits puts less probability on the optimum
(0.002) than random feasible sampling (0.042) and found it in 8 of 46
problems; only 4.4 % of its final state is a feasible assignment at all.

### Solve time

Mean wall-clock per solve (`quantum_timing_2026-10-15.csv`; pure Python on
one core, measured outside the simulator while other jobs shared the
machine, so treat the absolute values as indicative). The textbook and `xy`
columns cover the 93-problem ansatz sample, the others every recorded solve.

| qubits | brute force | QAOA `assignment` | QAOA `xy` | QAOA textbook `x` | SQA |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 0.03 ms | 3.8 ms | 5.8 ms | 22 ms | 27 ms |
| 6 | 0.04 ms | 10 ms | 15 ms | 112 ms | 35 ms |
| 8 | 0.04 ms | 24 ms | 26 ms | 545 ms | 41 ms |
| 9 | 0.04 ms | 10 ms | 50 ms | 1.18 s | 44 ms |
| 12 | 0.06 ms | 49 ms | 118 ms | 14.3 s | 59 ms |

Simulating only reachable states makes the default ansatz about 290 times
faster than the textbook one at 12 qubits. Brute force is still two to three
orders of magnitude faster than any simulator at every size used.

### Mission outcomes

All 480 missions completed. Means over 20 seeds; `faster`/`slower` count
seeds where the policy finished before/after the distance policy on the same
seed. Contested time is in task-seconds.

| agents | regime | policy | mean time (s) | mean distance | contested | faster | slower |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 4 | connected | distance | 10.13 | 251.6 | 0.00 | – | – |
| 4 | connected | connectivity | 10.13 | 251.6 | 0.00 | 0 | 0 |
| 4 | connected | quantum (QAOA = SQA) | 10.33 | 252.5 | 0.00 | 6 | 5 |
| 4 | sparse | distance | 10.23 | 256.2 | 0.00 | – | – |
| 4 | sparse | connectivity | 10.08 | 257.8 | 0.00 | 2 | 0 |
| 4 | sparse | quantum (QAOA = SQA) | 10.00 | 256.7 | 0.10 | 3 | 3 |
| 4 | sparse_jammed | distance | 9.98 | 268.1 | 1.43 | – | – |
| 4 | sparse_jammed | connectivity | 10.20 | 273.9 | 1.43 | 1 | 2 |
| 4 | sparse_jammed | quantum (QAOA = SQA) | 9.73 | 264.3 | 1.13 | 3 | 3 |
| 6 | connected | distance | 9.65 | 358.5 | 0.00 | – | – |
| 6 | connected | connectivity | 9.65 | 358.5 | 0.00 | 0 | 0 |
| 6 | connected | quantum (QAOA = SQA) | 9.28 | 349.0 | 0.00 | 8 | 4 |
| 6 | sparse | distance | 9.65 | 358.7 | 0.00 | – | – |
| 6 | sparse | connectivity | 10.18 | 379.2 | 0.00 | 2 | 5 |
| 6 | sparse | quantum QAOA | 9.83 | 352.7 | 0.00 | 6 | 6 |
| 6 | sparse | quantum SQA | 9.70 | 354.2 | 0.00 | 7 | 5 |
| 6 | sparse_jammed | distance | 9.88 | 363.5 | 0.58 | – | – |
| 6 | sparse_jammed | connectivity | 10.30 | 382.8 | 0.38 | 2 | 4 |
| 6 | sparse_jammed | quantum QAOA | 9.88 | 354.8 | 0.28 | 7 | 6 |
| 6 | sparse_jammed | quantum SQA | 9.75 | 356.3 | 0.28 | 8 | 5 |

QAOA and SQA runs are identical in 112 of 120 pairs. The 8 that differ are
exactly the 6-UAV sparse and jammed runs that contain a QAOA miss. No run had
a claim refused at allocation.

Over all 120 seeds, against distance:

| policy | faster | slower | mean time change (± s.e.) | sign test | mean flight distance change | mean contested change |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| quantum QAOA | 33 | 27 | −0.08 ± 0.15 s | p = 0.52 | −4.4 (−1.4 %) | −0.08 |
| quantum SQA | 35 | 25 | −0.12 ± 0.15 s | p = 0.25 | −4.0 (−1.3 %) | −0.08 |
| connectivity | 7 | 11 | +0.17 ± 0.08 s | p = 0.48 | +7.9 | −0.03 |

### Interpretation

- **The quantum solver is now doing the work.** With realistic shot sampling,
  the default ansatz returned the optimum in 99 % of in-mission solves and, at
  the default 12-qubit size, put 14 times the single-shot probability of
  random feasible sampling on the optimum. The textbook ansatz used in the
  first version does worse than random sampling at that size.
- **No significant mission-time effect.** Mean mission time moved by about
  −0.1 s against per-seed swings of ±4.5 s; neither quantum variant differs
  from distance at any conventional significance level. The objective is one
  decision step (summed next-leg distance), and mission time is decided by
  the whole sequence of legs.
- **Small, consistent reductions in flight distance and claim conflict.**
  Total flight distance was lower than distance in every 6-UAV cell and about
  1.4 % lower overall. In the jammed regime contested time fell from 1.43 to
  1.13 (4 UAVs) and 0.58 to 0.28 task-seconds (6 UAVs), with fewer contested
  yields (16 → 14, 10 → 6 for QAOA), consistent with UAVs stepping aside from
  objectives a heard peer is better placed for. 20 seeds per cell; not tested
  for significance per cell.
- **Where it does nothing.** When a UAV hears no peer, or has fewer than two
  claimable objectives, the policy is the distance policy (tested). Sparse
  4-UAV runs had only 38 non-trivial solves in 20 runs.
- **QAOA misses cost little at mission level.** The 8 suboptimal QAOA solves
  fall in 8 runs; in those two cells QAOA and SQA differ by 0.13 s in mean
  time.

### Superseded 2 October results

The first version (textbook ansatz, depth 2, 3 × 3 formulations, decoding by
the eight most probable bitstrings) reported every one of 927 QAOA solves as
optimal and a mission-time change of +0.004 s (25 faster, 29 slower). The
"optimal" figure was produced by classical post-selection over statevector
probabilities that hardware cannot read; at 9 qubits that state put 0.4 % on
the returned bitstring. Those CSV files were removed in this revision and
remain in git history (commit `6be2566`).

## Limits

- **This is quantum simulation on classical hardware.** The statevector and
  the path-integral Monte Carlo run in pure Python. No quantum speedup is
  claimed or measured; at these sizes exact enumeration is far faster than
  either simulator (see the timing table).
- **The default ansatz is harder to build on hardware than the textbook
  one.** Its swap terms are 4-qubit interactions and its move terms are
  hopping terms conditioned on the target objective being free, and its
  initial state is a uniform superposition of assignments, which needs its
  own preparation circuit. None of that circuit cost is modelled here; the
  simulation is of the ideal, noiseless operators.
- **Problem sizes are small by design.** A formulation holds at most 3 UAVs and
  4 objectives by default (12 qubits, 24 feasible assignments). Larger fleets
  are handled by many small local problems, not one large one.
- **One known solver weakness.** For 2 × 4 problems with a nearly degenerate
  optimum, QAOA can concentrate on the runner-up (8 of 241 in the study).
- **The objective is one decision step.** The QUBO minimises the summed
  distance of the next leg for the agents in the formulation. Mission time is
  closer to a makespan over several legs, and the study finds no significant
  mission-time change.
- **Tuning disclosure.** The formulation choices of the first version were
  made while looking at seeds 1–10. In this revision the formulation size and
  the onward-cost test used seeds 21–40 only; the γ range and grid were
  changed after seeing three failures from seeds 1–20 (see Solvers).
- **Peer positions are heartbeat positions.** They are up to one heartbeat
  interval old and are not extrapolated.
- **Point-mass, logical-time simulation.** Same limits as every other result
  in this repository; see [`measured_results.md`](measured_results.md).

## Moving to hardware

`qubo_to_bqm(problem.qubo)` returns

```python
{"vartype": "BINARY",
 "linear": {"a1_t3": -412.7, ...},           # label -> bias
 "quadratic": {("a1_t3", "a1_t5"): 851.2, ...},
 "offset": 1277.4}
```

and `qubo_to_ising(problem.qubo)` the equivalent `{"vartype": "SPIN", "h",
"J", "offset"}`. Labels are `a{agent}_t{task}`. These are the constructor
arguments of a binary quadratic model in common annealing SDKs (for example
`dimod.BinaryQuadraticModel(linear, quadratic, offset, "BINARY")`), and the
Ising form maps term-by-term onto a gate-model cost Hamiltonian
(`h_i Z_i + J_ij Z_i Z_j`) for QAOA on hardware or a hardware simulator.
`problem.decode(bits)` turns any returned sample back into
`(agent_id, task_id)` pairs and rejects infeasible ones, so a hardware
sampler could replace `solve()` without touching the policy.

For a gate-model run of the constrained ansatzes, `one_hot_groups(problem)`
gives the qubit indices of each UAV's register and `one_hot_cost_qubo(problem)`
the cost Hamiltonian with the smaller in-subspace penalty; the same
`qubo_to_ising` export applies to it. The `xy` ansatz needs W-state
preparation per register and two-qubit XY gates, both standard; the
`assignment` ansatz needs the multi-qubit terms noted under Limits.

Not done, and not claimed: no run on quantum hardware or a vendor simulator,
no embedding onto a hardware topology, and no latency budget for a remote
sampler inside a UAV's decision loop. A remote sampler would also break the
receiver-local property unless it ran on the UAV.
