# Distributed-state foundation

This milestone separates what the simulation knows from what each UAV can
legitimately know. It also gives newcomers one place to understand how network
delivery, peer status, failure declarations, and task-ownership evidence fit
together.

## The boundary to preserve

The simulator contains three different kinds of state. They coexist in one
Python process for deterministic testing, but they must not be treated as one
shared pool of knowledge.

| Layer | May contain | Must not do |
| --- | --- | --- |
| **World truth** | Physical `Agent` objects, actual positions, responsiveness, task records, and the current `CommunicationGraph` | Supply a remote UAV's live state directly to that UAV's decision logic |
| **Agent belief** | One UAV's `PeerStateStore`, delivered `Heartbeat` snapshots, local link evidence, locally received failure votes/declarations, and local task evidence | Infer physical failure from silence or copy another UAV's authoritative `Agent`/`Task` state |
| **Observer/evaluation** | Metrics, traces, visualisation, assertions, and tests that compare belief with world truth | Feed its privileged view back into allocation or failure decisions |

World truth is necessary. Physics must know where vehicles really are, the
network model must know which links can carry traffic, and `Mission` must apply
the resulting world-state mutation once a valid decision exists. The invariant
is about the direction of information flow: privileged state may be observed
for evaluation, but it may not become undeclared peer knowledge.

Two short rules capture the most important consequence:

```text
missing heartbeat != vehicle failure
unreachable       != failed
```

## Heartbeat and failure-declaration flow

`Mission.exchange_heartbeats()` asks each responsive source UAV to create an
immutable snapshot. It does not record that snapshot in a central liveness
cache. The snapshot becomes evidence for another UAV only if
`PeerStateTransport.deliver()` successfully carries it over an available
one-hop link to that receiver's `PeerStateStore`.

```text
source Agent
  -> immutable Heartbeat
  -> active CommunicationGraph one-hop link
  -> PeerStateTransport
  -> receiver's PeerStateStore
  -> receiver-local freshness and status
```

The link layer also exposes one narrow local fact through
`PeerStateTransport.synchronize_link_evidence()`: whether a direct pair can
currently deliver. It does not copy positions, connected components, physical
health, or task truth into a peer store.

Failure handling has two stages:

1. A local replica may create a `FailureVote` only when it has a previously
   delivered observation, the peer is locally `SILENT`, and the direct link has
   remained continuously reachable for strictly longer than
   `heartbeat_timeout`. The interval begins at the later of the observation's
   receiver-local arrival time and the start of the current reachable period.
2. A local replica may create a `FailureDeclaration` only after its own mailbox
   contains the required number of fresh votes, all referring to the same
   last-heartbeat timestamp and observed task ID. The declarer must be one of
   those voters.

For a configured swarm of `N` UAVs, the threshold is:

```text
required_votes = max(2, floor((N - 1) / 2) + 1)
```

`N - 1` is the number of possible voters after excluding the suspected UAV.
The two-vote floor is an intentional safety-over-liveness choice: one observer
never counts as consensus, so swarms with fewer than three configured UAVs
cannot declare failure. Three UAVs require both possible voters; four UAVs
require two of the three possible voters.

Votes and declarations use the same graph-mediated, one-hop transport as peer
snapshots. A vote enters its creator's mailbox locally, then is broadcast to
other eligible receivers; every locally originated declaration certificate is
retained and retried from its declarer to reachable peers. The transport does
not forward a certificate beyond that direct link. `Mission.detect_and_recover()`
accepts each newly established target once and applies world-state task
release/recovery without turning a raw timeout or link loss directly into
`AgentStatus.FAILED`.

Ongoing silence re-creates and broadcasts a vote on each failure-protocol
round, and locally originated declaration certificates are retransmitted the
same way. A receiver's vote mailbox is idempotent by voter, and a vote older
than `heartbeat_timeout` no longer counts toward a declaration; together those
rules let unchanged evidence cross a newly restored voter link without letting
an old partial quorum live forever. Restoring the suspect's own link also starts
a new full timeout grace period, so an old stale observation cannot trigger a
vote or declaration immediately after reconnection.

The current protocol is deliberately small and deterministic. It demonstrates
the evidence boundary; it is not a claim of Byzantine tolerance, production
consensus, or flight-safety-grade failure detection. `HeartbeatTimeout` remains
an import-compatible alias for `FailureDeclaration`, but timeout alone is no
longer its semantic meaning.

## `PeerKnowledgeState` and `PeerStatus` are different

Both enums are receiver-local, but they answer different questions.

`PeerKnowledgeState` describes the age of the last delivered snapshot:

| Value | Meaning |
| --- | --- |
| `UNKNOWN` | This receiver has never received a snapshot from that peer. |
| `FRESH` | The latest delivered snapshot has not exceeded `stale_after`. |
| `STALE` | A snapshot exists, but its receiver-local age is strictly greater than `stale_after`. |

`PeerStatus` interprets all currently available local evidence:

| Value | Meaning |
| --- | --- |
| `HEARD` | A fresh snapshot was received recently and no stronger status applies. |
| `SILENT` | No recent heartbeat evidence is available; this includes never-heard and timed-out cases. |
| `UNREACHABLE` | The receiver's local link evidence says the direct peer link cannot currently deliver. |
| `DECLARED_FAILED` | A validated quorum-backed failure declaration was applied locally. |

The two dimensions intentionally overlap. For example, a last snapshot can
still be `FRESH` while a newly blocked link makes `status_for(peer)` return
`UNREACHABLE`. Conversely, an old snapshot can be `STALE` while the link is up,
making the peer `SILENT`; only a full continuously reachable timeout can then
make that evidence eligible for a suspicion vote, never a declaration by
itself.

Status precedence is declaration, then link unreachability, then recent heard
evidence, then silence. A successfully delivered new heartbeat refreshes the
snapshot, supplies positive link evidence, and clears local silence. Only
`apply_failure_declaration()` can enter `DECLARED_FAILED`, and that method
validates the voter set and configured quorum.

The connectivity-aware allocator consumes `heard_observations`, not raw
`fresh_observations`. A snapshot can therefore remain freshness-`FRESH` for
diagnostics while being excluded from decisions because its complete status is
`SILENT`, `UNREACHABLE`, or `DECLARED_FAILED`.

Within `Simulation.run()`, the world orchestrator may use physical responsiveness
only to decide whether a UAV's local software executes at a timestamp. A
non-participating UAV does not emit or receive messages, advance the freshness
clock in its private store, or have new link evidence written into that store.
This execution gate does not tell any other replica why the UAV is absent.

## Link faults and balanced partitions

`CommunicationGraph` is an undirected graph. Each pair is stored in canonical
ascending order, and `can_deliver(a, b)` therefore has the same answer as
`can_deliver(b, a)`. Asymmetric loss is outside the current model.

`CommunicationGraph.update()` combines three facts:

- whether the endpoints are within `communication_range`;
- whether either endpoint appears in the compatibility-oriented
  `blocked_agent_ids` set; and
- whether the canonical pair appears in `blocked_links`.

`blocked_links` accepts either endpoint order and normalises duplicates. The
argument describes the fault policy for that update; omitting it on a later
update restores any links that distance and whole-agent policy permit.

For four in-range UAVs, a balanced partition is represented without disabling
any member's internal radio:

```python
cross_component_links = {
    (1, 3),
    (1, 4),
    (2, 3),
    (2, 4),
}

graph.update(positions, blocked_links=cross_component_links)
assert graph.connected_components == (
    frozenset({1, 2}),
    frozenset({3, 4}),
)
```

UAVs 1 and 2 can still exchange messages, as can UAVs 3 and 4. No heartbeat,
vote, or declaration crosses between the two components. Calling
`graph.update(positions)` later reconnects all otherwise available pairs
without restarting the graph or mission.

The command-line fault schedule still models one whole-UAV communication
outage. Link-level faults are currently a core `CommunicationGraph` API for
deterministic tests and future scenario scheduling.

## Task ownership: world state versus local evidence

`TaskStatus` remains the authoritative mission lifecycle:

```text
UNASSIGNED --assign--> ASSIGNED --complete--> COMPLETED
     ^                     |
     +--------release------+  failure recovery
```

That state is used by `Mission` to mutate and validate the simulated world. It
is not an agent-relative ownership belief.

`TaskOwnershipState` is the exact local vocabulary for future distributed task
decisions:

1. `UNCLAIMED`
2. `OWNED_BY_SELF`
3. `CLAIMED_BY_PEER_FRESH`
4. `CLAIMED_BY_PEER_STALE`
5. `CONTESTED`
6. `COMPLETE`

There is intentionally no `LEASE_UNKNOWN` state.

`classify_task_ownership()` accepts the deciding UAV's own current task, its
own `PeerStateStore`, and task IDs that it locally knows to be complete. It
classifies peer claims only from delivered heartbeat snapshots:

- one fresh peer claim becomes `CLAIMED_BY_PEER_FRESH`;
- one stale peer claim remains `CLAIMED_BY_PEER_STALE` rather than disappearing;
- a self claim becomes `OWNED_BY_SELF`;
- multiple claimant IDs, including self plus a peer, become `CONTESTED`;
- local completion evidence is terminal and becomes `COMPLETE`; and
- no locally visible claim becomes `UNCLAIMED`.

`UNCLAIMED` means “this UAV currently has no claim evidence,” not “the observer
has proved that nobody in the world owns the task.” The classifier does not
implement claim publication, leases, epochs, stealing, or reconciliation.

## Deterministic scheduler order

Ordering is part of the model because two events can occur at the same logical
timestamp. At a normal event boundary, `Simulation.run()` performs:

1. physical movement, or a non-mutating position projection for a
   communication-only boundary;
2. scheduled physical failure injection;
3. scheduled communication-fault start or restoration;
4. graph recomputation and receiver-local link-evidence synchronisation;
5. peer freshness advancement;
6. heartbeat creation and graph-mediated delivery when a heartbeat is due;
7. local failure-vote creation and delivery;
8. local quorum detection and declaration delivery;
9. authoritative recovery for declarations that now exist, including immediate
   recovery allocation and an internal consistency check;
10. task completion followed by the normal centralized allocation pass; and
11. position recording, the scheduler's consistency checkpoint, and observer
   trace capture.

Metrics are recorded alongside the transition or delivery they measure, not
deferred wholesale to step 11.

A communication-only boundary stops after observation, metrics, and tracing; it
does not add an allocation, completion, or failure-protocol tick. At startup,
`Mission.start(0)` creates baseline snapshots and then performs the legacy
initial allocation before the first graph-mediated delivery, so peer knowledge
remains unknown for that allocation. `Simulation.run()` next injects any
physical failure scheduled at `t=0`, applies any startup communication fault,
and only then delivers the already-created snapshots. This deliberate startup
contract gives the detector a baseline for a time-zero fail-stop; normal-boundary
failure-before-heartbeat ordering begins after startup.

## Invariants worth testing directly

- Emitting a heartbeat does not update another UAV or the failure protocol;
  successful delivery does.
- Cutting a link can produce `UNREACHABLE` and later `STALE`, but cannot by
  itself produce `DECLARED_FAILED`.
- A `SILENT` peer remains distinct from a failed peer until a valid vote quorum
  exists.
- A balanced partition preserves traffic inside both components and blocks all
  cross-component traffic.
- Restoring links permits fresh evidence to flow again without resetting the
  mission or peer stores.
- Restoring a suspect link starts a new timeout grace period; a pre-partition
  observation cannot cause an immediate vote or declaration.
- One observer cannot declare a failure, and votes too old to meet the local
  timeout window cannot complete a quorum.
- Agent-local connectivity scoring uses the last delivered peer position only
  while that peer's complete local status is `HEARD`, never a live remote
  `Agent.position`.
- A stale task claim remains a stale claim; it is not silently converted to
  `UNCLAIMED`.
- Metrics and trace code may compare belief with truth, but may not write belief
  or select an action.

## Deliberately deferred

This milestone does not turn the existing allocator into a distributed task
protocol. `TaskAllocator` and `CommunicationAwareTaskAllocator` remain
centralized reference policies, and `Mission` remains the sole authority that
applies their non-conflicting proposals. The local `TaskOwnershipState` seam is
present so a later claims protocol can be added without inventing vocabulary at
that point.

Also deferred are task claim messages, leases, epochs, partition reconciliation,
multi-hop routing and forwarding, queued/reliable heartbeat delivery,
acknowledgements, stochastic packet loss, asymmetric links, Byzantine behavior,
agent rejoin, simultaneous failures, RF propagation, aircraft dynamics, ROS 2,
MAVLink, ArduPilot, Gazebo, and quantum optimization.
