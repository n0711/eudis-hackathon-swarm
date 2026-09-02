# eudis-swarm

**eudis-swarm is a deterministic, dependency-free Python simulator for a
communications-aware autonomous UAV swarm.** It models what happens when radio
contact splits a swarm into disconnected groups: each UAV keeps working from its
own local knowledge, and conflicting task ownership is reconciled autonomously
once connectivity returns — with no central swarm controller in the loop.

Every result below is reproducible from a fixed command line. There is no
wall-clock time, no unseeded randomness, and no order-dependent iteration in the
simulator, so the same commands produce the same numbers on any machine.

[![CI](https://github.com/n0711/eudis-swarm/actions/workflows/ci.yml/badge.svg)](https://github.com/n0711/eudis-swarm/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Coverage 89%](https://img.shields.io/badge/coverage-89%25-brightgreen)](#verification)
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
        |     (real positions, CommunicationGraph, Task records, metrics, traces)
        |
        +-- Agent 1
        |    +-- PeerStateStore     (delivered heartbeats, freshness, link evidence, declarations)
        |    +-- TaskClaimStore     (immutable claims, leases, epochs, reconciliation)
        |
        +-- Agent 2
        |    +-- PeerStateStore
        |    +-- TaskClaimStore
        |
        +-- Agent N
             +-- PeerStateStore
             +-- TaskClaimStore
```

All inter-agent information passes through the modelled communication graph.
`PeerStateTransport` and `TaskClaimTransport` deliver a message to a receiver
only when that receiver has an **active one-hop link** to the source; there is no
routing, forwarding, retry queue, latency, or stochastic loss model.

Three kinds of state are kept strictly separate:

| Layer | Holds | Must not |
| --- | --- | --- |
| **World truth** | real `Agent` positions and responsiveness, `Task` records, the live `CommunicationGraph`, `Mission` lifecycle | hand a remote UAV's live state directly to that UAV's decision logic |
| **Receiver-local belief** | one UAV's `PeerStateStore` and `TaskClaimStore`: delivered snapshots, local link evidence, received votes/declarations, own claim/release/completion evidence | infer physical failure from silence, or read another UAV's authoritative `Agent` / `Task` / `Mission` state |
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
  are exchanged over the same modelled one-hop links as heartbeats. Each
  receiver owns an isolated vote mailbox; locally originated declaration
  certificates are retained and retried over links that come back up.
- **Receiver-local peer state.** One `PeerStateStore` per UAV holds delivered
  `Heartbeat` snapshots, their receiver-local arrival time, per-peer link
  evidence, and any applied declaration. Nothing is copied in from world truth.
- **Freshness vs. status, kept apart.** `PeerKnowledgeState` is snapshot age
  (`UNKNOWN` / `FRESH` / `STALE`). `PeerStatus` interprets all local evidence as
  `HEARD`, `SILENT`, `UNREACHABLE`, or `DECLARED_FAILED`.
- **Communication loss does not imply UAV failure.** `STALE`, `UNKNOWN`, and
  `UNREACHABLE` never become `FAILED`. A blocked but healthy UAV keeps moving
  and completing tasks, and releases no work. Tests assert this directly.
- **Quorum-based failure declaration.** For `N` configured UAVs the threshold is
  `required_votes = max(2, floor((N - 1) / 2) + 1)` — a strict majority of the
  possible voters with a two-vote floor. Swarms of fewer than three UAVs cannot
  declare a failure. The declaring replica must be one of the matching voters.
- **Arbitrary blocked links and balanced partitions.** The graph accepts a
  canonical set of undirected `blocked_links` alongside the existing whole-UAV
  fault; `can_deliver()` drives deterministic single-link, balanced 2+2, and
  reconnection cases.
- **Reconnect handling.** A restored link starts a fresh grace interval
  (`reachable_since`) before suspicion can resume, so an old stale observation
  cannot trigger a vote the instant a link returns. Stale votes age out of the
  quorum so an old partial quorum never lives forever.
- **No authoritative remote-position leakage.** Non-participating UAV software
  does not advance or receive updates to its private freshness and link state,
  and the connectivity-aware allocator scores only from `HEARD` peer snapshots.
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
  store reports `COMPLETE` and accepts no further claims or releases for it.
- **Message-order independence and duplicate tolerance.** Applying a claim is
  idempotent; an obsolete or lower epoch is stored for audit without becoming
  current again. Delivery order does not change the converged result.
- **Receiver-local claim evidence only.** The observer-only claim trace records
  every agent-by-task view without ever emitting `Task.assigned_agent`.

**Lowest-owner-ID reconciliation is a correctness mechanism, not a
utility-optimal allocator.** It guarantees that every replica converges on one
owner; it does not attempt to pick the *best* owner for mission utility. A
receiver-local utility layer is future work (see [Roadmap](#roadmap)).

## Task Ownership EFSM

`TaskOwnershipState` has exactly six states:

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
5. **Reconnection.** Cross links are restored at `t ≈ 3.6`; the next delivery
   (`t = 3.7`) makes both still-valid claims visible, so **all four stores**
   report `CONTESTED`.
6. **Independent reconciliation.** At `t = 4.0` every store runs `reconcile()`
   and independently selects **UAV 1** (lowest owner ID). UAV 4 is the local
   loser and emits an exact-generation release.
7. **Convergence.** The release propagates (`t = 4.2`); every store now reports
   `known_owner_agent_id == 1` and `contested == False`.
8. **Mission continues.** UAV 4 immediately claims **Task 29** at `t = 4.5`
   without a restart. The run emits a 10-frame observer-only trace.

CLI summary line from the demo:

```text
Task-claim demo: 2+2 partition, Task 19 -> UAV 1, UAV 4 released and claimed Task 29; 10 trace frames.
```

## Verification

Re-run on this working tree with Python 3.13, dependency-free simulator:

| Gate | Command | Result |
| --- | --- | --- |
| Lint | `python -m ruff check .` | pass — all checks passed |
| Format | `python -m ruff format --check .` | pass — 50 files already formatted |
| Types | `python -m pyright` | pass — 0 errors, 0 warnings, 0 informations |
| Tests + coverage | `python -m pytest --cov=eudis_swarm --cov-report=term-missing` | **179 passed**, **89.12%** branch coverage (threshold 85%) |
| Package build | `python -m build` | pass — builds `sdist` + `wheel` for `eudis_swarm-0.3.0a0` |

The GitHub Actions workflow runs the same lint, format, type, test/coverage, and
build steps on Python 3.11 and 3.13 for pushes and pull requests against `main`,
then installs the built wheel and smoke-tests `eudis-swarm --help` / `--version`.

## Repository Structure

```text
eudis-swarm/
├── src/eudis_swarm/
│   ├── communication.py        # distance + blocked-link comms graph, components, partitions, can_deliver()
│   ├── messaging.py            # one-hop transports: PeerStateTransport, TaskClaimTransport
│   ├── peer_state.py           # per-UAV PeerStateStore: freshness, link evidence, PeerStatus, declarations
│   ├── failure_manager.py      # isolated detector replicas, FailureVote / FailureDeclaration, quorum
│   ├── task_claims.py          # per-UAV TaskClaimStore: immutable claims, leases, epochs, reconciliation
│   ├── task.py                 # authoritative Task + the six-state TaskOwnershipState vocabulary
│   ├── task_allocator.py       # centralized distance baseline + connectivity-aware reference policy
│   ├── mission.py              # authoritative world mutation, mission lifecycle, invariants
│   ├── simulation.py           # scenario generation, logical clock, physics, orchestration, CLI (eudis-swarm)
│   ├── simulation_events.py    # structured comms / peer-knowledge / task-claim event types
│   ├── metrics.py              # separate physical-mission and network metric blocks
│   ├── trace.py                # versioned JSON playback frames for the dashboard
│   ├── task_claim_trace.py     # observer-only agent-by-task ownership trace (no authoritative fields)
│   ├── task_claim_demo.py      # deterministic 2+2 split-brain / reconcile / continue demo + CLI
│   ├── agent.py                # world-truth UAV state, 2D point-mass motion, immutable heartbeats
│   ├── config.py               # validated physical + communication configuration
│   ├── validation.py           # shared finite / monotonic time and identifier validation
│   ├── dashboard.py            # optional Streamlit/Plotly trace-playback entry point (eudis-swarm-dashboard)
│   ├── dashboard_app.py        # optional dashboard implementation
│   └── visualization.py        # optional legacy matplotlib final-frame view
├── tests/                      # 179 tests: peer state, failure detection, task claims/ownership,
│   │                           #   communication graph, transport, integration, packaging
│   └── ...
├── docs/
│   ├── distributed_state_foundation.md   # state boundary, EFSM, quorum, leases, reconciliation
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
| Distributed task ownership | `task_claims.py`, `task_claim_trace.py` |
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

1. **Receiver-local utility / distributed task allocation** — a local layer that
   creates claim intents from mission utility instead of relying on
   lowest-owner-ID reconciliation for allocation.
2. **Experiment metrics and baseline comparison** — quantified resilient-vs-baseline
   outcomes across partition scenarios.
3. **Coordinated search and target hand-off** between UAVs.
4. **Deconfliction** — spatial / task deconfliction between agents.
5. **ArduPilot SITL** integration for a software-in-the-loop swarm.
6. **Orounda-georeferenced simulation** — scenarios on the real airfield geometry.
7. **Tactical / 3D visualisation** of local beliefs and task ownership.
8. **Quantum-simulated reconciliation / allocation benchmark** — a QUBO-style
   comparison against the deterministic mechanism.
9. **Mixed SITL + physical UAV operation.**
10. **Larger-swarm scaling** beyond the current small deterministic scenarios.

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
targeting, RF-propagation, or safety-critical functionality, and no interface to
any real vehicle, radio, or autopilot stack. It must not be deployed on,
integrated into, or used to command any real vehicle. See
[`docs/distributed_state_foundation.md`](docs/distributed_state_foundation.md)
for the modelling boundary and the exhaustive list of current limitations in the
per-prototype design notes.
