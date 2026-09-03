# eudis-swarm

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

[![CI](https://github.com/n0711/eudis-swarm/actions/workflows/ci.yml/badge.svg)](https://github.com/n0711/eudis-swarm/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Coverage gate 85%](https://img.shields.io/badge/coverage%20gate-85%25-brightgreen)](#verification)
[![Checked with Pyright](https://img.shields.io/badge/types-pyright-informational)](https://microsoft.github.io/pyright/)

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
| **Observer / evaluation** | metrics, traces, the playback dashboard, test assertions comparing belief against truth | feed its privileged view back into allocation or failure decisions |

## Implemented Milestones

Everything in this section is implemented and regression-tested. Planned work is
in the [Roadmap](#roadmap).

### Milestone 1 — Distributed State Hardening

Modules: [`communication.py`](src/eudis_swarm/communication.py),
[`messaging.py`](src/eudis_swarm/messaging.py),
[`peer_state.py`](src/eudis_swarm/peer_state.py),
[`failure_manager.py`](src/eudis_swarm/failure_manager.py),
[`task.py`](src/eudis_swarm/task.py).

- **Graph-mediated failure protocol.** `FailureVote` and `FailureDeclaration`
  are immutable evidence flooded across the currently connected multi-hop
  component. Each receiver owns an isolated vote mailbox; evidence retains its
  origin while intermediate UAVs only forward it. Locally originated votes and
  declaration certificates are retained for deterministic retry.
- **Receiver-local peer state.** One `PeerStateStore` per UAV holds delivered
  `Heartbeat` snapshots, their receiver-local arrival time, and any applied
  declaration. Nothing is copied in from world truth — in particular there is
  no per-peer "is the link up" flag, because a radio cannot supply one.
- **Freshness vs. status, kept apart.** `PeerKnowledgeState` is snapshot age
  (`UNKNOWN` / `FRESH` / `STALE`). `PeerStatus` interprets all local evidence as
  `HEARD`, `SILENT`, or `DECLARED_FAILED`.
- **Silence is ambiguous, and the swarm can be wrong.** Nothing local
  distinguishes a jammed peer from a destroyed one, so a healthy but
  partitioned UAV *can* be declared dead by a quorum. The defence is not
  privileged knowledge but quorum, reversible declarations, and ownership
  reconciliation — and the cost of being wrong is measured, not hidden.
- **A declaration is belief, not a kill switch.** It never touches
  `responsive`: a wrongly declared UAV keeps flying, keeps transmitting, and
  keeps the task it does not know it lost. First-hand contact retracts the
  declaration when the link returns.
- **Quorum-based failure declaration.** For `N` configured UAVs the threshold is
  `required_votes = max(2, floor((N - 1) / 2) + 1)` — a strict majority of the
  possible voters with a two-vote floor. Swarms of fewer than three UAVs cannot
  declare a failure. The declaring replica must be one of the matching voters.
- **Arbitrary blocked links and balanced partitions.** The graph accepts a
  canonical set of undirected `blocked_links` alongside the existing whole-UAV
  fault; `can_deliver()` drives deterministic single-link, balanced 2+2, and
  reconnection cases.
- **Reconnect handling.** Restoring a link is world truth and changes no local
  belief by itself. A successfully delivered heartbeat restarts that receiver's
  silence interval. Failure-vote age is measured from receiver-local receipt, so
  an old partial quorum does not live forever and a duplicate does not refresh it.
- **No authoritative remote-position leakage.** Non-participating UAV software
  does not advance or receive updates to its private freshness state, and the
  connectivity task utility scores only from `HEARD` peer snapshots.
- **Six task-ownership states locked in.** `TaskOwnershipState` fixes the
  vocabulary — `UNCLAIMED`, `OWNED_BY_SELF`, `CLAIMED_BY_PEER_FRESH`,
  `CLAIMED_BY_PEER_STALE`, `CONTESTED`, `COMPLETE` — used by Milestone 2.

### Milestone 2 — Distributed Task Ownership

Modules: [`task_claims.py`](src/eudis_swarm/task_claims.py),
[`messaging.py`](src/eudis_swarm/messaging.py) (`TaskClaimTransport`),
[`task_claim_trace.py`](src/eudis_swarm/task_claim_trace.py),
[`task_claim_demo.py`](src/eudis_swarm/task_claim_demo.py).

- **Independent `TaskClaimStore` per UAV.** Each store derives its six-state
  interpretation only from local claims and delivered claim / release /
  completion evidence. It never reads `Task`, remote `Agent`, or `Mission`.
- **Immutable claims.** `TaskClaim`, `TaskClaimRelease`, and
  `TaskCompletionEvidence` are frozen dataclasses. A claim's identity is the
  structural tuple `(task_id, owner_agent_id, epoch)`; two records with the same
  identity cannot disagree.
- **Fresh → stale → expired lease semantics.** For claim age `a` at the
  receiver: `FRESH` while `a <= freshness_timeout`, `STALE` while
  `freshness_timeout < a <= lease_timeout`, `EXPIRED` once `a > lease_timeout`.
  A `STALE` claim is still lease-valid; a new competing claim may be created
  only once the locally known claim is `EXPIRED`.
- **Epochs scoped to `(task, owner)`.** Renewal advances an owner-local epoch.
  `newest_claims_by_owner` keeps each owner's highest epoch; epochs are compared
  only within one owner and never across owners.
- **Split-brain ownership is representable.** After a partition, two stores can
  each report `OWNED_BY_SELF` for the same task — `owns_task()` returns `True`
  on both sides. This is an allowed state, not a bug.
- **`CONTESTED`.** When delivery makes more than one valid current owner visible
  to a replica, that replica reports `CONTESTED` for the task.
- **Deterministic reconciliation.** `reconcile()` resolves an already-visible
  contest with `select_winning_claim`, which normalises to each owner's newest
  epoch and then picks the **lowest stable owner ID**
  (`claim_reconciliation_key` is the owner ID alone). Every replica reaches the
  same winner from protocol evidence only.
- **Local losing claimant releases.** If the local UAV is the loser,
  `reconcile()` emits an exact-generation `TaskClaimRelease` naming the winning
  claim; the losing UAV stops acting as owner.
- **`COMPLETE` is absorbing.** Once completion evidence exists for a task, the
  store reports `COMPLETE`. Later claims and releases may remain recorded for
  audit, but none can change that terminal interpretation.
- **Message-order independence and duplicate tolerance.** Applying a claim is
  idempotent; an obsolete or lower epoch is stored for audit without becoming
  current again. Delivery order does not change the converged result.
- **Receiver-local claim evidence only.** The observer-only claim trace records
  every agent-by-task view without ever emitting `Task.assigned_agent`.
- **Connected-component dissemination.** Claims, releases, and completions can
  traverse intermediate UAVs. Structural IDs and receiver-local seen state make
  retransmission duplicate-tolerant and loop-safe; forwarding never rewrites the
  owner or claim generation.

**Lowest-owner-ID reconciliation is a correctness mechanism, not a
utility-optimal allocator.** It guarantees that replicas with the same valid
evidence select one owner; stable multi-hop connectivity lets that evidence
converge. Utility decides which unclaimed objective a UAV tries first;
reconciliation decides which simultaneous claimant remains authorized.

### Milestone 3 — Receiver-local Task Control

Modules: [`task_utility.py`](src/eudis_swarm/task_utility.py),
[`simulation.py`](src/eudis_swarm/simulation.py), and
[`mission.py`](src/eudis_swarm/mission.py).

- **Immutable task discovery boundary.** Mission setup converts the configured
  task IDs and positions into frozen `TaskObjective` values. Every UAV ranks this
  catalogue without consulting mutable `Task.status` or `Task.assigned_agent`.
  Dynamic sensor discovery is not modeled yet; the current catalogue is
  preloaded mission input.
- **Receiver-local additive utility.** `ReceiverLocalTaskUtility` ranks by
  weighted travel, resource, communication, and role costs, with `(total_cost,
  task_id)` as the deterministic order. The default is distance only. Resource
  and role terms are validated extension points and default to zero.
- **Local connectivity option.** `--allocation-policy connectivity` changes the
  local utility weights and derives communication cost only from that UAV's
  `HEARD` snapshots and their delivered positions. It never queries
  `CommunicationGraph` or another live `Agent`.
- **Claim before bind.** Each responsive idle UAV chooses from its own
  locally-claimable objectives. Choices are batched from pre-mutation views,
  materialized as claims, gossiped, reconciled, and only then bound as execution
  intent when the same local store reports `OWNED_BY_SELF`.
- **One execution predicate.** `Mission.task_is_executable()` requires local
  `owns_task()`. Actual motion, projected motion used by communication-only
  boundaries, renewal, and completion all pass through that predicate. A bare
  `Agent.current_task` cannot move, project, renew, or complete anything;
  `current_task` is only a cache of the currently selected local intent.
- **Stand down and replan.** Release, strict lease expiry, losing deterministic
  reconciliation, or learned completion removes execution authority. The UAV
  clears the cached intent before another physical action and may choose another
  locally available objective in the same deterministic claim round.
- **Failure respects leases.** A fail-stop UAV stops participating and therefore
  stops renewing. Peers retain its claim through the stale-but-valid interval;
  a replacement UAV may claim only after its own received copy crosses strict
  lease expiry.
- **Projection is not authority.** Mutable `Task` fields are maintained for
  mission reporting and visualization. Corrupting that projection cannot create
  a claim, authorize execution, defeat terminal completion evidence, or choose a
  claimant.
- **Centralized policies are baselines.** `TaskAllocator` and
  `CommunicationAwareTaskAllocator` remain available for comparison. The normal
  `Simulation` enables distributed task control and never calls
  `Mission.allocate_tasks()`.

The simulation still runs all replicas in one deterministic process, but that
orchestrator is a physics/test harness: it batches independent local choices and
does not supply global task ownership or graph reachability to those choices.

### Milestone 4 — Formal Receiver-local Autonomy EFSMs

Modules: [`autonomy.py`](src/eudis_swarm/autonomy.py),
[`trace.py`](src/eudis_swarm/trace.py), and the generated
[`autonomy_efsm.md`](docs/autonomy_efsm.md) reference.

- **Pure transition relation.** Each transition has finite control state, frozen
  typed extended variables, a typed event, an explicit guard/reason, and ordered
  requested effects. Calling a reducer never moves an aircraft, changes network
  truth, sends a packet, mutates a peer, or executes an effect.
- **Orthogonal machines.** Every UAV composes one `ContactEFSM` and one preserved
  `PeerAvailabilityEFSM` mapping per peer, one `TaskOwnershipEFSM` conformance
  view per task, and one `CoordinationModeEFSM`. There is no global cross-product
  or requirement that different UAVs occupy the same mode.
- **Receiver-local contact inference.** `UNKNOWN`, `ACTIVE`, `DEGRADED`, `LOST`,
  and `RECOVERING` are derived from successful local receipts and elapsed local
  time. Recovery requires observations at distinct local times. A physical link
  restoration, simulator SNR value, component change, or failed delivery is not
  a contact event.
- **Epistemic coordination posture.** Each UAV independently moves among
  `COOPERATIVE`, `DEGRADED`, `LOCAL_AUTONOMY`, and `RECONCILING` from its composed
  local contact states and locally visible task contests. The mode is advisory
  in this milestone; it does not override the existing claim authority.
- **Effects are requests.** Values such as `ENTER_LOCAL_AUTONOMY`,
  `REQUEST_RECONCILIATION`, `STAND_DOWN_TASK`, and `RESUME_COOPERATION` are data
  returned by transitions and recorded for inspection. No dispatcher consumes
  them yet; existing claim orchestration continues its already-established
  reconciliation and stand-down actions independently.
- **Structured observability.** Trace schema 3 records per-peer contact variables,
  per-UAV coordination mode, and ordered state changes with timestamp, local
  sequence, previous state, event, next state, guard, reason, and effects. The
  dashboard displays these observer-only records without feeding them back into
  control.
- **Bounded checking.** Tests enumerate bounded event sequences, replay reducers,
  reach every control state, enforce terminal completion, and inspect the
  autonomy module for forbidden world/observer dependencies. Existing multi-hop,
  split-brain, radio, jammer, and distributed-execution tests remain intact.

The physical `RadioModel` still generates world-level delivery outcomes. The
`ContactEFSM` interprets only the evidence a receiving process obtains from
those outcomes; it never receives distance, SNR, BER, jammer state, or graph
reachability.

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

Current milestone result: **322 tests passed**, total branch-aware coverage is
**89.14%**, Pyright reports zero errors, Ruff lint/format checks pass, both
deterministic demos pass, and the source and wheel distributions build cleanly.

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
│   ├── simulation.py           # local claim rounds, logical clock, physics, orchestration, CLI
│   ├── simulation_events.py    # structured comms / peer-knowledge / task-claim event types
│   ├── metrics.py              # separate physical-mission and network metric blocks
│   ├── trace.py                # schema-v3 world/belief/ownership/EFSM playback frames
│   ├── task_claim_trace.py     # observer-only agent-by-task ownership trace (no authoritative fields)
│   ├── task_claim_demo.py      # deterministic 2+2 split-brain / reconcile / continue demo + CLI
│   ├── agent.py                # world-truth UAV state, 2D point-mass motion, immutable heartbeats
│   ├── config.py               # validated physical + communication configuration
│   ├── validation.py           # shared finite / monotonic time and identifier validation
│   ├── dashboard.py            # optional Streamlit/Plotly trace-playback entry point (eudis-swarm-dashboard)
│   ├── dashboard_app.py        # optional dashboard implementation
│   └── visualization.py        # optional legacy matplotlib final-frame view
├── tests/                      # peer state, failure detection, task claims/ownership,
│   │                           #   communication graph, transport, integration, packaging
│   └── ...
├── docs/
│   ├── distributed_state_foundation.md   # state boundary, EFSM, quorum, leases, reconciliation
│   ├── autonomy_efsm.md                   # generated transition tables + composition/locality rules
│   ├── prior_art.md                       # evidence-bounded research/industrial comparison
│   ├── prototype_0_1.md … prototype_0_3a.md
│   ├── visualization_layer.md
│   └── code_quality_and_performance.md
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

Optional extras: `python -m pip install -e ".[dev,visualization,dashboard]"`
(`visualization` = matplotlib final-frame view; `dashboard` = Streamlit/Plotly
trace playback).

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

### Trace-driven playback dashboard (optional)

```bash
eudis-swarm --record-trace trace.json
eudis-swarm-dashboard trace.json
```

## Roadmap

Planned work — **not yet implemented**:

1. **Mission Contract + Mission EFSM** — versioned operator intent, constraints,
   priorities, degradation policy, and explicit mission phases above local
   distributed execution. This is the exact recommended next milestone.
2. **Coordinated search** — a deterministic execution baseline followed by a
   separately evaluated distributed receding-horizon approach. Ownership answers
   who may act; search planning answers how an owned objective is executed.
3. **Role EFSM + connectivity support** — evidence- and utility-driven role
   changes, never the shortcut “poor link implies relay.”
4. **Cyber-physical topology repair and handoff** — close the
   network→knowledge→decision→movement loop, then add an explicit non-weapon
   track/observation handoff protocol.
5. **Independent local safety/deconfliction supervisor** — spatial separation
   that never waits for distributed consensus.
6. **Controlled experiments and RF validation** — sweep delivery loss, jammer
   duration, topology, lease policy, clocks, and message load against centralized
   and distributed baselines. The analytical radio abstraction must not be
   described as calibrated RF until measurements support that claim.
7. **Quantum-simulated initial planning benchmark** — QUBO/QAOA candidate plans
   compared with greedy, heuristic, and exact classical methods where feasible;
   never runtime ownership authority.
8. **ArduPilot SITL and vehicle adapters**, followed only later by Orounda
   georeferencing and controlled mixed SITL/physical-UAV validation.
9. **Larger-swarm scaling and tactical/3-D visualization** after the protocol,
   mission, search, role, and safety boundaries are stable.

## EUDIS Demonstration Direction

Intended future validation (**planned, not implemented**):

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
open-source licence is granted or implied by the repository being public. It is
published for review.

**Status:** active hackathon R&D / prototype work. Versions track prototype
milestones, not a stable API — interfaces change between prototypes.

**Not flight software.** This project models UAV coordination algorithmically in
two dimensions. It contains no autopilot, guidance, navigation, control,
targeting, validated RF propagation, or safety-critical functionality, and no
interface to any real vehicle, radio, or autopilot stack. Its optional free-space
link equation is a deterministic/seeded simulation abstraction, not a field radio
model. The project must not be deployed on,
integrated into, or used to command any real vehicle. See
[`docs/distributed_state_foundation.md`](docs/distributed_state_foundation.md)
for the modelling boundary and the exhaustive list of current limitations in the
per-prototype design notes.
