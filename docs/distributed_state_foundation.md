# Distributed-state foundation

The distributed-state milestones separate what the simulation knows from what
each UAV can legitimately know. This guide gives newcomers one place to
understand how network delivery, peer status, failure declarations, and the
receiver-local task-ownership protocol fit together.

## The boundary to preserve

The simulator contains three different kinds of state. They coexist in one
Python process for deterministic testing, but they must not be treated as one
shared pool of knowledge.

| Layer | May contain | Must not do |
| --- | --- | --- |
| **World truth** | Physical `Agent` objects, actual positions, responsiveness, task records, and the current `CommunicationGraph` | Supply a remote UAV's live state directly to that UAV's decision logic |
| **Agent belief** | One UAV's `PeerStateStore`, delivered `Heartbeat` snapshots, local link evidence, locally received failure votes/declarations, and its own task-claim, release, and completion evidence | Infer physical failure from silence or copy another UAV's authoritative `Agent`/`Task` state |
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
vote, declaration, task claim, release, or completion evidence crosses between
the two components. Calling
`graph.update(positions)` later reconnects all otherwise available pairs
without restarting the graph or mission.

The command-line fault schedule still models one whole-UAV communication
outage. Link-level faults are currently a core `CommunicationGraph` API for
deterministic tests and future scenario scheduling.

## Task ownership: world state versus local belief

`TaskStatus` remains the authoritative mission lifecycle:

```text
UNASSIGNED --assign--> ASSIGNED --complete--> COMPLETED
     ^                     |
     +--------release------+  failure recovery
```

That state is used by `Mission` to mutate and validate the simulated world. It
is not an agent-relative ownership belief and it is not an input to distributed
claim reconciliation.

`TaskOwnershipState` is the exact externally meaningful vocabulary for one
UAV's local interpretation of a task:

1. `UNCLAIMED`
2. `OWNED_BY_SELF`
3. `CLAIMED_BY_PEER_FRESH`
4. `CLAIMED_BY_PEER_STALE`
5. `CONTESTED`
6. `COMPLETE`

There is intentionally no `LEASE_UNKNOWN` state. Lease validity and evidence
freshness are internal evidence dimensions; they do not expand the public
six-state vocabulary.

For receiver UAV `i` and task `j`, this guide uses two symbols:

- `K_ij` is the complete receiver-local knowledge for that task: locally
  created claims, graph-delivered peer claims, the latest accepted claim for
  each owner, local receipt times, releases, reconciliation results, and any
  accepted completion evidence.
- `S_ij` is the six-state interpretation derived only from `K_ij`, the
  receiver's own identity, and deterministic simulation time.

Neither `K_ij` nor `S_ij` contains or consults the authoritative `Task` owner or
a remote live `Agent.current_task`. Consequently, two UAVs can legitimately
hold different `K_ij` sets and report different `S_ij` values while a partition
prevents evidence exchange.

`UNCLAIMED` means “UAV `i` currently has no lease-valid claim evidence for task
`j`,” not “nobody in the world owns the task.” `CLAIMED_BY_PEER_STALE` similarly
means that old but still lease-valid evidence exists; it is not permission to
take the task.

## Immutable claim evidence

A task claim is an immutable value message. Conceptually it identifies the
task, its claiming owner, one deterministic claim identifier, that owner's
per-task epoch, the configured lease information, and its creation time. It
contains integer identifiers and other copied protocol values, never pointers
or references to authoritative `Agent` or `Task` objects.

Local creation places the same immutable value in the creator's `K_ij` that a
peer would obtain after successful delivery. Peer knowledge changes only after
the communication model delivers the value; there is no global broadcast or
Mission-side loop that writes every store.

Claims are retransmittable data. Receiving the identical claim more than once
is idempotent and does not manufacture a renewal or extend its lease. Renewal
always creates a new immutable claim with the next owner-scoped epoch.

## Freshness, lease validity, and replacement

Freshness and lease validity use two distinct positive thresholds:

```text
fresh_after < lease_duration
```

For a claim's receiver-local age `a`, measured from the first local acceptance
of that exact immutable claim:

| Exact condition | Evidence meaning |
| --- | --- |
| `0 <= a <= fresh_after` | Fresh and lease-valid |
| `fresh_after < a <= lease_duration` | Stale but still lease-valid |
| `a > lease_duration` | Lease-expired and no longer a current ownership candidate |

The inequalities are intentional. A claim is still fresh exactly at
`fresh_after`, and its lease is still valid exactly at `lease_duration`.
Freshness changes only after the first boundary is crossed; replacement becomes
eligible only after the second boundary is crossed.

This gives `stale != invalid` a precise meaning. Missing one publication can
make evidence older without immediately freeing a task. A local UAV may create
a replacement claim only when the task is not locally `COMPLETE` and every
known incompatible claim is lease-expired or explicitly released. It may renew
its own current claim by creating the next immutable owner-scoped version.

The protocol uses deterministic simulation time and local acceptance times; it
does not sleep and does not compare wall clocks. A duplicate delivery retains
the original local acceptance time. A genuinely new renewal receives its own
acceptance time and therefore a new freshness and lease interval.

## Owner-scoped epochs and safe delayed delivery

Epochs are monotonic only within one `(task_id, owner_id)` stream. An owner
advances its epoch exactly once whenever it locally creates the first claim or
renews that task claim. Receiving, retransmitting, reconciling, or releasing a
claim never increments another owner's epoch.

Epochs from different owners are never compared. Owner 1 at epoch 8 is not
intrinsically newer or better than owner 3 at epoch 2 because those counters
describe independent histories. Cross-owner conflicts use the reconciliation
rule below, not an invalid global ordering of local counters.

A receiver accepts a gap in one owner's stream because intermediate renewals
may have been dropped by the graph. The modeled transport attributes a message
to its declared owner, and the local API is the only normal issuer; Byzantine
identity forgery remains out of scope. Even a malformed participant that jumps
to a large in-range epoch can suppress only its own older publications, never
gain priority over another owner. Values outside the positive signed 64-bit
range are rejected as impossible protocol versions.

For a single owner, a higher accepted epoch supersedes every lower epoch for
that task. The following safety rules apply before local state changes:

- an exact duplicate is accepted idempotently and does not refresh age;
- a delayed lower epoch cannot replace that owner's newer accepted epoch;
- the same owner, task, and epoch with a different claim identity or immutable
  payload is contradictory and is rejected safely;
- non-positive or otherwise malformed versions are rejected;
- evidence whose creation time is in the receiver's future is rejected; and
- a release tombstone prevents a delayed copy of the released claim from
  resurrecting ownership.

These checks make arrival order irrelevant within an owner's claim stream
without assuming synchronized clocks or treating a large counter from another
owner as globally newer.

## CONTESTED and deterministic reconciliation

After selecting the latest accepted claim separately for each owner, two or
more incompatible lease-valid owners make `S_ij = CONTESTED`. Freshness does not
break the tie: a stale-but-lease-valid claim remains a real contender.

Reconciliation is deterministic and uses only protocol evidence. From the set
of latest lease-valid per-owner claims, the claim with the lowest numeric
`owner_id` wins. Stable validated owner IDs provide a total ordering, so every
UAV given the same claim set selects the same winner regardless of delivery or
dictionary iteration order.

Conflict handling is explicitly two-phase:

1. **Observe conflict.** A receiver keeps all incompatible current claims and
   reports `CONTESTED`. Receipt alone neither records a winner nor erases losing
   evidence, which preserves an observable conflict boundary.
2. **Reconcile and release.** Reconciliation records the winner and suppresses
   the exact losing candidates in that receiver's interpretation. The local
   state therefore leaves `CONTESTED`; a losing local claimant immediately
   stops acting as owner and creates release evidence referencing its losing
   claim and the selected winner. A release can be authored only while that
   losing generation's local lease is valid. Releases then propagate through
   normal graph delivery and retain tombstones against delayed replay.

After reconciliation, the winner reports `OWNED_BY_SELF`; other receivers
report `CLAIMED_BY_PEER_FRESH` or `CLAIMED_BY_PEER_STALE` according to their own
local age. Claim receipt and reconciliation occur at distinct boundaries so a
real `CONTESTED` frame remains observable before this transition. No
authoritative `Mission` lookup or central arbitration participates in
selection.

## Monotonic completion

Valid completion evidence is terminal for one receiver and task. Once accepted,
it makes `S_ij = COMPLETE` and records a terminal marker in `K_ij`.

The local API creates completion evidence only while its exact claim is
`OWNED_BY_SELF` and lease-valid. The immutable completion carries that claim,
and its source-local creation time must fall within the same claim generation's
lease, making the evidence self-contained for later delivery.

Later claims, renewals, conflicting owners, releases, expired leases,
reconnection traffic, and duplicate or delayed messages cannot move that local
state away from `COMPLETE`. Repeated delivery of the same completion evidence
is idempotent. As with claims, future-dated or malformed completion evidence is
rejected before it can change belief.

Completion is graph-delivered belief, not an inference from the authoritative
world task. Different components may therefore learn completion at different
times, but every receiver that accepts it remains terminal thereafter.

## Local ownership state transitions

The compact transition diagram below describes the externally visible state;
freshness, lease expiry, winner selection, and tombstones remain internal
evidence details.

```text
UNCLAIMED --local valid claim----------------------> OWNED_BY_SELF
UNCLAIMED --valid peer claim-----------------------> CLAIMED_BY_PEER_FRESH
OWNED_BY_SELF --own release / lease expiry---------> UNCLAIMED
OWNED_BY_SELF --incompatible valid claim-----------> CONTESTED
CLAIMED_BY_PEER_FRESH --age > fresh_after----------> CLAIMED_BY_PEER_STALE
CLAIMED_BY_PEER_FRESH --incompatible valid claim---> CONTESTED
CLAIMED_BY_PEER_STALE --new peer epoch-------------> CLAIMED_BY_PEER_FRESH
CLAIMED_BY_PEER_STALE --release / age > lease------> UNCLAIMED
CLAIMED_BY_PEER_STALE --incompatible valid claim---> CONTESTED
CONTESTED --reconciliation selects self------------> OWNED_BY_SELF
CONTESTED --reconciliation selects peer------------> CLAIMED_BY_PEER_FRESH
CONTESTED --reconciliation selects stale peer------> CLAIMED_BY_PEER_STALE
ANY NON-COMPLETE STATE --valid completion evidence-> COMPLETE
COMPLETE --any later ownership input---------------> COMPLETE
```

A stale claim does not follow the `UNCLAIMED` transition until its age is
strictly greater than `lease_duration` or matching release evidence arrives.

## Balanced 2+2 split-brain lifecycle

The deterministic demonstration uses the existing four-agent topology without
embedding a four-agent assumption in core protocol logic:

1. UAVs `{1,2,3,4}` begin connected, and an initial claim reaches every local
   store through modeled delivery.
2. The cross-component links `(1,3)`, `(1,4)`, `(2,3)`, and `(2,4)` are blocked,
   producing components `{1,2}` and `{3,4}` while preserving internal traffic.
3. The original owner renews inside its own component. The other component
   cannot receive that renewal; its older evidence first becomes stale, remains
   unavailable for replacement through the exact lease boundary, and then
   expires.
4. After expiry, a UAV in the other component legitimately creates a replacement
   claim from its own `K_ij`. Both components now have useful but incompatible
   local ownership histories for the same task.
5. Reconnection restores the cross links without resetting stores. Claims are
   delivered normally, and receivers that know both current claims visibly
   enter `CONTESTED`.
6. At a separate deterministic reconciliation phase, each receiver selects the
   lower owner ID from the same per-owner-superseded candidate set.
7. The losing claimant stops acting as owner and publishes release evidence at
   the next explicit release phase. Peers apply it idempotently and converge on
   exactly one current owner.
8. Subsequent protocol and mission boundaries continue without a restart;
   delayed losing claims cannot resurrect the conflict, and accepted completion
   remains terminal.

The distinct observe, reconcile, and release phases are important for
traceability. They let a trace show the real `CONTESTED` interval, the selected
winner, and the later losing release instead of collapsing all three facts into
one observer snapshot.

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

Task ownership adds a separate deterministic evidence-flow contract. A protocol
round first advances receiver-local claim age, then creates any locally allowed
claim or renewal, routes immutable evidence through the current graph, and
derives `S_ij`. A `CONTESTED` result is observed before a later reconciliation
phase records a winner; a losing local owner creates release evidence only in
the following explicit release phase. This ordering is protocol semantics, not
permission for `Mission` to inspect every store and settle a conflict.

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
- A stale but lease-valid task claim remains a stale claim; only explicit
  release or strict lease expiry permits `UNCLAIMED`.
- A repeated immutable claim does not refresh its age; only a new owner-scoped
  epoch is a renewal.
- Owner-scoped epochs supersede old claims from the same owner and are never
  compared across different owners.
- Given the same latest lease-valid claim per owner, every receiver selects the
  lowest owner ID, independent of message arrival order.
- Conflict observation, winner selection, and losing release remain explicit
  phases, so `CONTESTED` is observable rather than transient hidden state.
- Once a receiver accepts completion evidence, its state for that task remains
  `COMPLETE` under every later claim, release, duplicate, or reconnection.
- `Mission` may observe and apply world effects after distributed evidence
  exists, but it may not arbitrate a task-claim conflict.
- Metrics and trace code may compare belief with truth, but may not write belief
  or select an action.

## Deliberately deferred

This milestone establishes distributed ownership evidence, not a full
distributed task allocator. `TaskAllocator` and
`CommunicationAwareTaskAllocator` remain centralized reference policies for
baseline comparison. CBBA, bidding, path planning, and global task-utility
optimisation remain separate future work; none is needed to make claims,
leases, split-brain, and reconciliation correct.

Also deferred are multi-hop routing and forwarding, queued/reliable delivery,
acknowledgements, stochastic packet loss, asymmetric links, Byzantine behavior,
agent rejoin, simultaneous failures, RF propagation, aircraft dynamics, ROS 2,
MAVLink, ArduPilot, Gazebo, 3D visualisation, and quantum optimization.
