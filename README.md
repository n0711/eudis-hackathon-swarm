# eudis-swarm

> **New here?** Start with **[`docs/onboarding.md`](docs/onboarding.md)** — setup,
> what to read in what order, and the three rules that must not be broken. About
> an hour from nothing to changing code with confidence.

**eudis-swarm is a deterministic, dependency-free Python simulator for a
communications-aware autonomous UAV swarm.** It models what happens when radio
contact splits a swarm into disconnected groups: each UAV keeps working from its
own local knowledge, and conflicting task ownership is reconciled autonomously
once connectivity returns. The normal mission now turns receiver-local utility
into claims; only a locally owned claim authorizes motion, renewal, or completion.
The centralized allocators remain comparison baselines, not the operational
controller. Each UAV now also exposes a composed, receiver-local EFSM kernel for
contact inference, peer availability, task-ownership conformance, and epistemic
coordination mode, with pure transition results and observer-only traces.

Every result below is reproducible from a fixed command line. There is no
wall-clock time, no unseeded randomness, and no order-dependent iteration in the
simulator, so the same commands produce the same numbers on any machine.

The operating environment this is aimed at is not hypothetical: EASA currently
lists `LCCC NICOSIA` among the Flight Information Regions affected by GNSS
jamming and spoofing, and a peer-reviewed flight experiment in that FIR recorded
a receiver losing positioning for 80% of the time aloft. See
[`docs/threat_context.md`](docs/threat_context.md) — including what those
sources deliberately do *not* establish about this simulator.

## The result in two commands

Declaring a UAV failed requires a quorum, and the threshold guarantees that one
observer can never declare a peer dead. With two aircraft the survivor **is**
the only observer, so the loss can never be declared at all. Both control modes
are then equally blind — what differs is whether recovery needed the
declaration.

```bash
eudis-swarm --scenario demo-quorum-floor      # distributed ownership
eudis-swarm --scenario baseline-quorum-floor  # central coordinator
```

On Windows, `scripts/pitch-demo.ps1` runs both back to back and needs no
activated environment; it calls the interpreter in `.venv` directly.

| | declared | never detected | tasks | mission |
| --- | ---: | ---: | ---: | --- |
| distributed ownership | 0 | 1 | **3/3** | completed at 10.75 s |
| central coordinator | 0 | 1 | 2/3 | **never finishes** |

The two presets differ by exactly one setting, asserted by test. Central
reassignment waits for a declaration that cannot arrive, so it holds a task
assigned to an aircraft that has stopped. Distributed ownership recovers
through lease expiry, which requires no agreement at all.

**Consensus-free recovery keeps working in exactly the conditions that make
consensus impossible.** A third aircraft restores the quorum and both modes
recover, which confirms the floor is about witness count rather than about the
control model.

This is not a property of one contrived pair. Across 440 runs with four
aircraft and a jammer, central control failed 68 missions and distributed
failed none, while the two modes detected losses *identically* at every
communication range — the same awareness, opposite outcomes. Full tables and
limits: [`docs/measured_results.md`](docs/measured_results.md).

[![CI](https://github.com/n0711/eudis-hackathon-swarm/actions/workflows/ci.yml/badge.svg)](https://github.com/n0711/eudis-hackathon-swarm/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Coverage gate 85%](https://img.shields.io/badge/coverage%20gate-85%25-brightgreen)](#verification)
[![Checked with Pyright](https://img.shields.io/badge/types-pyright-informational)](https://microsoft.github.io/pyright/)

## Current prototype boundary

The working demonstration is a **deterministic two-dimensional simulation** of
receiver-local task ownership under communication loss. It includes a point-mass
`VehicleBackend`, paired centralized comparisons, a recorded-trace dashboard,
and a separate one-vehicle ArduPilot SITL experiment. A `MavlinkBackend` adapter
can now route one agent's motion through live SITL telemetry inside an actual
coordination mission ([`docs/sitl.md`](docs/sitl.md)) — implemented and tested,
but not yet run against a live autopilot. It does **not** yet include live
perception observations, exploration mapping, relay positioning, flight safety
supervision, or SITL-controlled *swarm* missions (multiple vehicles at once).
For a short demonstration use the [runbook](docs/demo_runbook.md); for planned
perception input see the
[observation replay boundary](docs/observation_integration.md). A standalone
two-source JSONL replay now validates and ages receiver-local observations; it
does not yet deliver them through `Simulation` or create mission tasks.

## EUDIS Challenge

Independent project for the **EUDIS Defence Hackathon 2026 – Autumn Edition**,
**Challenge 2 – Swarm Coordination and Autonomous Operations**.

The challenge calls for:

- **multi-agent coordination** — many platforms cooperating on one mission;
- **resilient communications** — useful behaviour while links are degraded,
  jammed, or fully partitioned;
- **distributed task allocation** — deciding who does what without a single
  authoritative planner;
- **minimal human control** — the swarm sustains a mission on its own;
- **vehicle-agnostic direction** — coordination logic that does not depend on a
  particular airframe or autopilot.

This repository is one team's prototype. It is not affiliated with, sponsored
by, endorsed by, or derived from any hackathon partner, sponsor, or host
organisation.

## Core Idea

```text
communication loss != UAV failure
```

A UAV that has gone quiet may be jammed, behind terrain, or briefly out of range
— and still flying, still moving, still finishing tasks. Treating silence as
death discards that work and reassigns it needlessly.

So the system permits **temporary disagreement**. During a partition, two groups
can each believe they legitimately own the same task, and both beliefs can be
locally valid. The guarantee is deliberately weaker than global consensus:

> After connectivity is stably restored, every replica must converge on the same
> owner for every task, and the losing claimant must stand down on its own.

The rule is enforced structurally, not by convention: a peer is marked
`DECLARED_FAILED` only through a quorum vote carried over modelled links, never
because a heartbeat was missed.

## Current Architecture

```text
Mission / Simulation World
        |
        +-- world truth / physics / evaluation
        |     (real positions, CommunicationGraph, observer projections, metrics)
        |
        +-- Agent 1
        |    +-- immutable TaskObjective catalogue
        |    +-- ReceiverLocalTaskUtility
        |    +-- PeerStateStore     (delivered heartbeats, freshness, declarations)
        |    +-- TaskClaimStore     (immutable claims, leases, epochs, reconciliation)
        |    +-- LocalAutonomyKernel
        |    |     +-- ContactEFSM             (one per peer)
        |    |     +-- PeerAvailabilityEFSM    (one per peer)
        |    |     +-- TaskOwnershipEFSM       (one per task)
        |    |     +-- CoordinationModeEFSM    (one per UAV)
        |    +-- current_task       (local intent cache; not ownership authority)
        |
        +-- Agent 2
        |    +-- immutable task catalogue + local utility
        |    +-- PeerStateStore
        |    +-- TaskClaimStore
        |    +-- LocalAutonomyKernel
        |
        +-- Agent N
             +-- same receiver-local components
```

The hardening sequence is explicit. The original prototype delivered protocol
messages only over direct links and copied perfect pair reachability from
`CommunicationGraph` into local peer state. The oracle was removed first. This
was followed by multi-hop dissemination for immutable task/failure evidence. The
current task-control milestone now makes receiver-local claims the execution
authority without weakening either boundary.

All inter-agent information passes through the modelled communication graph. The
world/network layer decides whether each physical hop is available; it never
copies that topology fact into a peer store. Heartbeats intentionally remain
one-hop observations. Immutable task claims, releases, completions, failure
votes, and declarations use deterministic store-and-forward flooding, so an
intermediate UAV can carry evidence without becoming its logical origin.

Each forwardable message has a structural identity. Every receiver suppresses
duplicates and processes messages, nodes, and neighbours in stable order. A
receiver applies each logical message only once; unsuccessful directed routes
remain pending and are retried after later topology changes until delivery or
suppression by another successful route. Finite swarm membership and finite
per-message/per-node seen state make the flood loop-safe without a TTL. Logical
origin, forwarding hop, receiver, and hop count remain distinct in observer-only
delivery receipts. The `PROTOCOL GOSSIP` summary reports attempted, delivered,
useful, forwarded, duplicate, and inactive-deferral activity separately from
one-hop heartbeat delivery. Versioned trace metrics expose the same logical
outcomes, using `protocol_messages_dropped` for unavailable-link attempts.

```text
network connectivity != direct connectivity
```

Stable connectivity for long enough to run dissemination is the convergence
condition; a direct all-to-all graph is not required. A partition still prevents
evidence crossing its cut. The default graph is deterministic and range-based.
The repository also contains an optional free-space line-of-sight radio model
and seeded stochastic link sampling; this is a small analytical link model, not
realistic RF propagation. Latency, bandwidth, interference, terrain, and a full
MANET routing protocol remain outside scope.

Three kinds of state are kept strictly separate:

| Layer | Holds | Must not |
| --- | --- | --- |
| **World / physics** | real `Agent` positions and responsiveness, the live `CommunicationGraph`, and mutable `Task` records maintained as observer-facing mission projections | authorize ownership from `Task.assigned_agent`, or hand a remote UAV's live state to local decision logic |
| **Receiver-local control** | own pose and responsiveness, the immutable `TaskObjective` catalogue, `ReceiverLocalTaskUtility`, one `PeerStateStore`, one `TaskClaimStore`, and a local execution-intent cache | infer failure from silence alone, copy graph or projected task-owner truth, or act without `owns_task()` |
| **Observer / evaluation** | metrics, traces, test assertions comparing belief against truth | feed its privileged view back into allocation or failure decisions |

## Measured behaviour

Every number this project claims lives in one place, with the command that
reproduces it and an explicit statement of what is *not* measured:
**[`docs/measured_results.md`](docs/measured_results.md)**.

In the original sweep, the two control modes paid different costs:
the centralized runs in that sweep had no measured overlapping self-claims
and stranded work they could not reach, while replicated ownership had no
dropped ownership and occasionally overlapped claims during a partition. A
separate [fleet/network comparison](docs/generalization_study.md) found brief
central claim overlap involving a stopped UAV; claim overlap is not necessarily
duplicate flight. Failure detection has a connectivity floor below which a
genuinely dead aircraft is never declared, and no mission-level number reveals
the loss.
The [layout/fault-time check](docs/robustness_study.md) shows how strongly the
completion gap varies with geometry and when the physical failure happens.
Across 440 runs, central control failed 68 missions and distributed failed none,
with the two modes detecting losses identically at every range.

Figures are deliberately not repeated here: one source of truth, so a stale copy
cannot contradict the measured one.

For how the system got here — what was built, in what order, and why — see
[`docs/milestones.md`](docs/milestones.md).

## Task Ownership EFSM

`TaskClaimStore` remains the operational evidence ledger and `owns_task()`
remains authorization. The formal task machine is a conformance projection of
that receiver-local ledger view; it does not duplicate claim validation,
expiry, or reconciliation. `TaskOwnershipState` has exactly six states:

| State | Meaning at this receiver |
| --- | --- |
| `UNCLAIMED` | no valid current claim is known (none seen, or the only one is lease-expired) |
| `OWNED_BY_SELF` | the selected valid claim is this UAV's own |
| `CLAIMED_BY_PEER_FRESH` | a peer holds the selected valid claim and the local copy is `FRESH` |
| `CLAIMED_BY_PEER_STALE` | a peer holds the selected valid claim but the local copy is `STALE` |
| `CONTESTED` | more than one valid current owner is visible to this replica |
| `COMPLETE` | terminal completion evidence exists for the task (absorbing) |

Freshness of a stored claim, by receiver-local age:

```text
fresh:    age <= freshness_timeout
stale:    freshness_timeout < age <= lease_timeout
expired:  age > lease_timeout
```

```text
STALE != UNCLAIMED
```

A `STALE` claim is still within its lease: the task is not free, and a replica
may not create a competing claim until its known claim is `EXPIRED`.

### Clock semantics

Protocol timestamps have two roles:

```text
created_at / detected_at / Heartbeat.timestamp = source-clock metadata
received_at                                = receiver-local control time
```

Silence, freshness, vote retention, and claim lease expiry use receiver-local
age (`local_now - received_at`). Re-delivering identical forwardable protocol
evidence does not reset that age. A heartbeat is different: each successful
one-hop receipt is new first-hand contact and refreshes local liveness. The
simulator currently supplies one deterministic logical clock to every component,
so observer-only traces may compare send and receipt timestamps to report
latency. Decision logic does not rely on that cross-agent comparison; a physical
implementation may have clock skew.

An executing owner renews when its locally accepted claim reaches the freshness
threshold, rather than publishing on every simulation tick. The default
freshness threshold is half the lease duration. If ownership is no longer
actionable at the start of a claim round, the UAV stands down before renewal.

## Default Distributed-Control Result

The current default command is deterministic:

```bash
python -m eudis_swarm.simulation
```

It completes all **20 / 20 tasks in 17.25 s**. UAV 2 physically fails while
holding Task 19; the task becomes available to a receiver only after its local
copy of the failed owner's claim expires, and UAV 1 activates the replacement at
`t = 10.75 s`. One-hop peer-state traffic records `174 / 132 / 42`
attempted/delivered/undelivered deliveries.

The protocol-gossip summary is:

| Counter | Default | Meaning |
| --- | ---: | --- |
| Logical forwarding attempts | 178 | eligible sender/receiver/message evaluations |
| Successful first deliveries | 178 | first receipt of a structural message ID at a receiver |
| Unavailable-link attempts | 0 | eligible evaluations rejected by the current one-hop graph |
| Useful first deliveries | 178 | first deliveries that changed receiver-local domain state |
| Forwarded first deliveries | 0 | successful first deliveries whose hop sender was not the logical origin |
| Duplicate source publications | 1,552 | an origin republished a structural ID already seeded into transport |
| Duplicate routes suppressed | 428 | a redundant route was removed because the receiver already knew that ID |
| Inactive-endpoint deferrals | 53 | newly observed `(message, receiver)` obligations held without counting a link attempt |

The default graph remains a full clique, which explains zero unavailable-link
attempts and zero forwarded first deliveries; chain tests exercise actual relay
hops. These are logical simulator/evidence counters, not RF packet, byte,
bandwidth, queue, or airtime measurements. Inactive software is excluded from
the attempt denominator, while its pending evidence obligations remain available
for a later participating round.

## Demonstrated Partition Scenario

[`eudis_swarm.task_claim_demo`](src/eudis_swarm/task_claim_demo.py) runs a fixed,
fully deterministic 2+2 scenario. Agents `(1, 2, 3, 4)`, tasks
`(7, 11, 19, 23, 29)`, `freshness_timeout = 1.0`, `lease_timeout = 3.0`.

1. **Connected.** All four agents share an active graph. UAV 1 creates and
   publishes a claim on **Task 19** at `t = 0`.
2. **Partition `{1,2}` / `{3,4}`.** The four cross-component links
   `(1,3) (1,4) (2,3) (2,4)` are blocked; delivery still works inside each half.
3. **The isolated half ages its evidence.** With no renewals reaching `{3,4}`,
   their copy of the Task 19 claim goes `CLAIMED_BY_PEER_FRESH` →
   `CLAIMED_BY_PEER_STALE` (by `t ≈ 1.2`) → `UNCLAIMED` once the lease expires
   (by `t ≈ 3.2`). UAV 1 keeps renewing and still owns Task 19 on the left.
4. **A legitimate competing claim.** With its local evidence now `EXPIRED`,
   UAV 4 creates its own Task 19 claim at `t = 3.3`. Both `stores[1]` and
   `stores[4]` report `OWNED_BY_SELF` — a genuine split brain.
5. **Reconnection.** The CLI demo restores its original full-clique topology.
   The next delivery makes both still-valid claims visible, so all four stores
   report `CONTESTED`.
6. **Independent reconciliation.** At `t = 4.0` every store runs `reconcile()`
   and independently selects **UAV 1** (lowest owner ID). UAV 4 is the local
   loser and emits an exact-generation release.
7. **Convergence.** The release propagates; every store now reports
   `known_owner_agent_id == 1` and `contested == False`.
8. **Mission continues.** UAV 4 immediately claims **Task 29** at `t = 4.5`
   without a restart. The run emits a 10-frame observer-only trace.

Separate focused integration tests reconnect split-brain replicas as the chain
`1 <-> 2 <-> 3 <-> 4`. They prove convergence through intermediate UAVs without
changing this established CLI demonstration or its published timeline.

CLI summary line from the demo:

```text
Task-claim demo: 2+2 partition, Task 19 -> UAV 1, UAV 4 released and claimed Task 29; 10 trace frames.
```

## Verification

Run these gates on the final working tree. Exact counts and percentages are
intentionally not pinned here; the configured branch-coverage floor is 85%.

| Gate | Command | Required result |
| --- | --- | --- |
| Lint | `python -m ruff check .` | pass |
| Format | `python -m ruff format --check .` | pass |
| Types | `python -m pyright` | zero errors |
| Tests + coverage | `python -m pytest --cov=eudis_swarm --cov-report=term-missing` | pass at or above 85% |
| Package build | `python -m build` | build `sdist` + `wheel` |

At the current milestone every gate above passes: Pyright reports zero errors,
Ruff lint and format checks pass, branch coverage sits above the 85% floor, the
deterministic demos pass, and the source and wheel distributions build cleanly.
CI runs the same gates on every push to `main`, so the badge at the top of this
file is the live answer rather than a figure transcribed here and left to rot.

The GitHub Actions workflow runs the same lint, format, type, test/coverage, and
build steps on Python 3.11 and 3.13 for pushes and pull requests against `main`,
then installs the built wheel and smoke-tests `eudis-swarm --help` / `--version`.

## Repository Structure

```text
eudis-swarm/
├── src/eudis_swarm/
│   ├── communication.py        # distance + blocked-link comms graph, components, partitions, can_deliver()
│   ├── messaging.py            # one-hop heartbeats + multi-hop immutable evidence flooding
│   ├── peer_state.py           # per-UAV PeerStateStore: freshness, PeerStatus, declarations
│   ├── failure_manager.py      # isolated detector replicas, FailureVote / FailureDeclaration, quorum
│   ├── task_claims.py          # per-UAV TaskClaimStore: immutable claims, leases, epochs, reconciliation
│   ├── task_utility.py         # immutable objectives + receiver-local additive claim-intent utility
│   ├── autonomy.py             # pure Contact/Peer/Task/Coordination EFSMs + local composition kernel
│   ├── task.py                 # observer Task projection + six-state TaskOwnershipState vocabulary
│   ├── task_allocator.py       # centralized distance/connectivity comparison baselines
│   ├── mission.py              # claim-authorized execution, observer projection, lifecycle, invariants
│   ├── vehicle_backend.py      # physical movement protocol + default point-mass backend
│   ├── evaluation.py           # observer-only coordination cost measurements
│   ├── observation_replay.py   # receiver-local external observation JSONL replay
│   ├── generalization.py      # paired fleet/network evaluator CLI
│   ├── robustness.py          # paired layout/fault-time evaluator CLI
│   ├── mission_contract.py    # versioned operator intent and degradation policy
│   ├── sitl.py                # optional standalone one-vehicle ArduPilot utility
│   ├── simulation.py           # local claim rounds, logical clock, physics, orchestration, CLI
│   ├── simulation_events.py    # structured comms / peer-knowledge / task-claim event types
│   ├── metrics.py              # separate physical-mission and network metric blocks
│   ├── trace.py                # schema-v3 world/belief/ownership/EFSM playback frames
│   ├── task_claim_trace.py     # observer-only agent-by-task ownership trace (no authoritative fields)
│   ├── task_claim_demo.py      # deterministic 2+2 split-brain / reconcile / continue demo + CLI
│   ├── agent.py                # world-truth UAV state, 2D point-mass motion, immutable heartbeats
│   ├── config.py               # validated physical + communication configuration
│   └── validation.py           # shared finite / monotonic time and identifier validation
├── tests/                      # peer state, failure detection, task claims/ownership,
│   │                           #   communication graph, transport, integration, packaging
│   └── ...
├── scripts/
├── examples/isr_replay.jsonl    # synthetic two-source observation replay
│   ├── pitch-demo.ps1                     # runs both quorum-floor scenarios, no activation needed
│   └── sitl-copter.sh                     # starts one headless ArduCopter SITL instance
├── docs/
│   ├── README.md                          # index: which document answers which question
│   ├── onboarding.md                      # start here: setup, reading order, the rules
│   ├── measured_results.md                # reproducible sweep results, and their stated limits
│   ├── generalization_study.md            # paired fleet/network comparison
│   ├── robustness_study.md                # layout/fault-time sensitivity
│   ├── demo_runbook.md                    # repeatable hackathon demonstration
│   ├── observation_integration.md         # standalone ISR replay and proposed integration boundary
│   ├── threat_context.md                  # cited GNSS interference context, and what it does not show
│   ├── distributed_state_foundation.md   # state boundary, EFSM, quorum, leases, reconciliation
│   ├── autonomy_efsm.md                   # generated transition tables + composition/locality rules
│   ├── vehicle_model.md                   # open lane: kinematics, energy, calibration
│   ├── sitl.md                            # software-in-the-loop plan, and what it must not cost
│   ├── milestones.md                      # development history: what was built, in what order
│   ├── prior_art.md                       # evidence-bounded research/industrial comparison
│   └── history/                           # dated records: per-prototype design notes, quality history
├── pyproject.toml              # packaging, tool config; zero runtime dependencies
├── CHANGELOG.md  CONTRIBUTING.md  SECURITY.md
└── .github/workflows/ci.yml    # ruff + pyright + pytest/coverage + build on Python 3.11 & 3.13
```

Key areas:

| Concern | Files |
| --- | --- |
| Distributed peer state & failure detection | `peer_state.py`, `failure_manager.py` |
| Distributed task intent, ownership, and execution | `task_utility.py`, `task_claims.py`, `mission.py` |
| Receiver-local discrete autonomy | `autonomy.py` |
| Communication topology | `communication.py` |
| Messaging / transport | `messaging.py` |
| Simulations & demos | `simulation.py`, `task_claim_demo.py` |
| Tests | `tests/` |
| Software in the loop (optional) | `sitl.py`, `geodesy.py` |
| Design docs | `docs/` |

## Running the Project

Supported Python: **3.11+** (CI covers 3.11 and 3.13; 3.12 is also classified).
The headless simulator has **no runtime dependencies**; `pytest`, coverage,
Ruff, Pyright, and `build` come from the `dev` extra.

### Environment setup and installation

POSIX shell:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Optional extra: `python -m pip install -e ".[dev,charts]"`
(`charts` = Plotly, for the offline lease-timeout chart).

### Run the baseline simulation

```bash
python -m eudis_swarm.simulation        # or the installed console script: eudis-swarm
python -m eudis_swarm.simulation --help
```

### Run the distributed task-claim partition demo

```bash
python -m eudis_swarm.task_claim_demo
python -m eudis_swarm.task_claim_demo --record-trace task-claims.trace.json
```

### Tests, lint, and type-check

```bash
python -m pytest
python -m pytest --cov=eudis_swarm --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m pyright
python -m build
```

### Named demonstration scenarios

Each story is one command rather than eight flags. Presets override the
matching options and leave every default untouched.

```bash
eudis-swarm --scenario demo-quorum-floor      # two aircraft, one stops, distributed finishes
eudis-swarm --scenario baseline-quorum-floor  # the same loss under central control: never finishes
eudis-swarm --scenario demo-failstop          # a UAV really dies; the quorum notices
eudis-swarm --scenario demo-partition         # a balanced 2+2 split that heals
eudis-swarm --scenario demo-amendment         # operator intent diverges mid-partition
eudis-swarm --scenario baseline               # the same split under central control
```

`baseline-quorum-floor` is the one preset that deliberately fails, and it exits
non-zero for that reason. A test asserts it, so a change that let it succeed
would break the build rather than quietly erase the result.

### Parameter sweeps

```bash
eudis-swarm-sweep --param lease-timeout --from 0.5 --to 6.0 --steps 12 \
  --seeds 20 --communication-range 50 --output sweep.csv
```

One CSV row per (parameter value, seed). The same seeds reproduce
byte-identical output.

### Trace recording (optional)

```bash
eudis-swarm --record-trace trace.json
```

Records a schema-versioned JSON trace of every mission/communication boundary
— world state, receiver-local peer knowledge, and ownership views — for
offline inspection or tooling. There is currently no bundled viewer; see
`trace.py` for the schema.

## Roadmap

Mission Contract + Mission EFSM is implemented; see
[Milestone 5](docs/milestones.md). Controlled experiments are partially
covered: `eudis-swarm-sweep` sweeps lease policy, communication range, failure
timeout and peer-state staleness against both control modes. A separate
[paired fleet/network comparison](docs/generalization_study.md) checks two
fleet sizes under three link conditions. A separate
[sensitivity check](docs/robustness_study.md) changes initial layout and fault
timing. RF validation is not started.

For the hackathon, first freeze the reproducible coordination comparison and
recorded-trace presentation. A standalone recorded-observation replay with
receiver-local timeouts is implemented; link-mediated delivery into the simulator
and task discovery are future work. The following are longer-term plans, **not yet
implemented**:

1. **Coordinated search** — a deterministic execution baseline followed by a
   separately evaluated distributed receding-horizon approach. Ownership answers
   who may act; search planning answers how an owned objective is executed.
2. **Role EFSM + connectivity support** — evidence- and utility-driven role
   changes, never the shortcut “poor link implies relay.”
3. **Cyber-physical topology repair and handoff** — close the
   network→knowledge→decision→movement loop, then add an explicit non-weapon
   track/observation handoff protocol.
4. **Independent local safety/deconfliction supervisor** — spatial separation
   that never waits for distributed consensus.
5. **Controlled experiments and RF validation** — sweep delivery loss, jammer
   duration, topology, lease policy, clocks, and message load against centralized
   and distributed baselines. The analytical radio abstraction must not be
   described as calibrated RF until measurements support that claim.
6. **Quantum-simulated initial planning benchmark** — QUBO/QAOA candidate plans
   compared with greedy, heuristic, and exact classical methods where feasible;
   never runtime ownership authority.
7. **Connect the existing standalone ArduPilot SITL vehicle to the simulator's
   backend boundary**, followed only later by Orounda
   georeferencing and controlled mixed SITL/physical-UAV validation.
8. **Larger-swarm scaling and tactical/3-D visualization** after the protocol,
   mission, search, role, and safety boundaries are stable.

## EUDIS Demonstration Direction

The [runbook](docs/demo_runbook.md) describes the current repeatable simulator
demo. The following are possible future validation steps (**planned, not
implemented**):

- an **ArduPilot SITL** swarm running the coordination logic;
- deployment at the **USRL Orounda Airfield** site;
- **optional partner-provided physical UAV(s)** alongside SITL aircraft;
- **mixed physical + SITL** operation in one scenario;
- a **controlled software network partition** injected during the run;
- a **tactical display** showing each UAV's local beliefs and task ownership;
- a **baseline vs. resilient comparison** of mission outcomes.

## License / Status

This repository has **no `LICENSE` file**. The source carries an explicit
`Copyright 2026 Charalampos Nadiotis` notice; all rights are reserved and no
open-source licence is granted or implied by access to the repository. The
current repository is private; visibility and licensing require an explicit
decision before public distribution.

**Status:** active hackathon R&D / prototype work. Versions track prototype
milestones, not a stable API — interfaces change between prototypes.

**Not flight software.** The core models UAV coordination algorithmically in
two dimensions; it has no guidance, navigation, control, targeting, validated
RF propagation, or safety-critical functionality. The optional `SitlVehicle`
utility commands one ArduPilot software-in-the-loop instance, but it is not
connected to `Simulation` and has not been validated for physical vehicles.
The optional free-space link equation is a deterministic/seeded simulation
abstraction, not a field radio model. The project must not be deployed on,
integrated into, or used to command any real vehicle. See
[`docs/distributed_state_foundation.md`](docs/distributed_state_foundation.md)
for the modelling boundary and the exhaustive list of current limitations in the
per-prototype design notes.
