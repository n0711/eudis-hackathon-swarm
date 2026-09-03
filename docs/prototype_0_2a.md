# Prototype 0.2A technical design

> Historical scope: this document records the world-level communication graph
> milestone and its original results. Current agent decisions still never receive
> graph truth, but immutable task/failure evidence can now cross connected
> multi-hop components and task execution is authorized by receiver-local claims.
> See [the distributed-state foundation](distributed_state_foundation.md).

## Scope and acceptance criterion

Prototype 0.2A adds an explicit, time-varying communication topology to the
deterministic Prototype 0.1 mission simulation:

```text
UAV positions
    -> abstract distance-based links
    -> dynamic undirected graph
    -> deterministic degradation and partition
    -> deterministic communication restoration
```

The acceptance scenario isolates one physically healthy UAV, observes the
resulting link loss and graph partition, and later restores its links. During
the outage, that UAV must continue moving and completing its assigned work. It
must not be declared `FAILED`, and its task must not be released merely because
it is unreachable.

Prototype 0.1 remains the physical-resilience baseline:

```text
physical fail-stop
    -> direct heartbeat timeout
    -> FAILED declaration
    -> orphaned-task release
    -> survivor reassignment
    -> mission completion
```

The two paths are intentionally independent. Prototype 0.2A observes
communications; it does not yet use topology to deliver messages or make
mission decisions.

## Component boundaries

### `communication.py`

The communication module is a standard-library graph implementation with no
`networkx` dependency. Its public model consists of:

- `CommunicationState.REACHABLE` and `CommunicationState.UNREACHABLE`;
- `CommunicationLink`, an immutable pair record containing source UAV ID,
  destination UAV ID, Euclidean distance, and availability;
- `CommunicationUpdate`, an immutable delta between topology snapshots; and
- `CommunicationGraph`, the authoritative current communication topology.

`CommunicationGraph` reports:

- configured UAV IDs and currently blocked UAV IDs;
- all canonical pair records and the active subset;
- active-link count;
- active neighbor set for each UAV;
- connected components;
- isolated UAV IDs; and
- whether the graph is fully connected.

Its first position update establishes a baseline. That update is marked
`is_initial=True` and does not misclassify the baseline links as restorations.
Later updates report only actual changes: lost and restored links, newly
isolated and newly reachable UAVs, previous/current component counts, and
partition/reconnection transitions.

### `simulation.py`

`Simulation` owns both fault schedules and updates the graph as positions
change. The communication fault schedule is independent of the existing
physical failure schedule. At the configured start, every incident link for the
selected UAV is blocked. At the configured end, that override is removed and
the normal distance rule applies again.

Communication-only observations do not call the task allocator, release work,
declare failure, or suppress movement. Heartbeat generation and timeout
detection remain the direct Prototype 0.1 path.

### `metrics.py`

Network metrics are recorded from graph snapshots and topology transitions.
They are presented separately from physical failure and task-recovery metrics.
In particular, an unreachable UAV does not increment `Agents failed`.

### Preserved components

`Agent`, `Task`, `TaskAllocator`, `FailureManager`, and `Mission` retain their
Prototype 0.1 responsibilities. Communication state does not overload
`Agent.status`, and the graph does not mutate task ownership.

## Exact graph model

At logical time `t`, the graph is:

```text
G(t) = (V, E(t))
```

`V` contains every configured UAV ID, including an isolated UAV. Undirected
pairs are canonicalized as `(min(i, j), max(i, j))`, so each pair has exactly
one link record and deterministic logging order.

For current positions `p_i=(x_i,y_i)` and `p_j=(x_j,y_j)`:

```text
d(i, j) = sqrt((x_j - x_i)^2 + (y_j - y_i)^2)
```

The active-edge rule is exactly:

```text
(i, j) is in E(t)
    iff d(i, j) <= communication_range
    and i is not communication-blocked
    and j is not communication-blocked
```

The range comparison is inclusive. A pair exactly on the boundary is active.
An out-of-range or explicitly blocked pair still has a link record with its
current distance and `available=False`; only available records appear in
`active_links` and topology calculations.

No normalized quality, RSSI, SINR, propagation loss, or packet-success
probability is inferred from distance. `communication_range` is an abstract
threshold used to establish topology behavior before a realistic network model
exists.

## Communication state and topology semantics

The two state dimensions are independent:

| Physical state | Meaning |
| --- | --- |
| `IDLE` | Physically healthy, responsive, and not currently assigned |
| `ACTIVE` | Physically healthy, responsive, and performing a task |
| `FAILED` | Declared physical fail-stop and unavailable to the mission |

| Communication state | Meaning |
| --- | --- |
| `REACHABLE` | At least one active link to another UAV |
| `UNREACHABLE` | No active peer link; the UAV is an isolated graph vertex |

`STALE` is omitted because packets and message freshness are not modeled in
0.2A. Heartbeat staleness remains solely a physical failure-detector concept.

A network can be partitioned without containing an isolated UAV. For example,
two disconnected components of two UAVs each are a partition, while every UAV
is still reachable to one peer. Because there is no ground-station or
coordinator vertex, 0.2A does not claim global reachability from a privileged
anchor.

Connected components include every vertex and use active undirected links.
The network is fully connected when it has at most one component. In the
single-UAV edge case, that graph is connected in the graph-theoretic sense even
though the lone UAV has degree zero and is reported as isolated/unreachable.

## Topology transitions and logging

Each update compares its active edge set and topology with the previous
snapshot. Meaningful transitions include:

```text
[LINK] UAV 1 <-> UAV 2 LOST
[LINK] UAV 1 <-> UAV 2 RESTORED
[NETWORK] Network partitioned into 2 components
[NETWORK] UAV 2 became unreachable
[NETWORK] Network reconnected
```

Exact wording is an implementation detail, but the semantics and ordering are
deterministic. Stable links and a sustained partition are not logged again on
every tick. An initial link is a baseline, not a restoration event.

## Independent communication fault

The optional fault targets one configured UAV and has two exact logical times:

```text
comm_fault_start: block all links to/from the selected UAV
comm_fault_end:   remove that override and reapply the distance rule
```

Blocking changes only the graph. It does not set `responsive=False`, change
`Agent.status`, clear `current_task`, stop motion, suppress direct heartbeats,
or invoke task recovery. Restoration does not perform a physical rejoin; the
UAV was physically present and working throughout.

The communication fault is disabled when no `comm_fault_agent_id` is supplied.
This default preserves the exact Prototype 0.1 physical demonstration. The
graph still observes the default mission, but it does not inject a communication
outage.

If a communication boundary does not coincide with an existing physical
mission boundary, it is an observational graph boundary. Advancing to that time
is needed to obtain the correct positions, but the added observation must not
create an earlier heartbeat timeout, task completion, or allocation. This keeps
communication observation from changing physical results.

## Configuration and CLI

New validated `SimulationConfig` fields are:

| Field | Default | Validation and meaning |
| --- | ---: | --- |
| `communication_range` | `130.0` | Finite and greater than zero; inclusive link threshold |
| `comm_fault_agent_id` | `None` | Optional configured UAV ID; `None` disables fault injection |
| `comm_fault_start` | `4.0 s` | Finite and non-negative |
| `comm_fault_end` | `8.0 s` | Finite, non-negative, and strictly later than the start |

CLI mappings are:

| Argument | Configuration field |
| --- | --- |
| `--communication-range` | `communication_range` |
| `--comm-fault-agent` | `comm_fault_agent_id` |
| `--comm-fault-start` | `comm_fault_start` |
| `--comm-fault-end` | `comm_fault_end` |

All Prototype 0.1 options and defaults remain available. In particular, the
existing physical failure remains UAV 2 at `t=4.00 s` unless overridden. A
communications-only demonstration therefore postpones physical failure beyond
the deterministic mission by passing `--failure-time 100`.

## Network metrics

| Metric | Definition |
| --- | --- |
| Initial communication links | Active-link count in the baseline graph |
| Minimum communication links | Lowest active-link count observed through mission end |
| Maximum connected components | Highest component count observed through mission end |
| Isolation events | Number of transitions in which a UAV newly acquires degree zero |
| Link-loss events | Number of individual active pair transitions to unavailable |
| Link-restoration events | Number of individual unavailable pair transitions to active |
| Communication-degraded duration | Total logical time for which the graph is not fully connected |
| Network ended connected | Whether the final graph is fully connected |
| Healthy but unreachable UAV events | Newly isolated transitions where the UAV remains physically responsive and is not `FAILED` |

Event counters count transitions, not samples. A four-second sustained isolation
is one isolation event and four seconds of degraded duration, not one event per
tick. If a mission ends while partitioned, the open degraded interval is closed
at mission end.

These network fields appear in a separate summary block:

```text
NETWORK (PROTOTYPE 0.2A)

Initial communication links: ...
Minimum communication links: ...
Maximum connected components: ...
Isolation events: ...
Link-loss events: ...
Link-restoration events: ...
Communication-degraded duration: ...
Network ended connected: ...
Healthy but unreachable UAV events: ...
```

The existing physical summary remains authoritative for failed agents,
orphaned/reassigned tasks, physical recovery, and mission duration.

## Deterministic four-UAV demonstration

Run:

```console
python -m eudis_swarm.simulation --failure-time 100 --communication-range 130 --comm-fault-agent 2 --comm-fault-start 4 --comm-fault-end 8
```

The 130-unit range exceeds the approximately 127.28-unit diagonal separation
of the initial 100 x 100 corner layout. All six undirected pairs therefore begin
active, and the fault itself is the only cause of link changes in this trace.

| Logical time | Deterministic event |
| ---: | --- |
| `0.00 s` | Four UAVs and 20 tasks start; graph baseline is one connected component with six links |
| `4.00 s` | UAV 2's three links are blocked; it becomes isolated and the graph has two components |
| `4.00 s` | Still-healthy UAV 2 completes Task 19 and receives Task 15 |
| `4.75 s` | Still-isolated UAV 2 completes Task 15 and continues working |
| `7.50 s` | Still-isolated UAV 2 completes Task 20 and continues working |
| `8.00 s` | Blocking ends; UAV 2's three links return and the graph reconnects |
| `11.75 s` | All 20 tasks are complete and the mission finishes |

Expected result:

| Metric | Value |
| --- | ---: |
| Mission completed | `YES` |
| Tasks completed | `20 / 20` |
| Agents failed | `0` |
| Orphaned tasks | `0` |
| Tasks reassigned by recovery | `0` |
| Initial communication links | `6` |
| Minimum communication links | `3` |
| Maximum connected components | `2` |
| Isolation events | `1` |
| Link-loss events | `3` |
| Link-restoration events | `3` |
| Communication-degraded duration | `4.00 s` |
| Network ended connected | `YES` |
| Healthy but unreachable UAV events | `1` |

The proof of separation is both positive and negative: UAV 2 completes work
during the outage, while no physical-failure declaration, task release, or
recovery reassignment is generated for it.

## Prototype 0.1 regression

The unchanged default command is:

```console
python -m eudis_swarm.simulation
```

Its physical recovery remains:

| Logical time | Event |
| ---: | --- |
| `4.00 s` | Physical fail-stop injected into UAV 2 while it owns Task 19 |
| `5.75 s` | Last heartbeat at `3.00 s` is strictly stale; UAV 2 becomes `FAILED` and Task 19 is released |
| `10.75 s` | UAV 1 receives orphaned Task 19 |
| `17.25 s` | UAV 1 completes Task 19; mission completes `20 / 20` |

The default communication fault is disabled, and communication topology does
not participate in any of these transitions.

## Automated validation

Run all tests:

```console
python -m pytest
```

Prototype 0.2A.1 adds the following quality checks without changing the
observational communications model:

```console
python -m ruff check .
python -m ruff format --check .
python -m pyright
python -m pytest --cov=eudis_swarm --cov-report=term-missing
```

The coverage threshold is 85%. CI runs these checks, a package build, and an
installed CLI smoke test on Python 3.11 and 3.13. Runtime hardening rejects
invalid/non-finite configuration and backwards logical time, restricts mission
operations to the `RUNNING` lifecycle, and completes initially satisfied work
at `t=0.00 s`.

The complete suite retains Prototype 0.1 allocation, timeout, scheduling, and
mission-recovery coverage and adds tests for:

1. link creation at and inside the inclusive range boundary, and rejection
   outside it;
2. deterministic neighbors, connected components, isolation, and connectivity;
3. link loss and restoration when movement crosses the configured range;
4. transition idempotence, so steady state does not create repeated events;
5. independent communication fault scheduling;
6. a healthy isolated UAV retaining and completing its task;
7. communication restoration without physical recovery side effects;
8. network metrics and degraded-duration accounting; and
9. the exact original Prototype 0.1 failure and recovery timeline.

## Six-agent smoke scenario

Run:

```console
python -m eudis_swarm.simulation --agents 6 --tasks 30 --seed 7 --failure-agent 4 --failure-time 100 --communication-range 130 --comm-fault-agent 6 --comm-fault-start 3 --comm-fault-end 6
```

Expected deterministic outcome:

- all `30 / 30` tasks complete at `t=12.00 s`;
- no agent physically fails and no task is orphaned;
- the graph starts with 15 links and falls to 10;
- blocking UAV 6 loses five links, creates two components, and isolates one
  healthy UAV;
- restoration returns the same five links after 3.00 seconds;
- the network ends connected; and
- UAV 6 completes Tasks 23, 20, and 27 during its outage.

## Visualization

Visualization remains optional and secondary to the headless tests. The final
debugging view may show active final links and isolated/unavailable communication
state in addition to paths, task states, and physical UAV states. It is a static
end-of-run plot, not live telemetry or a control interface.

## Architectural limitations and deferred work

Prototype 0.2A deliberately does not implement:

- heartbeat or mission-message delivery through graph links;
- packet loss, latency, jitter, bandwidth, queues, routing, or retransmission;
- a `STALE` network state or packet-freshness semantics;
- realistic RF propagation, RSSI, SINR, interference, antennas, terrain, or
  weather;
- relay behavior, communication-aware allocation, or communication-aware path
  planning;
- distributed graph knowledge, clocks, consensus, or task allocation;
- ROS 2, MAVLink, ArduPilot, PX4, SITL, Gazebo, or AirSim;
- QUBO or quantum optimization; or
- real aircraft dynamics, safety, sensing, collision avoidance, or energy.

The graph is recomputed centrally from authoritative positions and an explicit
block set. Its availability flag expresses only this abstract model. Physical
failure and communication reachability can be combined in later experiments,
but one is never inferred from the other in Prototype 0.2A.

Prototype 0.2B now builds on this graph with one-hop, graph-mediated peer state
delivery and receiver-local freshness; see
[the Prototype 0.2B design](prototype_0_2b.md). The centralized physical failure
heartbeat path remains deliberately separate, and peer knowledge still does not
drive communication-aware decisions.
