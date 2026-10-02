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
| Assignment QUBO, three solvers, hardware-neutral export | [`quantum_opt.py`](../src/eudis_swarm/quantum_opt.py) |
| Receiver-local formulation and preference ordering | [`quantum_policy.py`](../src/eudis_swarm/quantum_policy.py) |
| Simulator wiring: `--allocation-policy quantum`, `--quantum-solver qaoa\|sqa` | [`simulation.py`](../src/eudis_swarm/simulation.py), [`config.py`](../src/eudis_swarm/config.py) |
| Paired study CLI (`eudis-swarm-quantum-study`) | [`quantum_study.py`](../src/eudis_swarm/quantum_study.py) |
| Raw data | [`data/quantum_*_2026-10-02.csv`](data/) |

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
| `qaoa` | QAOA simulated on a pure-Python statevector: diagonal cost-phase layer, RX mixer on every qubit, depth `p` (default 2). Angles: an 8×8 grid per layer, then four rounds of coordinate refinement on the expected energy, step halved each round. Decode: evaluate the 8 most probable bitstrings classically, keep the best feasible one. | ≤ 16 qubits |
| `sqa` | simulated quantum annealing: path-integral Monte Carlo of the transverse-field Ising model, 16 Suzuki–Trotter replicas, 200 sweeps, transverse field annealed linearly from 3.0 to 0.01 at temperature 0.05 (units of the largest Ising coefficient). `random.Random(seed)`, seed derived from a SHA-256 of the coefficients. Returns the best feasible replica seen. | ≤ 256 qubits |

The QAOA diagonal is normalised to `[0, 1]` before the angle search so one
grid fits every instance; this rescales `H_C` and changes no ordering.

**Depth choice.** On 60 random instances of up to 12 qubits, depth 1 decoded
the exact optimum in 33; depth 2 in 60; depth 3, with the same greedy
layer-by-layer angle search, in 44. Depth 2 is the default. Depth 3 doing worse
than depth 2 is a property of this angle search, not of QAOA.

**What decoding does.** The returned answer is the best of eight classically
evaluated candidates. The study records, for each solve, the probability the
final state puts on the returned bitstring and the total probability on
feasible bitstrings, so the contribution of the quantum state and of the
classical post-selection can be judged separately.

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
- **objectives**: up to `quantum_max_objectives` (default 3) of the objectives
  this UAV may itself claim, chosen round-robin — this UAV's nearest, then
  each included peer's nearest not yet chosen, and so on — so that no peer is
  forced onto objectives that only suit this UAV.

Defaults therefore give at most 9 qubits. A problem with one agent or one
objective has the UAV's own nearest objective as its exact optimum and is not
solved.

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

## Measured results (2 October 2026)

### Reproduce

```bash
eudis-swarm-quantum-study --seeds 20 --seed-base 1 \
  --output docs/data/quantum_runs_2026-10-02.csv \
  --solves-output docs/data/quantum_solves_2026-10-02.csv \
  --summary-output docs/data/quantum_summary_2026-10-02.csv \
  --timing-output docs/data/quantum_timing_2026-10-02.csv
```

About 9 minutes on one core. The run, solve and summary files are
byte-identical between runs; SHA-256 of the committed copies:

| File | SHA-256 |
| --- | --- |
| `quantum_runs_2026-10-02.csv` | `a331b96b2282b23d7425e753d903927116fe0ac77289e2455b5866f85d650c3f` |
| `quantum_solves_2026-10-02.csv` | `3ad84ce05776f447c3688971632a0f2d257b0c6c248786d08c5432e4d7f4d815` |
| `quantum_summary_2026-10-02.csv` | `91e9cb0077b4cf29e17a39bb967f46673744c6ffe7cb110aa81e3d5619fb2814` |

The timing file is wall-clock and differs on every run and machine.

### Design

Same grid as the [generalization study](generalization_study.md): fleets of 4
and 6 UAVs, two tasks per UAV, area side `100 × sqrt(agents/4)`; regimes
`connected` (range `130 × scale`), `sparse` (range `50 × scale`) and
`sparse_jammed` (sparse plus a jammer at `(40, 50) × scale`, radius
`15 × scale`, active for 3 ≤ t < 8, which partitions the swarm); seeds 1–20;
UAV 2 physically fails at t = 6.0; 0.5 s motion step; 30 s cap; distributed
control. Four policies per seed: `distance`, `connectivity`, `quantum_qaoa`,
`quantum_sqa`. 480 runs. Unlike the generalization study, the failure is at
6.0 rather than 4.0, so it falls inside the jammer window.

### Solver quality

| | QAOA (p = 2) | SQA |
| --- | ---: | ---: |
| receiver-local solves | 927 | 927 |
| feasible | 927 | 927 |
| equal to the brute-force optimum | 927 | 927 |
| solves that changed the UAV's first choice | 127 | 127 |

Every solve in the study was optimal for both solvers, so the two quantum
rows below are identical run for run, and identical to what an exact
classical solver would give on the same formulation. **The measured mission
effect is the effect of the formulation, not of the quantum algorithms.**

How much of the QAOA answer comes from the quantum state, by problem size
(mean over solves):

| qubits | solves | probability on returned bitstring | probability on any feasible bitstring | uniform feasible share |
| ---: | ---: | ---: | ---: | ---: |
| 4 | 23 | 0.298 | 0.594 | 2/16 = 0.125 |
| 6 | 336 | 0.030 | 0.172 | 6/64 = 0.094 |
| 9 | 568 | 0.004 | 0.022 | 6/512 = 0.012 |

At 4 qubits the depth-2 state concentrates clearly on the answer. At 9 qubits
it is only weakly concentrated (2.2 % feasible mass against 1.2 % for the
uniform state); the optimum is found because it is among the eight most
probable bitstrings and the classical evaluation of those eight picks it. A
sampler on hardware would need many shots for the same result.

Wall-clock per solve (this machine, `quantum_timing_2026-10-02.csv`):

| qubits | brute force | QAOA | SQA |
| ---: | ---: | ---: | ---: |
| 4 | 0.03 ms | 3.5 ms | 20 ms |
| 6 | 0.03 ms | 14 ms | 27 ms |
| 9 | 0.03 ms | 141 ms | 35 ms |

Brute force is three to four orders of magnitude faster at every size used.

### Mission outcomes

All 480 missions completed. Means over 20 seeds; `faster`/`slower` count
seeds where the policy finished before/after the distance policy on the same
seed. Contested time is in task-seconds.

| agents | regime | policy | mean time (s) | mean distance | contested | faster | slower |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 4 | connected | distance | 10.13 | 251.6 | 0.00 | – | – |
| 4 | connected | connectivity | 10.13 | 251.6 | 0.00 | 0 | 0 |
| 4 | connected | quantum | 10.23 | 254.9 | 0.00 | 5 | 4 |
| 4 | sparse | distance | 10.23 | 256.2 | 0.00 | – | – |
| 4 | sparse | connectivity | 10.08 | 257.8 | 0.00 | 2 | 0 |
| 4 | sparse | quantum | 10.00 | 256.7 | 0.10 | 3 | 3 |
| 4 | sparse_jammed | distance | 9.98 | 268.1 | 1.43 | – | – |
| 4 | sparse_jammed | connectivity | 10.20 | 273.9 | 1.43 | 1 | 2 |
| 4 | sparse_jammed | quantum | 9.73 | 264.3 | 1.13 | 3 | 3 |
| 6 | connected | distance | 9.65 | 358.5 | 0.00 | – | – |
| 6 | connected | connectivity | 9.65 | 358.5 | 0.00 | 0 | 0 |
| 6 | connected | quantum | 10.03 | 359.6 | 0.00 | 5 | 7 |
| 6 | sparse | distance | 9.65 | 358.7 | 0.00 | – | – |
| 6 | sparse | connectivity | 10.18 | 379.2 | 0.00 | 2 | 5 |
| 6 | sparse | quantum | 9.75 | 356.7 | 0.00 | 4 | 6 |
| 6 | sparse_jammed | distance | 9.88 | 363.5 | 0.58 | – | – |
| 6 | sparse_jammed | connectivity | 10.30 | 382.8 | 0.38 | 2 | 4 |
| 6 | sparse_jammed | quantum | 9.80 | 359.1 | 0.33 | 5 | 6 |

(`quantum` is both `quantum_qaoa` and `quantum_sqa`; they are identical.)
No run of any policy had a claim refused at allocation. Contested-claim yields
over 20 seeds: 4 UAVs jammed, 16 distance / 14 quantum; 6 UAVs jammed,
10 / 6; equal elsewhere. Concurrent-ownership time (summary CSV) in the
jammed regime: 0.98 vs 1.18 task-seconds (4 UAVs) and 0.18 vs 0.20 (6 UAVs),
quantum vs distance.

Over all 120 pairs, the quantum policy was faster than distance on 25 seeds
and slower on 29; the mean change in mission time was +0.004 s (range −4.5 s
to +4.5 s), in flight distance −0.9, and in contested time −0.075
task-seconds.

### Interpretation

- **No consistent mission-time benefit.** Per-seed differences of several
  seconds in either direction average out to nothing. The one-step objective
  (summed next-leg distance) is not the mission objective, and a better first
  leg often leads to a worse sequence of later legs.
- **Small reduction in claim conflict under partition.** In the jammed regime
  the quantum policy had less contested time than distance in both fleet
  sizes (1.13 vs 1.43 and 0.33 vs 0.58 task-seconds) and fewer contested
  yields, consistent with UAVs stepping aside from objectives a heard peer is
  better placed for. The differences are small and come from 20 seeds per
  cell; they are not tested for significance.
- **Where it does nothing.** When a UAV hears no peer, or has fewer than two
  claimable objectives, the policy is the distance policy (tested). In sparse
  4-UAV runs only 38 non-trivial solves occurred in 20 runs.
- **Compared with connectivity.** The connectivity policy was faster than
  distance on average only in the 4-UAV sparse cell, and flew further than
  distance in every sparse and jammed cell; the quantum policy flew within
  1.4 % of distance in every cell.
- **The quantum solvers did not decide anything an exact solver would not.**
  At this size that is the expected and honest outcome: the value shown here is
  a correct, receiver-local formulation that hardware samplers could take
  over, not a performance gain from quantum computation.

## Limits

- **This is quantum simulation on classical hardware.** The statevector and
  the path-integral Monte Carlo run in pure Python. No quantum speedup is
  claimed or measured; at these sizes exact enumeration is faster than either
  simulator (see the timing table).
- **Problem sizes are small by design.** A formulation holds at most 3 UAVs and
  3 objectives by default (9 qubits); the QAOA simulator is capped at 16
  qubits. Larger fleets are handled by many small local problems, not one
  large one.
- **The objective is one decision step.** The QUBO minimises the summed
  distance of the next leg for the agents in the formulation. Mission time is
  closer to a makespan over several legs; a cheaper next leg can lead to a
  worse tour, and the study shows this happening (29 of 120 seeds slower).
- **The formulation design was chosen while looking at seeds 1–10** of the
  4-UAV connected and sparse-jammed scenarios (including busy peers, and
  round-robin objective choice). The study uses seeds 1–20 and both fleet
  sizes, so half of the 4-UAV data and all of the 6-UAV data were not used
  for those choices.
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

Not done, and not claimed: no run on quantum hardware or a vendor simulator,
no embedding onto a hardware topology, and no latency budget for a remote
sampler inside a UAV's decision loop. A remote sampler would also break the
receiver-local property unless it ran on the UAV.
