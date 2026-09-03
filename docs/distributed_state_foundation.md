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
| **World / physics** | Physical `Agent` objects, actual positions, responsiveness, the current `CommunicationGraph`, and mutable `Task` records maintained as observer projections | Supply a remote UAV's live state, graph truth, or projected task owner to local decision logic |
| **Agent-local control** | Own physical state, a frozen `TaskObjective` catalogue, `ReceiverLocalTaskUtility`, one `PeerStateStore`, receiver-local receipt times, failure evidence, one `TaskClaimStore`, and a cached execution intent | Infer failure from silence alone, copy topology/task-owner truth, or execute without local `owns_task()` |
| **Observer/evaluation** | Metrics, traces, visualisation, assertions, and tests that compare belief with world truth | Feed its privileged view back into allocation or failure decisions |

World truth is necessary. Physics must know where vehicles really are, the
network model must know which links can carry traffic, and `Mission` maintains
observer-facing lifecycle projections after valid local decisions. The invariant
is about the direction of information flow: privileged state may be observed for
evaluation, but it may not become undeclared peer knowledge or execution
authority.

Two short rules capture the most important consequence:

```text
missing heartbeat != vehicle failure
unreachable       != failed
```

## Before and after this hardening sequence

The original direct-delivery design combined two limitations:

```text
direct-link protocol delivery
perfect pair reachability copied from CommunicationGraph into local peer state
```

The reachability oracle was removed before the current dissemination change. In
the immediately preceding design, local belief was already derived only from
delivered heartbeats and elapsed receiver-local time, but immutable task and
failure evidence still stopped after one hop.

After this milestone:

```text
world/network model decides whether each physical hop succeeds
                    |
                    v
delivered evidence + receiver-local time update local belief

immutable task/failure evidence floods across a connected multi-hop component
```

Heartbeats remain one-hop because receipt is intended to mean first-hand contact
with their source. Claims, releases, completions, failure votes, and failure
declarations are immutable facts and may be forwarded. Thus:

```text
network connectivity != direct connectivity
```

A connected component can converge without becoming a full clique, provided it
remains connected long enough for deterministic dissemination. A partition still
prevents evidence crossing the cut.

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

The link layer exposes **no** local fact about link state. A receiver learns
only what it actually receives: a delivered snapshot and the time it arrived.
An earlier `synchronize_link_evidence()` adapter published a per-pair "can
deliver" boolean into peer stores; it was removed because a radio cannot supply
that fact, and its presence made false positives structurally impossible.

Failure handling has two stages:

1. A local replica may create a `FailureVote` only when it has a previously
   delivered observation and has heard nothing from that peer for strictly
   longer than `heartbeat_timeout`, measured from the observation's
   receiver-local arrival time. Because silence is ambiguous, this can suspect
   a healthy peer across a partition, by design.
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

Votes and declarations do not use the heartbeat's one-hop semantics. A vote
enters its creator's mailbox locally, then immutable protocol evidence is
flooded through eligible intermediate UAVs. Every node preserves the original
voter or declarer identity; transport-level receipts separately identify the
forwarding and receiving UAVs. Locally originated evidence is retained for
retry. `Mission.detect_and_recover()` accepts each newly established target once
and applies world-state task release/recovery only after a valid certificate,
never from a raw timeout or graph transition.

Ongoing silence can re-create and publish a vote on a later failure-protocol
round, and locally originated declaration certificates are retransmitted. A
receiver's vote mailbox is duplicate-tolerant by structural message identity and
voter generation. Its retention window is measured from that receiver's first
local receipt, so a duplicate does not refresh an old vote. Restoring a graph
link changes no belief by itself; only a newly delivered heartbeat restarts the
receiver-local silence interval.

A quorum certificate is corroborated belief, not proof of physical failure. In
this model there is no positive remote-health signal that can distinguish a
silent failed UAV from a healthy UAV behind a partition. Consequently, several
receivers can independently time out the same healthy peer and form a false
declaration. `SILENT` alone never becomes `DECLARED_FAILED`; the certificate is
required, and first-hand contact plus declaration retraction mitigate mistakes.
Eliminating this ambiguity would require an additional observable signal rather
than access to graph truth.

The current protocol is deliberately small and deterministic. It demonstrates
the evidence boundary; it is not a claim of Byzantine tolerance, production
consensus, or flight-safety-grade failure detection. `HeartbeatTimeout` remains
an import-compatible alias for `FailureDeclaration`, but timeout alone is no
longer its semantic meaning.

## Deterministic store-and-forward flooding

Only immutable protocol evidence is forwardable:

- `TaskClaim` uses `(task_id, owner_agent_id, epoch)`;
- a `TaskClaimRelease` uses its message kind plus the exact losing claim identity;
- `TaskCompletionEvidence` uses its message kind plus the exact completed claim;
- a `FailureVote` identifies its voter, suspected UAV, and evidence generation;
- a `FailureDeclaration` identifies its suspected UAV, declarer, and certificate.

These structural identities are content-derived and stable. They are not random
packet IDs. Each receiver keeps finite seen state for each logical message. A
new message is applied locally once and forwarded over eligible neighbours in
sorted order; a duplicate can be counted in observer data but cannot refresh
evidence, mutate a store again, or trigger another unbounded broadcast. For each
finite dissemination batch, finite membership and per-message/per-node seen
state guarantee termination without a TTL. An unsuccessful directed route stays
pending until that hop succeeds or the receiver learns the same message through
another route, so later topology changes trigger deterministic retry.

The public transport participation set names replicas allowed to receive or
relay in that round. A logical origin explicitly included in the current source
batch may seed and transmit even if it is omitted from the receive/relay set; an
already retained origin omitted on a later empty call cannot run its pending
routes until it participates or publishes again. Normal `Simulation` source
batches include responsive participants only, so a fail-stop UAV stops both
renewing and transmitting.

Forwarding metadata belongs to a transport envelope or receipt, not the domain
evidence:

```text
origin_agent_id     logical author; never changes
forwarder_agent_id  UAV that transmitted this hop
receiver_agent_id   UAV that accepted this hop
hop_count           number of physical hops from origin
```

Observer-only batches and the `PROTOCOL GOSSIP` summary classify logical work:

| Summary label | Batch meaning |
| --- | --- |
| Logical forwarding attempts | sender, receiver, and message were eligible for a graph delivery evaluation |
| Successful first deliveries | this receiver learned the structural message ID for the first time |
| Unavailable-link attempts | eligible evaluation found no active direct link; together with successful deliveries this partitions attempts |
| Useful first deliveries | the first transport receipt also changed receiver-local domain state rather than carrying obsolete evidence |
| Forwarded first deliveries | successful first receipt came from a hop sender other than the immutable logical origin |
| Duplicate source publications | an origin submitted a structural ID already seeded into transport |
| Duplicate routes suppressed | another path became redundant because the receiver already knew that ID |
| Inactive-endpoint deferrals | a newly observed `(message ID, receiver)` obligation had no eligible active endpoint and was retained without entering attempts |

The compatibility property `duplicates_suppressed` is the sum of duplicate
source publications and duplicate-route suppressions. The versioned JSON trace
uses `protocol_messages_dropped` for unavailable-link attempts and also exposes
the classified fields. These counters support tests and demonstrations but never
feed failure detection, task selection, or ownership.

They do **not** count RF packets, bytes, bandwidth, queue occupancy, airtime, or
retransmission cost. This is audited flooding for small swarms, not route
discovery or a MANET protocol.

The transport retains seen IDs and pending logical routes for its lifetime; it
does not yet garbage-collect superseded evidence. Memory can therefore grow as
`O(messages * N^2)` in an unbounded run. Delivery counters describe logical
directed-route attempts and suppressions, not bytes or modeled wire packets.
Each transport also assumes that its configured store set and `FailureManager`
remain the same instances for its lifetime.

## Clock domains

Source and receiver time have different meanings:

| Field | Clock domain | Valid use |
| --- | --- | --- |
| `Heartbeat.timestamp` | heartbeat source | snapshot generation metadata and comparison with another heartbeat from that same source |
| `FailureVote.last_heartbeat` / `FailureDeclaration.last_heartbeat` | suspected heartbeat source | evidence-generation metadata shared by observations of that heartbeat |
| `FailureVote.created_at` / `last_heard_at` | voter | immutable vote identity and voter-local source history |
| `FailureDeclaration.detected_at` | declarer | certificate source metadata |
| task evidence `created_at` | claim/release/completion author | immutable source history; same-owner ordering where applicable |
| `received_at` | receiver | silence, vote retention, freshness, and lease age |

A receiving replica uses `local_now - received_at`. Exact duplicates of
forwardable protocol evidence retain the first receipt time and cannot extend a
vote window or lease. Each successfully delivered heartbeat is instead new
first-hand contact and refreshes the local liveness observation. Comparisons
within one source stream remain meaningful; comparisons between a remote source
time and receiver-local time do not authorize decisions.

All UAV components currently run from one deterministic simulator clock. That
lets observer-only traces calculate transport latency and keeps tests exact, but
it is a simulator convenience rather than an assumption that physical UAV clocks
will be synchronized.

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
| `DECLARED_FAILED` | A validated quorum-backed failure declaration was applied locally. |

There is deliberately no status meaning "the link is down". Cutting a link
produces no local evidence whatsoever; only elapsed silence does, and that
silence is indistinguishable from the peer having been destroyed.

Status precedence is declaration, then recent heard evidence, then silence. A
successfully delivered heartbeat refreshes the snapshot and clears local
silence — and, if that peer had been declared failed, retracts the declaration,
because first-hand contact outranks a second-hand certificate. Only
`apply_failure_declaration()` can enter `DECLARED_FAILED`, and that method
validates the voter set and configured quorum.

The connectivity-aware allocator consumes `heard_observations`, not raw
`fresh_observations`. A snapshot can therefore remain freshness-`FRESH` for
diagnostics while being excluded from decisions because its complete status is
`SILENT` or `DECLARED_FAILED`.

Within `Simulation.run()`, the world orchestrator may use physical responsiveness
only to decide whether a UAV's local software executes at a timestamp. A
non-participating UAV does not emit or receive messages, advance the freshness
clock in its private store, or forward protocol evidence. This execution gate
does not tell any other replica why the UAV is absent.

## Link faults and balanced partitions

`CommunicationGraph` is an undirected graph. Each pair is stored in canonical
ascending order, and `can_deliver(a, b)` therefore has the same answer as
`can_deliver(b, a)`. Asymmetric loss is outside the current model.

In the default `range` mode, `CommunicationGraph.update()` combines three facts:

- whether the endpoints are within `communication_range`;
- whether either endpoint appears in the compatibility-oriented
  `blocked_agent_ids` set; and
- whether the canonical pair appears in `blocked_links`.

`blocked_links` accepts either endpoint order and normalises duplicates. The
argument describes the fault policy for that update; omitting it on a later
update restores any links that distance and whole-agent policy permit.

The pre-existing optional `radio` mode replaces the range threshold with a
free-space line-of-sight path-loss/SNR/BPSK-BER calculation. Its hard mode is
deterministic; optional stochastic link sampling uses a seeded stream consumed
in canonical pair order. It does not model interference, terrain, fading,
antennas in context, or measured packet delivery, so it must not be described as
realistic RF. Both link modes remain world/network models: their outcome gates a
physical hop and is never copied into a receiver's peer belief.

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

## Task control: immutable catalogue, local utility, replicated authority

Task discovery currently has an explicit, limited boundary. At simulation
construction, configured task IDs and positions are copied into frozen
`TaskObjective` values and sorted into `Simulation.task_objectives`. Every UAV
may use that immutable mission catalogue. There is no dynamic sensing or network
task-discovery protocol yet, and local planning never rereads mutable `Task`
fields to discover availability.

`ReceiverLocalTaskUtility` evaluates one objective from the receiver's own
position and additive non-negative costs:

```text
U_i(j) = w_distance * travel_i(j)
       + w_resource * resource_i(j)
       + w_communication * communication_i(j)
       + w_role * role_i(j)
```

Lower cost wins, with `task_id` as the stable tie-breaker. The default weights
select distance only. Resource and role are validated scalar/per-task extension
inputs and currently default to zero; they are not claims that resource sensing
or role assignment already exists.

The `connectivity` option assigns a dominant communication weight. For receiver
`i`, communication cost is the configured peer count minus the degree predicted
at the objective from positions in **that receiver's** `HEARD` snapshots. It
never reads `CommunicationGraph`, another live `Agent.position`, or raw-fresh but
silent peer data.

Mutable `TaskStatus` and `Task.assigned_agent` now form an observer-facing
mission projection:

```text
UNASSIGNED --assign--> ASSIGNED --complete--> COMPLETED
     ^                     |
     +--------release------+  failure recovery
```

`Mission` updates this projection after a locally authorized bind, release, or
completion so metrics and visualizations remain readable. It is not an input to
claim selection, ownership reconciliation, or execution authorization. A single
mutable owner field cannot represent legitimate partition-local split ownership.

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
  created claims, graph-delivered or forwarded peer claims, the latest accepted
  claim for each owner, local receipt times, releases, reconciliation results,
  and any accepted completion evidence.
- `S_ij` is the six-state interpretation derived only from `K_ij`, the
  receiver's own identity, and its local control time.

Neither `K_ij` nor `S_ij` contains or consults the projected `Task` owner or a
remote live `Agent.current_task`. Consequently, two UAVs can legitimately
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
the communication model delivers the value, possibly through intermediate UAVs;
there is no Mission-side loop that writes every store. A forwarding UAV does not
become the owner and does not rewrite the claim epoch.

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

The protocol uses receiver-local acceptance time for control decisions; it does
not sleep or compare wall clocks. A duplicate delivery retains the original
local acceptance time. A genuinely new renewal receives its own acceptance time
and therefore a new freshness and lease interval.

`created_at` is source-clock metadata. `received_at` belongs to the receiver's
clock domain. A remote `created_at` is not compared with receiver-local time to
decide freshness or lease validity. The deterministic simulator currently gives
all components one logical clock, so observer code may compare source and receipt
metadata when reporting latency. That convenience is not a synchronization
requirement on a future physical implementation.

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
- a release tombstone prevents a delayed copy of the released claim from
  resurrecting ownership.

Same-owner source timestamps may validate the internal order of a claim and its
release or completion. These checks make arrival order irrelevant within an
owner's claim stream without comparing a remote source clock with the receiver's
clock or treating a large counter from another owner as globally newer.

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
   losing generation's local lease is valid. Releases then flood through normal
   graph-mediated forwarding and retain tombstones against delayed replay.

After reconciliation, the winner reports `OWNED_BY_SELF`; other receivers
report `CLAIMED_BY_PEER_FRESH` or `CLAIMED_BY_PEER_STALE` according to their own
local age. Claim receipt and reconciliation are distinct domain operations. The
standalone claim demo places trace capture between them so a real `CONTESTED`
frame is visible; the normal mission currently performs them back-to-back before
its next trace frame. No authoritative `Mission` lookup or central arbitration
participates in selection.

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
is idempotent. Malformed completion evidence is rejected before it can change
belief; its source timestamp does not control receiver-local age.

Completion is graph-delivered belief, not an inference from the mutable observer
task projection. Different components may therefore learn completion at
different times, but every receiver that accepts it remains terminal thereafter.

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
5. The established CLI demo restores the full clique without resetting stores.
   Claims are delivered normally, and receivers that know both current claims
   visibly enter `CONTESTED`.
6. At a separate deterministic reconciliation phase, each receiver selects the
   lower owner ID from the same per-owner-superseded candidate set.
7. The losing claimant stops acting as owner and publishes release evidence at
   the next explicit release phase. Peers forward and apply it idempotently and
   converge on exactly one current owner.
8. Subsequent protocol and mission boundaries continue without a restart;
   delayed losing claims cannot resurrect the conflict, and accepted completion
   remains terminal.

The distinct observe, reconcile, and release phases are important for
traceability. They let a trace show the real `CONTESTED` interval, the selected
winner, and the later losing release instead of collapsing all three facts into
one observer snapshot.

A separate focused integration test reconnects the same kind of split brain as
`1 <-> 2 <-> 3 <-> 4`. It proves that claims and the losing release converge
through intermediate UAVs; the full-clique CLI topology is not being used as the
multi-hop proof.

## Deterministic scheduler order

Ordering is part of the model because two events can occur at the same logical
timestamp. At a normal event boundary, `Simulation.run()` performs:

1. claim-authorized physical movement, or a non-mutating claim-authorized
   position projection for a communication-only boundary;
2. scheduled physical failure injection;
3. scheduled communication-fault start or restoration;
4. world-level graph recomputation, with no topology fact written to a peer
   store;
5. peer freshness advancement;
6. heartbeat creation and one-hop delivery when a heartbeat is due;
7. local failure-vote creation and multi-hop evidence dissemination;
8. local quorum detection and multi-hop declaration dissemination;
9. observer recovery effects for newly established declarations, without
   revoking or replacing claims centrally;
10. a distributed task-control round: advance local leases, stand down invalid
    intents, renew actionable claims at their freshness threshold, batch local
    utility choices, create claims, gossip, reconcile, activate local owners,
    and repeat without motion until the claim round reaches a fixed point;
11. completion for arrivals that still pass `task_is_executable()`; terminal
    evidence is created before the mutable task projection changes;
12. when any completion occurred, another task-control round to disseminate it
    and let newly idle UAVs replan at the same timestamp; and
13. position recording, the scheduler's consistency checkpoint, and observer
    trace capture.

Metrics are recorded alongside the transition or delivery they measure, not
deferred wholesale to the final observer step.

Every physical action reaches `Mission.task_is_executable()`: the UAV must be
responsive, its `current_task` cache must name that objective, and its own store
must report `OWNED_BY_SELF`. The same predicate gates actual motion, projected
motion, arrival handling, renewal, and completion. Thus a bare pointer cannot
perform work, and a lost claim is cleared before another action.

Receipt and reconciliation remain distinct domain operations; the standalone
claim demo captures the `CONTESTED` boundary before selecting a winner. The
normal fixed-point round may perform them back-to-back before trace capture. A
losing local owner creates exact release evidence, stands down, and can choose a
different locally available objective without inserting a movement step between
those decisions.

A communication-only boundary stops after observation, metrics, and tracing; it
does not add a task-claim round, completion, or failure-protocol tick. At startup,
`Mission.start(0)` creates baseline snapshots and deliberately skips the legacy
allocator. `Simulation.run()` next builds the graph, injects any physical failure
scheduled at `t=0`, applies any startup communication fault, delivers the
already-created snapshots, and runs the first receiver-local claim round. This
contract gives the detector a baseline for a time-zero fail-stop while ensuring
that no task is bound before its claim becomes locally authoritative.

## Invariants worth testing directly

- Emitting a heartbeat does not update another UAV or the failure protocol;
  successful delivery does.
- Cutting a link produces no immediate local evidence, and later `STALE`, but cannot by
  itself produce `DECLARED_FAILED`.
- A `SILENT` peer remains distinct from a failed peer until a valid vote quorum
  exists.
- A balanced partition preserves traffic inside both components and blocks all
  cross-component traffic.
- Restoring links permits evidence to flow again without resetting the mission
  or peer stores, but the graph transition itself changes no receiver belief.
- A newly received heartbeat restarts that receiver's silence interval; mere
  restoration of a suspect link does not.
- One observer cannot declare a failure, and votes too old to meet the local
  timeout window cannot complete a quorum.
- Receiver-local connectivity utility uses the last delivered peer position only
  while that peer's complete local status is `HEARD`, never graph truth or a live
  remote `Agent.position`.
- A stale but lease-valid task claim remains a stale claim; only explicit
  release or strict lease expiry permits `UNCLAIMED`.
- A repeated immutable claim does not refresh its age; only a new owner-scoped
  epoch is a renewal.
- Owner-scoped epochs supersede old claims from the same owner and are never
  compared across different owners.
- In a chain, immutable task and failure evidence can reach a non-neighbour
  through an intermediate UAV while the logical origin remains unchanged.
- Cyclic flooding terminates; a duplicate neither mutates a receiver twice nor
  refreshes its receiver-local evidence age.
- Evidence cannot cross a disconnected cut, but persistent retry lets it reach
  the rest of the component after a bridge reconnects.
- Given the same latest lease-valid claim per owner, every receiver selects the
  lowest owner ID, independent of message arrival order.
- Conflict observation, winner selection, and losing release remain explicit
  domain phases; the standalone task-claim trace captures `CONTESTED` between
  receipt and reconciliation.
- Once a receiver accepts completion evidence, its state for that task remains
  `COMPLETE` under every later claim, release, duplicate, or reconnection.
- Local claim choices are batched from pre-mutation views, so agent traversal
  order cannot silently allocate a task.
- A claim is created before `current_task` is bound, and a bare `current_task`
  cannot move, project, renew, or complete.
- Renewal occurs when an actionable local claim reaches its freshness threshold,
  not on every scheduler tick.
- Losing reconciliation, release, strict expiry, or learned completion clears
  execution intent before further motion and permits receiver-local replanning.
- A fail-stop owner ceases renewal; another UAV cannot replace it until the
  replacement UAV's received claim copy is strictly lease-expired.
- Mutating `Task.status` or `Task.assigned_agent` cannot alter catalogue ranking,
  create a claim, or authorize execution.
- `Mission` may apply physics and update observer projections after distributed
  evidence exists, but it may not arbitrate a task-claim conflict or supply a
  global owner.
- Metrics and trace code may compare belief with truth, but may not write belief
  or select an action.

## Authoritative distributed task control

Normal `Simulation` construction enables `distributed_task_control`. It creates
one store per UAV, a frozen objective catalogue, and one pure local utility
implementation. `Mission.start()` skips centralized allocation, and
`Mission.allocate_tasks()` rejects calls in this mode. The operational path is:

```text
immutable TaskObjective catalogue + own state + delivered local evidence
        |
        v
receiver-local additive utility
        |
        v
claim intent -> create_claim() before binding work
        |
        v
deterministic multi-hop dissemination
        |
        v
local reconciliation and ownership decision
        |
        v
bind current_task intent -> execute only while owns_task()
```

Each responsive idle UAV selects its best locally claimable objective. All such
choices are collected before any store changes, then claims are created and
flooded. Simultaneous claims are allowed: disconnected components can each
execute a locally valid owner, while connected replicas deterministically retain
the lowest owner ID and losing UAVs stand down before more motion. The round
repeats to a bounded fixed point so losers can replan without a central matching
pass.

`Agent.current_task` is now only the cached intent selected after local
ownership resolution. `Mission.task_is_executable()` combines that cache with
responsiveness and `owns_task()`, and all actual/projected movement, renewal,
arrival, and completion paths use it. Voluntary release publishes a tombstone;
lease expiry or reconciliation loss clears the cache; received completion is
absorbing, forces any duplicate executor to stand down, and blocks future
claims. Mutable `Task` fields follow these outcomes for observers but do not feed
them.

A physical failure removes that UAV from the participant set immediately, so it
cannot move, relay, renew, or complete. A later failure declaration may update
the observer task projection, but it does not revoke claim evidence. Survivors
must wait until their own received copy of the failed owner's lease is strictly
expired before claiming and activating the orphaned objective.

The centralized `TaskAllocator` and `CommunicationAwareTaskAllocator` remain
available as baseline implementations. They are intentionally outside the
normal operational path and must not become a second ownership authority.

## Counter audit and current default

The classified counters correct a misleading earlier total. In the audited
pre-fix default run, telemetry showed 15,075 logical attempts and 474 successes.
The graph was a clique: the complete 14,601 difference came from repeatedly
evaluating delivery toward failed, inactive UAV 2, and instrumentation classified
97.1% of those evaluations as repeats. Deduplication exposed 141 unique
`(message ID, inactive receiver)` obligations. These are now retained as
inactive-endpoint deferrals and excluded from link attempts.

After receiver-local task control, freshness-threshold renewal pacing, and
removal of an unnecessary second ownership round on non-completion ticks, the
default run completes 20 / 20 tasks in 17.25 s. Peer-state delivery is
`174 / 132 / 42`. Protocol gossip reports 178 eligible attempts, 178 successful
and useful first deliveries, zero unavailable-link attempts, zero forwarded
first deliveries, 1,552 duplicate source publications, 428 duplicate-route
suppressions, and 53 new inactive-endpoint deferrals. Task 19 is activated by its
replacement at `t = 10.75 s`, after local lease expiry. Zero forwarded deliveries
is expected for this full-clique default; chain tests remain the multi-hop proof.

## Formal receiver-local autonomy layer

The next layer is now implemented in `eudis_swarm.autonomy` without replacing
the stores described above. Each UAV owns a `LocalAutonomyKernel` composed from:

- one five-state `ContactEFSM` per peer, driven by successful local receipts and
  receiver-local elapsed time;
- one executable `PeerAvailabilityEFSM` mapping per peer that preserves
  `HEARD`, `SILENT`, and `DECLARED_FAILED` semantics;
- one six-state `TaskOwnershipEFSM` conformance view per task over the existing
  `TaskClaimStore` ledger; and
- one `CoordinationModeEFSM` per UAV with `COOPERATIVE`, `DEGRADED`,
  `LOCAL_AUTONOMY`, and `RECONCILING` states.

The pure transition signature is conceptually:

```text
delta(control state, typed extended variables, typed local event)
    -> (next state, next variables, requested effects, guard, reason)
```

Requested effects are data. Reducers do not move vehicles, send packets, change
network truth, mutate another replica, or read observer state. No dispatcher
consumes these requests yet; the simulation's existing claim orchestration
continues its established actions independently. Coordination mode is advisory,
and task execution authority remains `TaskClaimStore.owns_task()`.

The contact machine is deliberately distinct from the physical radio model. A
graph link restoration, component change, distance, SNR, BER, jammer boundary,
or failed attempt is not a local contact event. Successful protocol receipts are
attributed to their immediate physical forwarder, never to an immutable
message's logical origin. Recovery requires receipts at distinct receiver-local
times, preventing one same-timestamp message burst from creating false
stability.

Trace schema 3 records ordered control-state changes and current per-UAV contact
and coordination state. These records remain observer-only. Canonical tables,
the exact guards/effects, bounded model checks, and integration limitations are
in [the autonomy EFSM reference](autonomy_efsm.md).

## Deliberately deferred

This milestone implements a small receiver-local intent and execution controller,
not a dynamic task-discovery, auction, fleet-resource, or role-assignment system.
The frozen catalogue is preloaded, and resource/role utility terms are extension
points with zero default cost. CBBA, bidding, path planning, and global
task-utility optimisation remain separate future work.

Also deferred are route discovery and optimization, delivery queues, modeled
latency/bandwidth, acknowledgements, asymmetric links, Byzantine behavior,
simultaneous failures, aircraft dynamics, ROS 2, MAVLink, ArduPilot, Gazebo, 3D
visualisation, and quantum optimization. The existing optional free-space
SNR/BER link equation and seeded packet sampling remain simplified abstractions;
terrain, interference, measured channel calibration, and realistic RF are not
implemented.

The exact next milestone is a versioned Mission Contract plus a composed Mission
EFSM above the current distributed runtime. It should express operator intent,
constraints, priorities, degradation policy, and explicit mission phases without
turning centralized planning into runtime task authority. Coordinated search,
dynamic role/connectivity support, physical topology repair, handoff, safety,
experiments, quantum-assisted initial planning, and SITL remain later milestones.
