<h1>EUDIS Swarm</h1>

**EUDIS Swarm is a deterministic simulator for UAV teams that must keep working
when the radio does not. It models resilient coordination while keeping
simulation truth separate from what each UAV has actually learned.**

[![CI](https://github.com/n0711/eudis-swarm/actions/workflows/ci.yml/badge.svg)](https://github.com/n0711/eudis-swarm/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Coverage 89%](https://img.shields.io/badge/coverage-89%25-brightgreen)](#run-the-tests)
[![Checked with Pyright](https://img.shields.io/badge/types-pyright%20strict--standard-informational)](https://microsoft.github.io/pyright/)

Most multi-UAV coordination work assumes reliable communication and treats a
silent aircraft as a lost one. In a contested electromagnetic environment both
assumptions are wrong, and the second one is expensive: a jammed UAV that is
still flying, still working, and still completing tasks gets written off, and
its work gets needlessly reassigned.

This repository holds the central claim as an enforced invariant:

```text
COMMUNICATION LOSS != UAV FAILURE
```

Physical state (`IDLE` / `ACTIVE` / `FAILED`), network reachability,
receiver-local snapshot freshness (`UNKNOWN` / `FRESH` / `STALE`), and
receiver-local peer status (`HEARD` / `SILENT` / `UNREACHABLE` /
`DECLARED_FAILED`) are separate dimensions. Receiver-local task ownership adds
`UNCLAIMED`, `OWNED_BY_SELF`, fresh/stale peer ownership, `CONTESTED`, and
terminal `COMPLETE` without copying authoritative task state. A peer becomes
`DECLARED_FAILED` only through the graph-mediated vote-and-quorum protocol, not
because a heartbeat went missing.

## Why this is not another swarm demo

Every result published in this README is reproducible from a documented command
line, to the exact timestamp. There is no wall-clock time, no unseeded
randomness, and no order-dependent iteration anywhere in the simulator. Run the
commands below on any machine and you get the same numbers — which is the
property that makes a coordination algorithm verifiable rather than merely
demonstrable.

| | |
| --- | --- |
| Runtime dependencies | **None** — Python standard library only |
| Tests | **179** passing, **89.12%** branch coverage |
| Type checking | Pyright, zero errors |
| Determinism | Bit-reproducible across machines and Python 3.11–3.13 |

## Quickstart

```bash
git clone https://github.com/n0711/eudis-swarm.git
cd eudis-swarm
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m eudis_swarm.simulation
```

On Windows PowerShell, substitute `py -3.11 -m venv .venv` and
`.\.venv\Scripts\Activate.ps1`.

That runs the baseline scenario: four UAVs, twenty tasks, and one physical
fail-stop injected at `t=4.00 s`. Surviving replicas form a quorum-backed
failure declaration at `t=5.75 s`; the mission releases the orphaned task,
reassigns it, and completes all twenty tasks at `t=17.25 s` with
`Human interventions: 0`.

## The result that matters

Identical mission inputs, one flag different — the allocation policy:

```bash
python -m eudis_swarm.simulation --seed 1 --failure-time 100 --communication-range 35 --allocation-policy distance
python -m eudis_swarm.simulation --seed 1 --failure-time 100 --communication-range 35 --allocation-policy connectivity
```

| Result | Distance-greedy | Connectivity-aware |
| --- | ---: | ---: |
| Tasks completed | `20 / 20` | `20 / 20` |
| Mission duration | **`12.00 s`** | `17.25 s` |
| Isolation events | `4` | **`1`** |
| Link losses / restorations | `4 / 5` | `5 / 8` |

Both finish the mission. One finishes faster; the other keeps the swarm
connected. **Neither of those points was chosen** — they fall out of how each
policy ranks candidates, and there is currently no way to ask for a point
between them. Closing that gap is the active line of work; see the
[roadmap](#roadmap).

For each candidate in the centralized reference policy, the connectivity
prediction uses only that UAV's own `HEARD` peer snapshots delivered over active
one-hop links. It never reads another UAV's authoritative position, although
the still-central allocator does receive authoritative candidate self-state and
global tasks.

## Where to go next

| If you want to | Read |
| --- | --- |
| Understand the current state boundary | [`docs/distributed_state_foundation.md`](docs/distributed_state_foundation.md) — world truth, local belief, delivery, quorum, claim leases, and reconciliation |
| See the swarm move | [`docs/visualization_layer.md`](docs/visualization_layer.md) — trace playback dashboard |
| Understand failure recovery | [`docs/prototype_0_1.md`](docs/prototype_0_1.md) |
| Understand the comms graph | [`docs/prototype_0_2a.md`](docs/prototype_0_2a.md) |
| Understand peer knowledge | [`docs/prototype_0_2b.md`](docs/prototype_0_2b.md) |
| Understand the allocator | [`docs/prototype_0_3a.md`](docs/prototype_0_3a.md) |
| Contribute | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Know what this cannot do | [Current limitations](#current-limitations) — read this one |

## Scope and intent

> **This is a simulation, and it is not flight software.** The prototypes are
> algorithmic two-dimensional point-mass models. The distance threshold and
> instantaneous one-hop state delivery are deliberate abstractions, not RF
> propagation or network-protocol results. There is no flight dynamics, radio,
> network transport, autopilot, sensor, collision-avoidance, or safety-critical
> modelling of any kind, and nothing here may be deployed on a real vehicle.
> The [Current limitations](#current-limitations) section is exhaustive and
> deliberately unflattering — please read it before drawing conclusions.

Built for the [EUDIS Defence Hackathon 2026](https://eudis-hackathon.eu/)
(Autumn Edition), Swarm Coordination challenge. The repository contains
incremental prototypes, each adding one capability while preserving every
earlier documented result:

- **Prototype 0.1** — detects one fail-stop UAV, releases its unfinished task,
  reallocates it, and finishes the mission without human intervention.
- **Prototype 0.2A** — an explicit, time-varying communications graph and a
  deterministic outage/restoration experiment, observational only.
- **Prototype 0.2A.1** — hardened configuration, logical time, mission
  lifecycle, invariant testing, and automated quality checks.
- **Prototype 0.2B** — active one-hop links deliver immutable state snapshots
  into receiver-local `UNKNOWN` / `FRESH` / `STALE` peer views.
- **Prototype 0.3A** — an optional connectivity-aware allocator that evaluates
  task endpoints using each UAV's own `HEARD` peer snapshots.
- **Distributed-state foundation** — graph-delivered heartbeats,
  receiver-local link/status evidence, quorum-backed failure declarations,
  link-level partitions, and a locked task-ownership vocabulary.
- **Distributed task ownership (current)** — immutable graph-delivered claims,
  owner-scoped renewals, receiver-local leases, visible split-brain,
  deterministic reconciliation, losing release, and monotonic completion.

## Current milestone: distributed task ownership

Milestone 2 adds one `TaskClaimStore` per participating UAV. Each store derives
its six-state task interpretation only from local claims and graph-delivered
claim, release, and completion values; it never reads authoritative `Task`,
remote `Agent`, or `Mission` ownership.

Claims become stale after the freshness threshold but remain lease-valid until
the later lease boundary. Renewals advance an owner-scoped per-task epoch;
epochs suppress old messages from the same owner but are never compared across
owners. When incompatible current owners meet after a partition, every local
replica selects the lowest owner ID from protocol evidence alone. The losing
owner stops acting and publishes an exact-claim release tombstone.

See [Distributed-state foundation](docs/distributed_state_foundation.md) for the
information-flow boundary, EFSM, exact boundary conditions, reconciliation
rule, deterministic 2+2 lifecycle, and deliberate limitations.

## What Prototype 0.1 still demonstrates

The original scenario remains intact. Four simulated UAV agents service 20
exploration tasks in a 100 x 100 mission area. Agents receive tasks using a
deterministic nearest-distance baseline and move toward them at constant speed.
They publish periodic in-process heartbeats containing position, physical
state, current task, and logical timestamp.

At `t=4.00 s`, UAV 2 is physically fail-stopped and therefore emits no further
heartbeats. Once receiver-local evidence is strictly older than the configured
timeout, surviving peers exchange failure votes; a local replica forms a
declaration from at least two voters and a strict majority of the possible
non-target voters before `Mission` marks it `FAILED`, releases Task 19, later
assigns that task to UAV 1, and completes all 20 tasks at `t=17.25 s`.

See [the Prototype 0.1 technical design](docs/prototype_0_1.md) for the preserved
physical state model, scheduler, failure recovery sequence, and original test
coverage.

## What Prototype 0.2A adds

Prototype 0.2A derives an undirected graph from current UAV positions:

```text
G(t) = (V, E(t))

V    = every configured UAV ID
E(t) = every currently available undirected UAV pair
```

For a canonical pair `i < j`, its Euclidean distance is:

```text
d(i, j) = sqrt((x_j - x_i)^2 + (y_j - y_i)^2)
```

The pair is available exactly when `d(i, j) <= communication_range`, neither
endpoint is blocked by the compatibility-oriented whole-agent fault, and its
canonical pair is not in `blocked_links`. The graph reports all pair records,
active links, neighbors, connected components, isolated UAVs, and whether the
network is fully connected. Updates compare consecutive snapshots and emit only
meaningful link, isolation, partition, and reconnection transitions.

Physical and communication states are deliberately independent:

| Dimension | States and meaning |
| --- | --- |
| Physical agent state | `IDLE`, `ACTIVE`, or `FAILED` |
| Communication state | `REACHABLE` when the UAV has at least one active peer link; `UNREACHABLE` when it is isolated |
| Receiver-local snapshot freshness | `UNKNOWN`, `FRESH`, or `STALE` independently for each remote UAV |
| Receiver-local peer status | `HEARD`, `SILENT`, `UNREACHABLE`, or quorum-backed `DECLARED_FAILED` |

Topology itself still does not use `STALE`; Prototype 0.2B models that separately
as receiver-local information freshness. A UAV in a disconnected component of two or more UAVs remains
reachable to peers in that component even though the whole network is
partitioned.

Most importantly:

```text
COMMUNICATION LOSS != UAV FAILURE
```

A blocked UAV remains physically responsive, continues moving and completing
tasks, and retains its work. Prototype 0.2A originally treated the graph as
observational; the current foundation now uses it for heartbeat and
failure-protocol delivery while preserving the rule that link loss cannot by
itself declare a physical failure.

See [the Prototype 0.2A technical design](docs/prototype_0_2a.md) for the exact
graph contract, timing semantics, metrics, deterministic trace, and limitations.

## What Prototype 0.2B adds

At each existing heartbeat publication time, every responsive UAV produces an
immutable state snapshot. The one-hop transport attempts to deliver that snapshot
to each other UAV and succeeds only when their direct `CommunicationGraph` link
is active. The heartbeat path has no routing, forwarding, retry, queue, latency,
or stochastic loss model.

Each receiver owns an independent peer-state store. An observation is `UNKNOWN`
before first delivery, `FRESH` while its strict age is at most
`peer_state_stale_after`, and `STALE` when:

```text
current_time - received_at > peer_state_stale_after
```

Physical ground truth, graph reachability, and peer-information freshness remain
separate. `STALE`, `UNKNOWN`, and `UNREACHABLE` never imply `FAILED`. Peer views
do not directly release or reassign tasks. In the current foundation, isolated
local failure-detector replicas consume only these delivered observations and
locally exposed link evidence. Votes must cross active modeled links to form a
quorum; locally originated declaration certificates retry over active links,
while `Mission` applies their world-state recovery consequence only once.

See [the Prototype 0.2B technical design](docs/prototype_0_2b.md) for the transport
contract, deterministic outage trace, tests, metrics, and deferred work.

## What Prototype 0.3A adds

The nearest-distance `TaskAllocator` remains the default experimental baseline.
Selecting `--allocation-policy connectivity` uses a second greedy policy. For
each candidate UAV/task pair it predicts direct endpoint connectivity from that
UAV's own `HEARD` peer observations and minimizes:

```text
(predicted_isolation, -predicted_peer_degree, distance, agent_id, task_id)
```

Only `HEARD` peers contribute predicted links. A raw `FRESH` observation is
excluded whenever the peer status is `SILENT`, `UNREACHABLE`, or
`DECLARED_FAILED`; the allocator never reads another UAV's authoritative current
position for this prediction. If all peer knowledge is initially unknown, every
predicted degree is zero and the score naturally falls back to the existing
distance and ID ordering.

The policy affects only new task proposals. `Mission` remains the centralized
authoritative owner of assignment, and active work is never preempted. See
[the Prototype 0.3A technical design](docs/prototype_0_3a.md) for the exact
policy, knowledge boundary, measured comparison, and limitations.

## Architecture

| Module | Responsibility |
| --- | --- |
| `agent.py` | World-level UAV state, constant-speed 2D movement, and immutable heartbeat creation |
| `task.py` | Authoritative `TaskStatus`, exact six-state local ownership vocabulary, and legacy heartbeat classifier |
| `task_claims.py` | Immutable claim/release/completion evidence and one receiver-local lease/reconciliation machine |
| `task_allocator.py` | Centralized distance baseline and local-snapshot-aware connectivity reference policy |
| `failure_manager.py` | Isolated local vote mailboxes, strict-timeout suspicion, quorum validation, and declarations |
| `communication.py` | Undirected distance/link fault policy, canonical `blocked_links`, topology, and transitions |
| `messaging.py` | One-hop delivery of heartbeats, failure evidence, task claims/releases/completions, and local link evidence |
| `peer_state.py` | Receiver-local observations, freshness, link evidence, peer status, and applied declarations |
| `mission.py` | Authoritative world mutation after proposals/declarations, events, and invariants |
| `simulation.py` | Scenario generation, logical clock, physics, fault scheduling, delivery, and protocol orchestration |
| `simulation_events.py` | Structured communication, peer-knowledge, and task-claim event types |
| `metrics.py` | Separate physical-mission and network metrics derived from transitions |
| `config.py` | Validated physical and communication configuration |
| `validation.py` | Shared finite, monotonic logical-time and identifier validation |
| `trace.py` | Immutable playback frames, event explanations, and versioned JSON serialization |
| `task_claim_trace.py` | Observer-only agent-by-task ownership traces with no authoritative task fields |
| `task_claim_demo.py` | Deterministic `{1,2}` / `{3,4}` split-brain, reconnect, reconciliation, and continuation scenario |
| `dashboard_app.py` | Local Streamlit/Plotly mission playback and debugging dashboard |
| `visualization.py` | Legacy optional final matplotlib debugging view |

Allocators only propose assignments; `Mission` applies them and checks
bidirectional world ownership. The optional 0.3A policy reads receiver-local
peer stores, but neither allocator mutates agents or tasks directly. Both
allocation policies remain centralized reference mechanisms and do not resolve
distributed claim conflicts; the new ownership protocol is a separate local
belief/action layer for a later distributed allocator.

## Requirements and installation

- Python 3.11 or newer
- `pytest`, coverage, Ruff, Pyright, and build tooling in the `dev` extra
- `matplotlib` only for the legacy static visualisation
- Streamlit and Plotly in the optional `dashboard` extra

The headless simulator has no third-party runtime dependencies. From the
repository root, create an isolated environment and install the package with its
test dependency.

PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

POSIX shell:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

To include both visualization interfaces and their development tooling:

```console
python -m pip install -e ".[dev,visualization,dashboard]"
```

## Run the prototypes

The original command remains the deterministic Prototype 0.1 physical-failure
regression. Communication fault injection is disabled by default, so this
command preserves the original recovery path:

```console
python -m eudis_swarm.simulation
```

The installed console entry point is equivalent:

```console
eudis-swarm
```

### Trace-driven playback dashboard

Record a structured trace and launch the preferred local viewer:

```console
eudis-swarm --record-trace trace.json
eudis-swarm-dashboard trace.json
```

The dashboard provides mission and topology views, UAV and receiver-local peer
status, allocation explanations, live metrics, playback controls, an event
timeline, and a structured event log. See
[`docs/visualization_layer.md`](docs/visualization_layer.md) for panel semantics
and acceptance-scenario commands. The existing `--visualize` option remains a
legacy final-frame matplotlib debugging view.

### Distributed task-claim demonstration

Run the receiver-local ownership scenario directly and optionally record its
standalone belief trace:

```console
python -m eudis_swarm.task_claim_demo
python -m eudis_swarm.task_claim_demo --record-trace task-claims.trace.json
```

The deterministic run starts connected, cuts the four cross-links that form
`{1,2}` and `{3,4}`, and lets the isolated right component age its Task 19
evidence from fresh to stale and then strictly beyond the lease. UAV 4 can then
claim Task 19 legitimately while UAV 1 still owns and renews it on the left.
After reconnection, all four local stores visibly enter `CONTESTED`; each
independently selects UAV 1, UAV 4 releases its exact losing generation, and
then UAV 4 claims Task 29 to prove work continues without a restart.

The JSON trace contains ten strictly increasing stages. Every stage includes
the active graph, components, and every agent-by-task local view with known
claims, receipt-time age, freshness, epoch, contested flag, reconciliation
winner, release tombstones, and completion evidence. It deliberately contains
no authoritative `Task.assigned_agent` field that could be mistaken for agent
belief.

### Prototype 0.3A policy comparison

These commands use identical mission inputs and differ only by allocation
policy:

```console
python -m eudis_swarm.simulation --seed 1 --failure-time 100 --communication-range 35 --allocation-policy distance
python -m eudis_swarm.simulation --seed 1 --failure-time 100 --communication-range 35 --allocation-policy connectivity
```

Both policies make the same first 11 assignments. At `t=4.50 s`, the distance
baseline selects UAV 2 -> Task 2 at `11.18` distance units. UAV 2's local peer
store predicts zero reliable links there. The connectivity policy instead
selects UAV 2 -> Task 3 at `24.20` units because its `HEARD` snapshots predict
one direct peer link.

| Result | Distance | Connectivity |
| --- | ---: | ---: |
| Tasks completed | `20 / 20` | `20 / 20` |
| Mission duration | `12.00 s` | `17.25 s` |
| Isolation events | `4` | `1` |
| Minimum links | `0` | `0` |
| Maximum components | `4` | `4` |
| Degraded duration | `8.25 s` | `8.25 s` |
| Link losses/restorations | `4 / 5` | `5 / 8` |

This is a measured tradeoff: fewer isolation transitions and more travel/mission
time. It does not establish global optimality, and several network metrics are
unchanged or mixed.

### Prototype 0.2A communications demonstration

Use this command to postpone physical failure beyond the seeded mission and
isolate the otherwise healthy UAV 2 from `t=4.00 s` through `t=8.00 s`:

```console
python -m eudis_swarm.simulation --failure-time 100 --communication-range 130 --comm-fault-agent 2 --comm-fault-start 4 --comm-fault-end 8 --peer-state-stale-after 2.5
```

The 130-unit range exceeds the initial corner-layout diagonal of approximately
127.28 units and keeps all six pairs active throughout this seeded trace. The
explicit fault removes only UAV 2's three incident links, so injected
degradation is easy to distinguish from movement across the distance threshold.

Expected deterministic result:

| Observation | Expected value |
| --- | ---: |
| Tasks completed | `20 / 20` |
| Mission duration | `11.75 s` |
| Physically failed agents | `0` |
| Orphaned/reassigned tasks | `0 / 0` |
| Initial/minimum communication links | `6 / 3` |
| Maximum connected components | `2` |
| Isolation events | `1` |
| Link losses/restorations | `3 / 3` |
| Communication-degraded duration | `4.00 s` |
| Healthy-but-unreachable events | `1` |
| Network ended connected | `YES` |
| Peer messages attempted/delivered/undelivered | `144 / 120 / 24` |
| Peer stale/refresh transitions | `6 / 6` |

UAV 2 completes Task 19 at `t=4.00 s`, Task 15 at `t=4.75 s`, and
Task 20 at `t=7.50 s` while its communication is blocked. It remains responsive,
is never declared `FAILED`, and causes no task release. At `t=8.00 s`, its three
links return and the graph reconnects.

The last pre-fault snapshots involving UAV 2 arrive at `t=3.00 s`. At `t=5.75 s`,
strict freshness expiry makes the three other UAVs' views of UAV 2 stale and also
makes UAV 2's three remote peer views stale. Restoration occurs before the
`t=8.00 s` delivery batch, so all six observations refresh immediately. None of
these information transitions changes physical status or task ownership.

### Communication CLI options

| Argument | Default | Meaning |
| --- | ---: | --- |
| `--communication-range` | `130.0` | Inclusive Euclidean link threshold |
| `--comm-fault-agent` | omitted | UAV whose incident links are blocked; omission disables the communication fault |
| `--comm-fault-start` | `4.0` | Logical time at which blocking starts |
| `--comm-fault-end` | `8.0` | Logical time at which blocking ends; must be later than the start |
| `--peer-state-stale-after` | `2.5` | Strict receiver-local snapshot freshness threshold |
| `--allocation-policy` | `distance` | `distance` baseline or `connectivity` local-knowledge policy |

The existing `--agents`, `--tasks`, `--seed`, `--failure-agent`,
`--failure-time`, `--failure-timeout`, `--visualize`, and `--log-level` options
remain available. `--record-trace PATH` writes dashboard playback data. View the
authoritative help with:

```console
python -m eudis_swarm.simulation --help
```

`--visualize` requires the `visualization` extra and is retained as a legacy
static debug view. The process exits with status 0 when the mission completes,
1 on simulated mission timeout, and 2 for a command-line error or unavailable
requested visualization.

## Preserved Prototype 0.1 recovery

With the default seed, UAV 2 initially owns Task 19. Physical failure is
injected at `t=4.00 s`; because injection precedes that tick's heartbeat, its
last delivered heartbeat remains `t=3.00 s`. Local suspicion uses the strict
rule:

```text
now - max(last_heard_at, reachable_since) > failure_timeout
```

The receiver-arrival time, not the heartbeat's source timestamp, starts the
evidence-age interval; reconnecting later would start a new reachability grace
interval. In the default run, `last_heard_at` is `t=3.00 s`, so equality at
`t=5.50 s` does not fire. At `t=5.75 s`, surviving local replicas exchange
matching votes over available links, one replica forms a strict-majority
declaration, and `Mission` releases Task 19. UAV 1 receives the task at
`t=10.75 s`, and all 20 tasks complete at `t=17.25 s`.

## Run the tests

```console
python -m pytest
```

Run the complete local quality baseline with:

```console
python -m ruff check .
python -m ruff format --check .
python -m pyright
python -m pytest --cov=eudis_swarm --cov-report=term-missing
python -m build
```

Coverage has an initial 85% branch-aware threshold. GitHub Actions runs the
same lint, formatting, type, test, coverage, build, and installed-CLI checks on
Python 3.11 and 3.13 for pushes and pull requests involving `main`.

Configuration rejects booleans where integer counts or UAV IDs are required,
non-finite numeric values, invalid ranges, and illogical fault windows. Mission
operations are legal only while the lifecycle is `RUNNING`, and timestamps must
remain finite and monotonic. Tasks already satisfied at mission start complete
at `t=0.00 s` without an artificial movement tick.

For individual test names:

```console
python -m pytest -v
```

Coverage includes the preserved allocation and fail-stop recovery tests plus:

- inclusive distance-based link creation and rejection outside range;
- neighbor sets, components, isolation, and connectivity;
- deterministic loss/restoration when positions cross the range boundary;
- canonical link-level blocking, balanced 2+2 partitions, and reconnection;
- one-shot transition reporting without repeated steady-state logs;
- exact communication fault scheduling and restoration;
- proof that an unreachable healthy UAV continues moving and working;
- proof that communication loss does not release tasks or trigger physical
  failure recovery;
- exact regression of the original `4.00 -> 5.75 -> 10.75 -> 17.25` physical
  recovery sequence;
- proof that only `HEARD` delivered peer positions affect connectivity scores;
- proof that authoritative peer movement is invisible until a new snapshot is
  delivered;
- proof that silence and link loss cannot directly produce
  `DECLARED_FAILED`, while delivered vote quorum can;
- exact receiver-local task ownership vocabulary, including strict fresh,
  stale-valid, and lease-expired boundaries;
- graph-gated claim propagation, legitimate 2+2 split-brain, visible
  `CONTESTED`, order-independent convergence, and losing release;
- duplicate/old-message safety, monotonic completion, authoritative-state
  isolation, and scale tests with six noncontiguous UAV IDs; and
- deterministic baseline-versus-connectivity decision and outcome comparison.

### Six-agent smoke scenario

```console
python -m eudis_swarm.simulation --agents 6 --tasks 30 --seed 7 --failure-agent 4 --failure-time 100 --communication-range 130 --comm-fault-agent 6 --comm-fault-start 3 --comm-fault-end 6 --peer-state-stale-after 2.5
```

The deterministic smoke run completes `30 / 30` tasks at `t=12.00 s`, with no
physical failure or orphaned work. It begins with 15 links, falls to 10 while
UAV 6 is isolated, loses and restores five links, remains degraded for 3.00
seconds, and ends connected. UAV 6 completes Tasks 23, 20, and 27 during its
communication outage. Ten directed peer observations become stale at `t=4.75 s`
and refresh at `t=6.00 s`; no false physical failure is introduced.

## Network and peer-state metrics

The physical mission summary remains separate from a Prototype 0.2A network
block containing:

- initial and minimum active-link counts;
- maximum connected-component count;
- isolation, link-loss, and link-restoration event counts;
- total logical time for which the network is not fully connected;
- whether the final graph is connected; and
- transitions in which a physically healthy UAV becomes unreachable.

Initial links are a baseline, not restoration events. A sustained loss or
isolation is counted once when the state changes, not once per simulation tick.
Physical `Agents failed` and network `Healthy but unreachable UAV events` are
never merged.

The separate `PEER STATE (PROTOTYPE 0.2B)` block reports directed delivery
attempts, successful and link-gated undelivered messages, stale and refresh
transitions, and the maximum number of simultaneous stale receiver-local
observations. The metrics are observer-only; the underlying delivered
observations may feed the connectivity reference policy and local failure
protocol, but metric values never feed decisions.

The `ALLOCATION (PROTOTYPE 0.3A)` block reports the selected policy,
connectivity-aware assignment count, assignments predicting isolation, and mean
and minimum predicted heard-peer degree. Applied decision records retain the
timestamp, selected IDs, distance, policy, degree, and isolation prediction.

## Current limitations

- The link model is only an inclusive Euclidean distance threshold plus
  explicit whole-UAV and undirected link blocks. It is not RF propagation and
  does not calculate RSSI, SINR, interference, antenna effects, terrain, or
  weather.
- Peer snapshots use instantaneous one-hop in-process delivery. There are no
  queues, retries, forwarding, multi-hop routing, latency, jitter, bandwidth, or
  stochastic packet loss.
- Peer knowledge affects new allocations when the connectivity policy is
  explicitly selected and supplies evidence to the failure protocol. It does
  not affect path planning or movement.
- Failure detection is a deterministic one-hop strict-majority prototype with a
  two-voter minimum, so fewer than three configured UAVs cannot declare
  failure. It is not Byzantine fault tolerance, a production consensus system,
  or proof that a real vehicle has physically failed.
- The graph is centralized, recomputed from authoritative positions, and has no
  ground-station or coordinator-reachability anchor. Global
  `CommunicationState.UNREACHABLE` means isolated from every peer, while local
  `PeerStatus.UNREACHABLE` means the direct pair cannot currently deliver.
- All components execute in one Python process. There is no distributed
  deployment, process isolation, or real network transport; local replicas and
  their peer-to-peer vote protocol are simulated deterministically.
- Agents remain point masses without aircraft dynamics, vehicle constraints,
  terrain, collision avoidance, or energy limits.
- Tasks remain independent points. Both policies are centralized greedy pairing,
  not joint trajectory prediction or global optimization. The task-claim EFSM
  is a distributed ownership foundation, not yet a distributed task-utility or
  bidding algorithm, and it is not wired into authoritative mission movement.
- Claim delivery is deterministic, instantaneous, and one-hop over the modeled
  graph. The protocol assumes non-Byzantine participants with stable validated
  IDs; it does not authenticate messages, tolerate identity forgery, or prove
  that a remote claimant followed the local epoch-advance rule.
- Owner-scoped epochs order only one owner's publications. Cross-owner conflicts
  deliberately choose the lowest owner ID as a transparent foundation rule,
  not as an operationally optimal priority policy.
- Physical failure injection still models one fail-stop UAV; there is no
  rejoin, intermittent, Byzantine, or simultaneous-failure model.
- The optional visualization is a final debugging view, not a live dashboard or
  control interface.
- There is no ROS 2, MAVLink, ArduPilot, PX4, Gazebo, AirSim, SLAM, computer
  vision, database, web application, or real-time guarantee.

## Roadmap

- **Prototype 0.1 — implemented:** basic swarm simulation and fail-stop task
  recovery.
- **Prototype 0.2A — implemented:** explicit dynamic communication graph,
  abstract link degradation, partition, isolation, restoration, and metrics.
- **Prototype 0.2B — implemented:** active direct links mediate immutable peer
  state delivery and receiver-local freshness, without adaptive behavior.
- **Prototype 0.3A — implemented:** optional connectivity-aware allocation from
  receiver-local heard peer snapshots.
- **Distributed-state foundation — implemented:** delivered heartbeat and link
  evidence, receiver-local peer status, quorum-backed declarations, link-level
  partitions, and the six-state task-ownership seam.
- **Distributed task ownership — implemented:** immutable claims, distinct
  freshness/lease thresholds, owner-scoped epochs, split-brain, deterministic
  reconciliation, losing release, terminal completion, and belief-only trace.
- **Later Prototype 0.3 milestones — planned:** a distributed allocator or
  bidding layer that creates claim intents, plus communications-aware path
  planning, relay, and comparative allocation experiments.
- **Prototype 0.4 — planned:** QUBO / quantum-simulated optimization experiments.
- **Prototype 0.5 — planned:** distributed ROS 2 implementation.
- **Prototype 0.6 — planned:** ArduPilot multi-UAV SITL integration.
- **Prototype 1.0 — planned:** EUDIS demonstration baseline.

## Working agreement

Development is currently closed to the core team. The working agreement,
architectural boundaries that must be preserved, and the local quality baseline
are documented in [`CONTRIBUTING.md`](CONTRIBUTING.md). The short version: every
published number in this README is regression-tested, and a change that moves
one must say so explicitly.

If you have spotted a correctness defect, a report is genuinely welcome — see
[`SECURITY.md`](SECURITY.md) for what to include.

## Copyright and use

**Copyright 2026 Charalampos Nadiotis. All rights reserved.**

This repository is published for review only. It carries **no open-source
licence**, and no licence is granted or implied by its visibility. You may not
use, copy, modify, distribute, or create derivative works from this code, in
whole or in part, without prior written permission from the copyright holder.

This repository is expected to become private. Enquiries about access or use:
charalamposnadiotis44@gmail.com

## Not flight software

This repository models UAV coordination algorithmically. It contains no
autopilot, guidance, navigation, control, targeting, or safety-critical
functionality, and no interface to any real vehicle, radio, or autopilot stack.
It must not be deployed on, integrated into, or used to command any real
vehicle. See [Current limitations](#current-limitations).
