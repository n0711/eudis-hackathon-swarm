# Prototype 0.3A: connectivity-aware task allocation

This document explains the centralized reference allocator introduced in
Prototype 0.3A. The current distributed-state foundation further requires a
peer's complete local status to be `HEARD` before its snapshot affects scoring.

## Objective

Prototype 0.3A is the first milestone in which delivered communication
information can change a new task assignment. It preserves the established
separation between:

1. authoritative physical `Agent` and `Mission` state;
2. actual direct links in `CommunicationGraph`;
3. receiver-local knowledge in each `PeerStateStore`; and
4. allocation proposals that `Mission` may authoritatively apply.

The implementation is a centralized reference policy, not a distributed
allocation protocol. It answers one narrow question: can a UAV avoid a task
endpoint that its own current knowledge predicts would isolate it?

## Policy selection

`allocation_policy` and `--allocation-policy` accept:

- `distance` (default): the unchanged Prototype 0.1 globally greedy
  nearest-pair allocator;
- `connectivity`: the new local-knowledge connectivity preference.

Keeping `distance` as the default preserves every established Prototype
0.1/0.2 deterministic regression.

Both allocators implement one minimal proposal interface. They return immutable
`Allocation` values and never mutate `Agent.current_task` or
`Task.assigned_agent`. `Mission` remains the only owner of cross-entity
assignment transitions.

## Distance baseline

For every available UAV and unassigned task, the baseline minimizes:

```text
(travel_distance, agent_id, task_id)
```

It selects the best pair globally, removes that UAV and task from the candidate
sets, and repeats. It does not inspect topology or peer knowledge.

## Connectivity score

For a candidate `UAV i -> Task j`, the policy assumes UAV `i` reaches the task
endpoint. It examines only UAV `i`'s own `PeerStateStore`.

For each peer `k`:

- `HEARD`: use the position in the last successfully delivered immutable
  snapshot;
- `SILENT`, `UNREACHABLE`, or `DECLARED_FAILED`: exclude it.

Raw freshness remains separately observable as `UNKNOWN`, `FRESH`, or `STALE`.
`FRESH` is necessary but no longer sufficient for connectivity scoring because
a blocked link or protocol declaration is stronger local evidence.

The predicted degree is:

```text
predicted_degree(i, j) = count of HEARD peers k where
distance(Task_j.position, last_delivered_position_i_knows_for_k)
    <= communication_range
```

Then:

```text
predicted_isolation = 1 if predicted_degree == 0 else 0

score = (
    predicted_isolation,
    -predicted_degree,
    travel_distance,
    agent_id,
    task_id,
)
```

The minimum score wins. This lexicographically avoids predicted isolation,
maximizes predicted reliable direct links, minimizes travel, and finally uses
stable ID tie-breaking. No arbitrary weighting constant is introduced.

## Knowledge boundary

The candidate UAV's current position comes from its own `Agent`, because that is
self-state needed for travel distance. Positions of other UAVs come exclusively
from:

```text
PeerStateStore -> PeerObservation -> immutable Heartbeat.position
```

The allocator never uses another candidate's live `Agent.position` to predict a
link. Tests deliver a peer at `P1`, move the authoritative peer to `P2` without
delivery, and prove scoring still uses `P1`. A subsequent delivered snapshot is
required before scoring uses `P2`.

This also means a stale or never-delivered peer supplies no connectivity
evidence. `STALE` and `UNKNOWN` do not imply physical failure: the UAV remains
eligible if its authoritative physical state is otherwise idle and healthy.

## Initial UNKNOWN fallback

`Mission.start()` intentionally retains its existing ordering: initial task
allocation occurs before the first graph-mediated delivery. Peer stores are
therefore initially `UNKNOWN`.

When no candidate has useful heard peer information, every predicted degree is
zero. The leading score terms tie, so distance and IDs reproduce the baseline
ordering naturally. There is no authoritative-peer lookup or scheduler hack.

## Deterministic comparison

Run the same scenario with only the policy changed:

```console
python -m eudis_swarm.simulation --seed 1 --failure-time 100 --communication-range 35 --allocation-policy distance
python -m eudis_swarm.simulation --seed 1 --failure-time 100 --communication-range 35 --allocation-policy connectivity
```

The policies make identical decisions through allocation index 10. At
`t=4.50 s`, UAV 2 is available and the decision diverges:

| Candidate | Travel distance | Predicted heard degree | Predicted isolation |
| --- | ---: | ---: | --- |
| UAV 2 -> Task 2 | `11.18` | `0` | yes |
| UAV 2 -> Task 3 | `24.20` | `1` | no |

The distance baseline selects Task 2. The connectivity policy selects Task 3
because avoiding predicted isolation precedes distance in its documented score.

Measured outcomes are:

| Metric | Distance | Connectivity |
| --- | ---: | ---: |
| Tasks completed | `20 / 20` | `20 / 20` |
| Physical failures | `0` | `0` |
| Mission duration | `12.00 s` | `17.25 s` |
| Initial/minimum links | `0 / 0` | `0 / 0` |
| Maximum components | `4` | `4` |
| Isolation events | `4` | `1` |
| Link losses/restorations | `4 / 5` | `5 / 8` |
| Degraded duration | `8.25 s` | `8.25 s` |
| Peer messages delivered/undelivered | `38 / 118` | `120 / 96` |

The result is a tradeoff, not proof of universal superiority: the connectivity
policy reduces isolation transitions but accepts a longer assignment and a
5.25-second longer mission. Minimum link count, maximum component count, and
degraded duration do not improve. The policy is greedy and is not globally
optimal.

## Reporting and metrics

Every applied allocation record retains its logical timestamp, selected agent
and task IDs, travel distance, policy, predicted heard-peer degree, and predicted
isolation flag where applicable.

The `ALLOCATION (PROTOTYPE 0.3A)` summary reports:

- selected policy;
- connectivity-aware assignment count;
- assignments predicting isolation;
- mean predicted peer degree; and
- minimum predicted peer degree.

INFO logs use `[ALLOC-COMM]` for selected connectivity decisions. Rejected
candidates are not logged at INFO level.

## Validation

Focused tests cover the unchanged distance baseline, initial unknown fallback,
heard connectivity preference, non-heard exclusion, last-delivered versus
authoritative peer position, refresh behavior, deterministic tie-breaking,
failed-agent exclusion, decision metadata, deterministic side-by-side outcomes,
and every Prototype 0.1/0.2 regression.

## Deliberate limitations

Prediction is a geometric one-hop endpoint estimate, not RF, RSSI, SINR, or a
propagation model. It assumes peer positions remain at their last delivered
locations and does not jointly predict simultaneous assignments or motion.

Prototype 0.3A adds no task preemption, stealing, relay role, topology-repair
movement, communication-aware path planning, multi-hop routing, forwarding,
queues, general reliable heartbeat retransmission, latency, distributed auction,
consensus, leader election, ROS 2, MAVLink, autopilot integration, SITL, QUBO,
QAOA, or quantum simulation. Those remain separately scoped future work.
