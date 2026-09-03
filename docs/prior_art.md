# Prior art and evidence boundaries

This note records the research and industrial references that most directly
bound the architectural claims of `eudis-swarm`. It is a comparison, not a
novelty claim: a cited technique is not implemented merely because it appears
here, and a paper's result is not evidence that this repository has reproduced
it. Sources were checked against publisher, author, institutional, project, or
official developer pages on 2026-09-03.

## Repository scope

At the starting baseline for the formal-autonomy milestone, the repository
already implements receiver-local peer evidence, immutable multi-hop protocol
flooding, lease-based distributed task ownership, deterministic split-brain
reconciliation, and simulated range/radio/jammer link behaviour. Centralized
allocators remain comparison baselines rather than operational authority.

The current milestone adds explicit receiver-local contact, peer-availability,
task-ownership-conformance, and coordination-mode EFSMs. A mission contract and
Mission EFSM, coordinated 3-D search, dynamic connectivity-support roles,
physical topology repair, deconfliction, quantum-assisted planning, autopilot
integration, and hardware validation remain future work. The implementation and
its tests are authoritative; this literature review is not.

## Evidence comparison

### 1. Partition-tolerant swarm state: SwarmDAG

J. A. Tran et al., [“SwarmDAG: A Partition Tolerant Distributed Ledger Protocol
for Swarm Robotics”](https://doi.org/10.5195/ledger.2019.174), *Ledger*, vol. 4,
2019.

**Establishes.** SwarmDAG proposes an extended-virtual-synchrony, DAG-based
ledger in which disconnected robot groups can continue recording progress and
later merge toward eventual consistency. It makes partition tolerance and
post-partition conflict handling explicit swarm-system concerns.

**Does not establish.** It does not validate this repository's claim, lease,
failure-evidence, or EFSM semantics, and it does not make partition-tolerant
swarm state novel by itself. The paper presents a system architecture rather
than a field validation of `eudis-swarm`-style UAV autonomy.

**Lesson for this repository.** Treat SwarmDAG as a close distributed-state
comparator. The defensible distinction must lie in the receiver-local autonomy
semantics, bounded evidence rules, execution authority, and observable recovery
loop—not in the bare fact that replicas tolerate partition and reconcile.

### 2. Communication-aware distributed task allocation

L. Lu et al., [“Communication-aware distributed task allocation for synchronous
multi-UAV collaboration”](https://doi.org/10.1016/j.ast.2026.113589),
*Aerospace Science and Technology*, article 113589, 2026.

**Establishes.** The paper combines small-world topology construction with a
robust auction-consensus allocator. Under its Webots/simulation conditions it
reports resilience to packet loss, delay, and topology switching, including a
100% allocation rate up to 60% packet loss and substantially lower message
volume than the selected baselines.

**Does not establish.** Those scenario-specific results do not prove general
partition tolerance, real-radio performance, lease-based split-brain safety, or
the correctness of this repository. Communication-aware distributed allocation
is therefore not a novelty claim available to `eudis-swarm`.

**Lesson for this repository.** Use comparable disturbances, allocation
success, completion efficiency, and message-volume metrics in future
experiments. Preserve the current separation between world-level delivery and
receiver-local authority instead of importing a globally coherent bid view.

### 3. Distributed 3-D search and receding-horizon control: GLIMPSE

S. Papaioannou et al., [“Distributed Search Planning in 3-D Environments With a
Dynamically Varying Number of
Agents”](https://doi.org/10.1109/TSMC.2023.3240023), *IEEE Transactions on
Systems, Man, and Cybernetics: Systems*, 2023; see also the official
[KIOS GLIMPSE guidance-and-planning
summary](https://www.kios.ucy.ac.cy/glimpse/multi-drone-guidance-and-planning/).

**Establishes.** The work formulates distributed multi-UAV 3-D search as a
rolling-horizon model-predictive-control problem. Agents may enter or leave,
exchange state, maps, intentions, and remaining flight time opportunistically,
and adapt trajectories to reduce duplicated coverage. Its evidence is
qualitative and quantitative simulation.

**Does not establish.** It is not a task-ownership authority protocol, a
partition-reconciliation mechanism, or a receiver-local contact EFSM. It also
does not show that the present repository already performs 3-D search.

**Lesson for this repository.** Reuse this line of work later for **how** an
owned search objective is executed. Keep continuous trajectory optimization
below discrete mission, ownership, coordination, and safety supervisors rather
than asking an EFSM to replace MPC or geometry.

### 4. Formal multi-agent task generation

A. A. Tziola and S. G. Loizou, [“A Formal Framework for Multi-Agent Task
Planning”](https://doi.org/10.1109/TAC.2025.3601769), *IEEE Transactions on
Automatic Control*, vol. 71, no. 1, pp. 630–637, 2026 (DOI/early-access record
issued in 2025).

**Establishes.** The framework composes agent capabilities, constraints, and
failure modes using nondeterministic finite automata with epsilon transitions
to generate plans satisfying a task specification. It analyzes completeness
and optimality and also offers a lower-cost heuristic that relaxes those
guarantees.

**Does not establish.** Its system model and high-level synthesis result do not
solve distributed runtime coordination under receiver-local partial
observability. It does not validate this repository's evidence propagation or
per-UAV decisions.

**Lesson for this repository.** Use formal task generation as a possible layer
above distributed execution: a future mission contract may compile authorized
objectives, while local EFSMs and ownership evidence decide what each UAV may
execute with the knowledge it actually has.

### 5. Communication-constrained physical coverage

S. G. Loizou and C. C. Constantinou, [“Multi-robot coverage on dendritic
topologies under communication
constraints”](https://doi.org/10.1109/CDC.2016.7798244), *55th IEEE Conference
on Decision and Control*, pp. 43–48, 2016.

**Establishes.** The paper gives an algorithm for complete coverage of
dendritic networks with location-dependent communication, collision avoidance,
and the minimum required robot count as an output. Evaluation is by simulation.

**Does not establish.** It does not cover arbitrary aerial topology, noisy
radio evidence, distributed task replicas, or real-UAV validation. It also
shows that communication-aware motion predates this project.

**Lesson for this repository.** A future relay/connectivity-support role needs
an explicit mission-utility test and comparative evaluation. “Move to preserve
communications” alone is neither a novel contribution nor a sufficient role
transition rule.

### 6. Heterogeneous vehicles and distributed shared memory: CHARISMA

[CHARISMA project—heterogeneous unmanned vehicles for maritime
surveillance](https://projects.algolysis.com/charisma/) (Algolysis, CMMI, and
CYENS; Cyprus Research and Innovation Foundation-supported project).

**Establishes.** The project page describes a hardware/software add-on through
which heterogeneous aerial, surface, and underwater vehicles form a mesh
network, expose a distributed shared-memory service, support ML/RL swarm
behaviour, and feed a maritime-surveillance digital twin. It states a target of
validation in a relevant environment at TRL 6.

**Does not establish.** A project description is not a peer-reviewed proof of
those outcomes, and “will be validated” is not evidence that every target has
been achieved. The public material reviewed here does not support a detailed
technical comparison of consistency, partition, or ownership semantics.

**Lesson for this repository.** Treat CHARISMA as a strong regional systems
comparator. Contrast its shared-memory direction carefully with this project's
availability-first, immutable-evidence task semantics; do not claim superiority
without common experiments and disclosed protocol details.

### 7. External heterogeneous-UAV benchmark: CARIC

M. Cao et al., [“Cooperative Aerial Robot Inspection Challenge: A Benchmark for
Heterogeneous Multi-UAV Planning and Lessons
Learned”](https://arxiv.org/abs/2501.06566), arXiv:2501.06566, 2025.

**Establishes.** CARIC is a simulation benchmark for heterogeneous multi-UAV
inspection with complementary sensors, a perception/control software stack,
diverse scenarios, and metrics emphasizing inspection quality and efficiency.
The paper compares strategies used by leading challenge teams.

**Does not establish.** A CARIC result would not by itself validate radio
fidelity, evidence semantics, safety, or hardware readiness. This repository
does not currently integrate CARIC merely by citing it.

**Lesson for this repository.** Use CARIC as a future external environment for
search, inspection, allocation, and motion-planning comparisons after stable
adapters exist. Keep protocol tests and local-knowledge invariants as separate
verification layers.

### 8. QAOA capabilities and limits

K. Blekos et al., [“A review on Quantum Approximate Optimization Algorithm and
its variants”](https://doi.org/10.1016/j.physrep.2024.03.002), *Physics
Reports*, vol. 1068, pp. 1–66, 2024; author manuscript
[arXiv:2306.09198](https://arxiv.org/abs/2306.09198).

**Establishes.** The review surveys QAOA formulations, variants, performance,
parameter optimization, noise, and hardware constraints. It confirms that
QUBO-encoded combinatorial optimization is a legitimate QAOA use case while
emphasizing that practical quantum advantage remains problem- and
hardware-dependent.

**Does not establish.** It does not show that QAOA beats strong classical
methods for swarm allocation, or that a quantum optimizer should control a
degraded swarm at runtime.

**Lesson for this repository.** A future quantum component should be an
optional candidate-plan generator. Compare identical instances against greedy,
strong classical heuristics, and exact solvers where feasible; report solution
quality, runtime, simulator resources, and failure fallback without implying
quantum advantage.

### 9. QUBO/QAOA for communication-network routing

K.-C. Chen et al., [“Resource-Efficient Compilation of Distributed Quantum
Circuits for Solving Large-Scale Wireless Communication Network
Problems”](https://arxiv.org/abs/2501.10242), arXiv:2501.10242, 2025.

**Establishes.** This preprint partitions a wireless-sensor-network routing
problem with spectral clustering, formulates intra-cluster routing as QUBO, and
uses classically simulated QAOA for subproblems. It reports lower modeled energy
cost than its greedy subgroup comparator on a proof-of-concept instance.

**Does not establish.** It does not demonstrate quantum advantage, real-QPU or
real-radio performance, multi-UAV task authority, or resilience under this
repository's failure model. Its evidence is a preprint and simulated quantum
execution with bounded subproblem sizes.

**Lesson for this repository.** It is a formulation reference, not a
performance promise. If network or initial-role optimization is encoded as
QUBO, retain deterministic classical baselines and keep the result advisory to
the distributed runtime EFSMs.

### 10. Statechart-based UAS mission execution

E. Santamaría Barnadas, [“Formal Mission Specification and Execution Mechanisms
for Unmanned Aircraft
Systems”](https://doi.org/10.5821/dissertation-2117-93334), doctoral thesis,
Universitat Politècnica de Catalunya, 2010.

**Establishes.** The thesis separates flight/mission specification from
execution, layers flight-plan and mission managers over a waypoint controller,
and uses State Chart XML (SCXML) to specify mission behaviour. Two simulated
missions exercise the proposed managers.

**Does not establish.** It does not demonstrate a distributed swarm controller,
receiver-local reasoning during partitions, or the correctness of this
repository's composed EFSMs. It also does not imply that SCXML is required here.

**Lesson for this repository.** It is direct precedent for explicit,
event-driven UAS mission state and separation from low-level flight control.
Prefer a small typed Python transition representation now, while preserving a
clean boundary for later mission specifications and vehicle adapters.

### 11. Supervisory control theory for robot swarms

Y. K. Lopes et al., [“Supervisory control theory applied to swarm
robotics”](https://doi.org/10.1007/s11721-016-0119-0), *Swarm Intelligence*,
vol. 10, pp. 65–97, 2016.

**Establishes.** The authors model robot capabilities and specifications as
formal languages, synthesize supervisors, generate controller code, and report
four case studies on e-puck and Kilobot platforms, including experiments with
up to 600 physical robots. The approach makes controllable/uncontrollable
events, deadlock freedom, decomposition, and reuse explicit.

**Does not establish.** The paper explicitly depends on representing the
relevant logic as a discrete-event system and does not claim that continuous
optimization or control should become an FSM. Its case studies are not UAV
partition/reconciliation experiments, and distributing one supervisor across
robots was identified as future work.

**Lesson for this repository.** Compose small explicit machines and keep their
events, guards, effects, and traces analyzable. Let EFSMs supervise discrete
autonomy decisions while radio equations, geometry, optimization, and flight
control remain numerical systems.

### 12. Public industrial task lifecycle: Anduril Lattice

Anduril, [“Tasks overview”](https://developer.anduril.com/guides/tasks/overview/)
and [“Update task
status”](https://developer.anduril.com/reference/rest/tasks/update-task-status),
Lattice Developers documentation.

**Establishes.** The public API documents an explicit task lifecycle, named
statuses, monotonically increasing status versions, stale-update handling, and
terminal success/failure states that cannot be updated. This is an industrial
example of explicit, versioned lifecycle semantics at an integration boundary.

**Does not establish.** Public API documentation reveals neither Anduril's
proprietary autonomy implementation nor a universal engineering rule. In
particular, it does not support claims that Anduril “never solves a problem
without a state machine,” that all Lattice internals are FSMs, or that its task
model matches this repository's distributed ownership protocol.

**Lesson for this repository.** Cite Lattice only as evidence that explicit
task transitions, terminal states, and versioned updates are used in a public
industrial interface. Make no absolute or proprietary claim.

## Cross-cutting conclusions

1. Partition tolerance, communication-aware allocation, communication-aware
   motion, formal swarm controllers, and statechart UAS missions are established
   prior-art categories. None is a defensible novelty claim in isolation.
2. The strongest project thesis is their composition under an epistemic rule:
   each UAV's transition must be justified only by evidence locally available
   to that UAV, while observer-only truth remains outside control guards.
3. Finite-state structure belongs around discrete decisions—contact posture,
   peer availability, ownership, coordination mode, mission phase, role,
   handoff, and safety supervision—not around radio propagation, trajectory
   geometry, numerical optimization, or flight control.
4. Future search, connectivity repair, quantum planning, CARIC integration, and
   SITL/hardware work require explicit baselines and experiments. They must not
   be described as implemented before code and tests exist.

## Verification notes and uncertainty

- Elsevier currently assigns the Lu et al. article to volume 179, part 4,
  December 2026, which is later than this review date; the DOI, title, abstract,
  and article number are live, but final issue metadata should be rechecked when
  cited in a submission.
- The Tziola–Loizou DOI and accepted manuscript date are from 2025, while the
  final journal issue is volume 71(1), 2026. Both dates may appear in indexes.
- The Chen et al. and CARIC sources are arXiv preprints. Their claims should be
  reported as author results, not as independently reproduced results.
- The CHARISMA source is consortium project material. It establishes stated
  aims and architecture, not peer-reviewed protocol guarantees; the public page
  reviewed here does not state the expanded project acronym or enough detail
  for a consistency-model comparison.
- The Anduril comparison is deliberately limited to public developer
  documentation. No inference about proprietary internal autonomy is warranted.
