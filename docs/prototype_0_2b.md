# Prototype 0.2B: graph-mediated peer state delivery

> Historical scope: this document records the 0.2B one-hop heartbeat transport
> and its original measurements. The current architecture retains one-hop
> heartbeats but later adds deterministic multi-hop flooding for immutable task
> and failure-protocol evidence plus receiver-local claim-authorized task control.
> See
> [the distributed-state foundation](distributed_state_foundation.md).

## Objective

Prototype 0.2B turns the Prototype 0.2A communication graph into a small,
deterministic information-delivery mechanism. It establishes a strict separation
between:

1. authoritative physical `Agent` and `Mission` ground truth;
2. current direct-link topology in `CommunicationGraph`; and
3. each UAV's receiver-local last-known information about its peers.

The new peer knowledge is observational. It does not drive allocation, task
release, physical failure detection, movement, or recovery.

## Transport contract

At the existing heartbeat publication schedule, each responsive UAV produces the
existing immutable `Heartbeat` snapshot: source ID, position tuple, physical
`AgentStatus`, current task, and logical send timestamp.

For each source/receiver pair:

```text
active direct graph link -> deliver snapshot immediately
unavailable direct link  -> no delivery; retain prior receiver state
```

Delivery is one-hop and instantaneous in logical simulation time. There is no
routing, forwarding, flooding, retry, queue, latency, jitter, bandwidth, or
stochastic packet-loss model. An immutable snapshot contains no reference to the
mutable source `Agent`; later source movement or task changes cannot alter a
previously received observation.

## Receiver-local peer knowledge

Every UAV owns a `PeerStateStore` containing only other UAV IDs. A
`PeerObservation` contains the delivered snapshot and `received_at` time. Stores
are independent: one receiver may retain an older view of a source than another.
They are populated only by `PeerStateTransport` after an active-link check; they
never read `Mission.agents` or another live `Agent`.

Freshness is a separate dimension from topology and physical status:

- `UNKNOWN`: no snapshot has ever been delivered from that peer;
- `FRESH`: a snapshot exists and its receiver-local age is within the threshold;
- `STALE`: a snapshot exists and its age is strictly greater than the threshold.

The strict rule is:

```text
current_time - received_at > peer_state_stale_after
```

The default `peer_state_stale_after` is 2.5 logical seconds. It is validated as a
finite positive numeric value with booleans rejected. It is conceptually separate
from the physical heartbeat failure timeout even though their defaults match.

## Scheduler ordering

Prototype 0.2B preserves the established deterministic scheduler. At each event
boundary it:

1. advances physical positions when the boundary is physical;
2. injects a scheduled physical failure;
3. starts or ends the communication fault;
4. recomputes the communication graph;
5. advances peer freshness and emits newly stale transitions;
6. on heartbeat boundaries, publishes responsive snapshots and delivers them
   through active direct links;
7. runs the unchanged centralized physical failure/recovery path;
8. completes arrivals and allocates authoritative mission work.

This ordering makes a fault beginning at a heartbeat timestamp block that
timestamp's delivery. Restoration at a heartbeat timestamp permits immediate
delivery and refresh.

## Deterministic four-UAV trace

At the Prototype 0.2B baseline revision, run:

```console
python -m eudis_swarm.simulation --failure-time 100 --communication-range 130 --comm-fault-agent 2 --comm-fault-start 4 --comm-fault-end 8 --peer-state-stale-after 2.5
```

The seeded result is:

| Time | Observation |
| ---: | --- |
| `0.00-3.00` | Full-mesh direct state delivery succeeds |
| `3.00` | Last pre-fault snapshots involving UAV 2 reach peers |
| `4.00` | UAV 2 links are blocked before delivery; UAV 2 remains healthy and keeps working |
| `5.75` | Six directed receiver-local observations become stale under strict timeout semantics |
| `8.00` | Links restore before delivery; six observations refresh immediately |
| `11.75` | Mission completes all `20 / 20` tasks with zero physical failures |

The delivery metrics are 144 attempts, 120 deliveries, and 24 link-gated
non-deliveries. There are six stale and six refresh transitions, with at most six
simultaneous stale observations.

These figures archive the 0.2B acceptance result. Current `main` retains the
scenario but includes later failure- and ownership-protocol phases, so this same
command is not expected to reproduce the historical mission time or message
totals.

## Metrics and logging

The `PEER STATE (PROTOTYPE 0.2B)` summary reports:

- directed messages attempted;
- messages delivered;
- messages undelivered because no active direct link existed;
- stale transitions;
- refresh-after-stale transitions; and
- maximum simultaneous stale receiver-local observations.

INFO logs contain only stale and refresh transitions. Per-batch delivery details
are DEBUG-level to avoid message spam.

## Physical recovery boundary

The Prototype 0.1 failure detector still receives the responsive UAV snapshots
through its existing centralized mission scaffold. That transitional path remains
separate to preserve physical fail-stop recovery and its deterministic regression.
All new UAV-to-UAV peer knowledge, however, comes only from graph-gated delivery.

Therefore:

```text
STALE       != FAILED
UNKNOWN     != FAILED
UNREACHABLE != FAILED
```

Stale peer information never releases or reassigns a task in Prototype 0.2B.

## Automated validation

The tests cover active-link delivery, no-link non-delivery, immutable snapshots,
independent receiver views, unknown state, strict fresh-to-stale expiry,
stale-to-fresh restoration, multiple-peer isolation, deterministic transition
traces, the preserved Prototype 0.1 recovery, and Prototype 0.2A graph metrics.

Run:

```console
python -m ruff check .
python -m ruff format --check .
python -m pyright
python -m pytest --cov=eudis_swarm --cov-report=term-missing
python -m build
```

## Deliberately deferred

Within its historical scope, Prototype 0.2B did not implement
communications-aware allocation or task release, connectivity-aware path
planning, relay roles, consensus, leader election,
multi-hop routing, forwarding, flooding, retransmission, message queues, latency,
jitter, bandwidth, stochastic loss, RF propagation, ROS 2, DDS, MAVLink,
autopilots, SITL, Gazebo, or quantum optimization. Later milestones add the
optional 0.3A new-task policy, receiver-local claim ownership, a small radio-link
option, and immutable evidence flooding; they do not change the historical 0.2B
heartbeat results above.
