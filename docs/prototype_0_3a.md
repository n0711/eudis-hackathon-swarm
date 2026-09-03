# Prototype 0.3A: connectivity-aware task allocation

> Historical scope: this document explains the centralized comparison policy
> introduced in Prototype 0.3A. The normal mission now uses receiver-local
> utility, task claims, and `owns_task()` execution authority. The
> `TaskAllocator` classes remain baselines; `--allocation-policy` maps their
> distance/connectivity choices into the operational local-utility path described
> below. See [the distributed-state foundation](distributed_state_foundation.md).

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

In the original 0.3A implementation, `allocation_policy` and
`--allocation-policy` selected:

- `distance` (default): the unchanged Prototype 0.1 globally greedy
  nearest-pair allocator;
- `connectivity`: the new local-knowledge connectivity preference.

Keeping `distance` as the default preserves every established Prototype
0.1/0.2 deterministic regression.

Both baseline allocators implement one minimal proposal interface. They return
immutable `Allocation` values and mutate neither agents nor tasks. Normal
distributed-control runs do not call that interface.

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
- `SILENT` or `DECLARED_FAILED`: exclude it.

Raw freshness remains separately observable as `UNKNOWN`, `FRESH`, or `STALE`.
`FRESH` is necessary but no longer sufficient for connectivity scoring because
elapsed receiver-local silence or a protocol declaration is stronger local
evidence. The graph's blocked-link truth is never copied into this store.

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

## Current operational mapping

The normal mission now treats those policy names as local utility modes:

- `distance` uses `TaskUtilityWeights(distance=1)` and zero resource,
  communication, and role costs;
- `connectivity` adds a communication cost equal to configured peer count minus
  predicted heard degree. Its weight is twice the mission-area diagonal, so one
  locally predicted degree step dominates any in-area travel difference.

Each responsive idle UAV ranks the frozen `TaskObjective` catalogue
independently, creates a claim before work is bound, and executes only after its
own store resolves that claim to `OWNED_BY_SELF`. The historical global greedy
score remains useful as a comparator, but it is not the operational authority.

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

## Initial knowledge

In historical 0.3A, initial allocation preceded heartbeat delivery, so every
peer store began `UNKNOWN`. Normal distributed control now skips centralized
startup allocation, delivers the initial heartbeat snapshots through the graph,
and then runs its first local claim round. If a receiver still has no useful
`HEARD` copies, every predicted degree is zero and its local ordering falls back
to travel distance and task ID. There is no graph or live-peer lookup.

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

Every activated local owner produces a compatible observer `Allocation` record
containing its logical timestamp, selected agent and task IDs, travel distance,
policy, predicted heard-peer degree, and predicted-isolation flag where
applicable. That record describes a decision already made from local utility; it
does not authorize work.

The `ALLOCATION (PROTOTYPE 0.3A)` summary reports:

- selected policy;
- connectivity-aware assignment count;
- assignments predicting isolation;
- mean predicted peer degree; and
- minimum predicted peer degree.

The historical centralized baseline logs selected connectivity decisions with
`[ALLOC-COMM]`. Normal distributed task activation instead logs `[CLAIM]` after
local ownership is established. Rejected candidates are not logged at INFO
level.

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

Within its historical scope, Prototype 0.3A added no task preemption, stealing,
relay role, topology-repair movement, communication-aware path planning,
multi-hop forwarding, queues, general reliable heartbeat retransmission,
latency, distributed auction, consensus, leader election, ROS 2, MAVLink,
autopilot integration, SITL, QUBO, QAOA, or quantum simulation. Later milestones
add deterministic immutable-evidence flooding and authoritative receiver-local
task control, but not dynamic task discovery, resource/role models, route
planning, relay movement, or the other capabilities in that list.
