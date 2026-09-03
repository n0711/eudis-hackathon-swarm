# Contributing

Thanks for looking at EUDIS Swarm. This guide protects both reproducible results
and the boundary between simulated world truth and receiver-local evidence.

## The one rule that matters

The repository publishes deterministic scenario times, ownership transitions,
and message counts. Those are regression-tested. If your change moves one, that
is not automatically wrong — but it must be deliberate, explained in the pull
request, and the relevant documentation and tests must be updated in the same
commit. A silent change to a published figure is the one failure this project
cannot tolerate.

Determinism means: no wall-clock time, no unseeded randomness, no iteration
over unordered sets where order reaches an output, and no dependence on dict
insertion order that is not itself deterministic.

## Setting up

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,visualization,dashboard]"
```

If tests fail to import `eudis_swarm`, your editable install is stale — most
often because the repository was moved. Re-run the install command above.

## Before you open a pull request

Run the full local baseline. CI runs the same checks on Python 3.11 and 3.13.

```bash
python -m ruff check . && python -m ruff format --check . && python -m pyright && python -m pytest --cov=eudis_swarm --cov-report=term-missing && python -m build
```

Coverage is gated at 85% branch-aware. Pyright runs in `standard` mode and must
report zero errors.

## Architectural boundaries

These separations are load-bearing. Breaking one is a design change, not a
refactor, and needs discussion first.

| Boundary | Rule |
| --- | --- |
| World truth vs agent control | Physics, authoritative positions, topology, and mutable observer projections belong to the simulation. An agent-local decision may use self state, the immutable objective catalogue, and delivered/local evidence only. |
| Physical vs communication state | `IDLE`/`ACTIVE`/`FAILED` never derives directly from link state. `SILENT`, `STALE`, and `UNKNOWN` must never imply `FAILED`. |
| Belief vs physical truth | `status is FAILED` is what the swarm *believes*. Only `inject_failure()` stops a vehicle. A declaration must never touch `responsive`, movement, or transmission. |
| Silence vs declaration | A timeout may create a local `FailureVote`; only a validated quorum in one replica's mailbox may create `DECLARED_FAILED` and authorize recovery. |
| Emission vs delivery | Creating a heartbeat, vote, declaration, claim, release, or completion is not remote delivery. Heartbeats require a direct available link; immutable protocol evidence may cross a sequence of available links through modeled forwarding. |
| Direct vs connected | Two UAVs need not be direct neighbours for immutable evidence to converge. They must be in one connected component for enough deterministic dissemination work to complete. |
| Origin vs forwarder | Forwarding must not rewrite a claim owner, release author, vote voter, or declaration declarer. Transport metadata names origin, forwarder, receiver, and hop count separately. |
| Source vs receiver clock | Source timestamps are immutable metadata. Silence, vote retention, freshness, and lease expiry use the receiver's own `received_at`; never compare a remote clock with local time to authorize a decision. |
| Catalogue vs mutable projection | Frozen `TaskObjective` values are local planning input. `Task.status` and `Task.assigned_agent` are observer projections and must not select a claim or authorize execution. |
| Claim intent vs authority | Local utility may create a claim intent, but binding, motion, projection, renewal, and completion require the same UAV's `TaskClaimStore.owns_task()`. |
| Intent cache vs authority | `Agent.current_task` remembers locally selected work; by itself it authorizes nothing and must be cleared when ownership becomes unactionable. |
| Local vs global knowledge | The task-control layer may read only the deciding UAV's own state, immutable objectives, configured local costs, and its own delivered `PeerStateStore` copies—never graph truth, another live UAV, or a mutable task owner. The pure utility scorer receives derived costs, not a peer store. |
| Observation vs behaviour | Metrics and traces are derived from transitions. They never feed decisions back into the simulation. |

`Mission.assert_consistent()` enforces the selected ownership model. In normal
distributed control it requires every responsive cached intent to have local
claim authority without demanding global agreement during a partition. The
legacy path still checks bidirectional mutable task pointers. `Simulation` calls
the invariant at scheduler checkpoints; preserve it after every new compound
mutation path.

`Mission.exchange_heartbeats()` creates immutable source snapshots only. Do not
reintroduce a call that records them directly in `FailureManager`; receiver
heartbeat evidence must come from successful `PeerStateTransport.deliver()`
calls.

`CommunicationGraph` is allowed to use authoritative positions because it is
the world-level network model. **No topology fact may be copied into a peer
store at all.** A receiver learns only what it actually receives: a delivered
snapshot, and the time it arrived. There is deliberately no per-peer "is the
link up" flag, because a real radio cannot supply one — silence from a jammed
peer and silence from a destroyed peer are identical. An earlier
`synchronize_link_evidence()` adapter did exactly this and made the project's
central claim unfalsifiable; do not reintroduce it in any form.

Failure suspicion therefore requires only a previously delivered observation
and elapsed silence beyond the strict timeout. It follows that a healthy but
partitioned UAV *can* be wrongly declared dead. That is intended: the defence
is quorum plus ownership reconciliation, not privileged knowledge. Preserve
the threshold `max(2, floor((N - 1) / 2) + 1)`: the suspected UAV is excluded
from `N - 1`, and one observer must never be able to declare a peer failed.
Persistent transport routes make unchanged evidence retryable, while votes older
than the timeout at that receiver must not count toward a declaration. Locally
originated votes and declaration certificates remain available for
graph-mediated retries, but their world-state consequence is applied only once.

Heartbeats are intentionally not forwarded: receiving one is first-hand evidence
of a direct hop from its source. `FailureVote`, `FailureDeclaration`,
`TaskClaim`, `TaskClaimRelease`, and `TaskCompletionEvidence` are immutable and
may be forwarded. Give each logical message one structural identity, suppress
duplicates per receiver, and traverse messages and neighbours in stable order.
Intermediate nodes are transport hops, not new protocol authors. Finite
per-message/per-node seen state is the termination mechanism; do not add random
backoff or depend on set/dict iteration order.

For the low-level transport API, `receiving_agent_ids` is the active
receive/relay set. An origin explicitly present in the current publication batch
may seed and transmit its message even if omitted from that set; a retained
origin omitted on a later call stays inactive until it participates or publishes
again. The normal simulation publishes only from responsive participants, so a
fail-stop UAV cannot use this compatibility rule to keep transmitting.

Protocol metrics count logical evidence handling, not RF packets. Keep eligible
link attempts split into successful first deliveries and unavailable-link
attempts; keep useful domain mutations separate from successful transport; split
duplicate source publications from duplicate-route suppressions; and count an
inactive `(message, receiver)` obligation once as a deferral without adding it to
the attempt denominator.

World truth may decide whether a UAV's software can execute, but inactive
software must not mutate its private freshness state. That execution gate must
not reveal physical health to any other replica.

A declaration is evidence, not a verdict. First-hand contact outranks it: a
snapshot received straight from a peer that was declared dead retracts that
declaration locally, and a quorum of retractions withdraws it in the world.
Keep declarations reversible.

Task-claim epochs are scoped to one `(task_id, owner_agent_id)` publication
stream and must never be compared across owners. A local store advances only
its own stream by exactly one on creation or renewal; a larger remote-owner
epoch cannot buy cross-owner priority. Reconciliation first supersedes older
publications per owner and then selects the lowest validated owner ID, using no
`Mission`, live remote `Agent`, or authoritative `Task` field.

Keep claim receipt and reconciliation as separate domain operations. The
standalone task-claim demonstration captures them at separate observer boundaries
so `CONTESTED` is visible; the normal mission may run them back-to-back before
trace capture. A losing owner must stop acting from its own reconciliation result
and publish an exact-generation release tombstone through `TaskClaimTransport`;
do not erase every receiver's losing evidence from a central loop. Completion is
receiver local and absorbing once valid evidence is accepted.

Normal task control follows one path:

```text
immutable TaskObjective catalogue
  -> receiver-local additive utility
  -> create claim before binding work
  -> gossip and reconcile
  -> bind/execute only while owns_task()
```

Batch all idle UAV choices from their pre-mutation local views before creating
claims; otherwise traversal order becomes a hidden allocator. The default
utility is distance. Resource, communication, and role costs are structured
local extension inputs; absent models contribute zero. The connectivity option
may derive communication cost only from that receiver's `HEARD` delivered peer
positions. It must never query `CommunicationGraph` during scoring.

Actual movement, projected movement at communication-only boundaries, renewal,
and completion must all use `Mission.task_is_executable()`. Renew an actionable
claim only when its receiver-local age reaches the freshness threshold, not on
every tick. On release, strict expiry, reconciliation loss, or received
completion, clear the cached intent before any further physical action and allow
the UAV to replan locally. A failed UAV does not participate or renew; peers wait
for their own received copy to expire before replacement.

`created_at`, `detected_at`, and `Heartbeat.timestamp` describe the source's
clock. `received_at` is the receiving replica's control clock. The deterministic
simulator currently supplies one logical clock, which permits observer-only
latency calculations, but protocol decisions must remain valid if physical UAV
clocks are skewed. An exact duplicate of forwardable protocol evidence retains
its first local receipt time; a successfully delivered heartbeat is new
first-hand liveness evidence and refreshes its receipt time.

For the complete rationale and state vocabulary, read
[`docs/distributed_state_foundation.md`](docs/distributed_state_foundation.md)
before changing failure detection, peer state, messaging, task ownership, or
allocation.

## Adding task utility or a comparison policy

Operational task intent belongs in `ReceiverLocalTaskUtility` or in local cost
construction around it. Keep its public inputs limited to the UAV ID, own
position, frozen `TaskObjective` values, and validated local resource,
communication, or role costs. Preserve additive non-negative weights and stable
`(total_cost, task_id)` ordering unless deliberately changing the protocol.

The older centralized comparison seam is the `AllocationPolicy` protocol in
`task_allocator.py`:

Implement the `AllocationPolicy` protocol in `task_allocator.py`:

```python
def allocate(
    self, agents: Iterable[Agent], tasks: Iterable[Task]
) -> list[Allocation]: ...
```

Return unique, non-conflicting proposals and mutate nothing. These policies are
baselines; normal `Simulation` runs distributed task control and must not invoke
`Mission.allocate_tasks()`. If a new CLI policy should also affect operational
task intent, define its receiver-local utility mapping explicitly and cover both
the local path and the baseline comparison.

The `AllocationPolicy` interface is not a distributed claims protocol. Never
route one of its global proposals back into the normal execution path or create a
second ownership mechanism beside `TaskClaimStore`.

## Tests

New behaviour needs a test that would fail without it. Prefer exact assertions
on timestamps and counts over loose ones — this is a deterministic simulator,
so `assert duration == 17.25` is both legal and more useful than a tolerance.

Name tests after the property they protect, not the function they call.
For distributed-state changes, include a negative assertion: prove that the
forbidden shortcut—undelivered heartbeat refresh, silence-to-failure promotion,
cross-partition delivery, rewritten origin identity, duplicate reapplication, or
live-peer lookup—cannot occur. Include chain and cyclic topologies; a full clique
does not prove multi-hop convergence or loop safety.

For task-control changes, also prove that a bare `current_task` cannot move,
project, renew, or complete; mutable `Task` projection changes cannot alter local
selection; simultaneous claims converge and the loser stops before moving; and
a fail-stop replacement waits through the exact local lease boundary.

## Commits

Conventional-style prefixes (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
`chore:`) matching the existing history. One logical change per commit.

## Scope

This is a research prototype for a specific challenge. Contributions that add
real autopilot, flight-control, targeting, or vehicle-integration code are out
of scope and will be declined.
