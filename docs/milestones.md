# Implemented milestones

A development record: what was built, in what order, and why. It is history
rather than reference — for what the system does now, start with
[`onboarding.md`](onboarding.md); for measured behaviour, see
[`measured_results.md`](measured_results.md).

Paths below are relative to the repository root.

Everything here is implemented and regression-tested.

### Milestone 1 — Distributed State Hardening

Modules: [`communication.py`](../src/eudis_swarm/communication.py),
[`messaging.py`](../src/eudis_swarm/messaging.py),
[`peer_state.py`](../src/eudis_swarm/peer_state.py),
[`failure_manager.py`](../src/eudis_swarm/failure_manager.py),
[`task.py`](../src/eudis_swarm/task.py).

- **Graph-mediated failure protocol.** `FailureVote` and `FailureDeclaration`
  are immutable evidence flooded across the currently connected multi-hop
  component. Each receiver owns an isolated vote mailbox; evidence retains its
  origin while intermediate UAVs only forward it. Locally originated votes and
  declaration certificates are retained for deterministic retry.
- **Receiver-local peer state.** One `PeerStateStore` per UAV holds delivered
  `Heartbeat` snapshots, their receiver-local arrival time, and any applied
  declaration. Nothing is copied in from world truth — in particular there is
  no per-peer "is the link up" flag, because a radio cannot supply one.
- **Freshness vs. status, kept apart.** `PeerKnowledgeState` is snapshot age
  (`UNKNOWN` / `FRESH` / `STALE`). `PeerStatus` interprets all local evidence as
  `HEARD`, `SILENT`, or `DECLARED_FAILED`.
- **Silence is ambiguous, and the swarm can be wrong.** Nothing local
  distinguishes a jammed peer from a destroyed one, so a healthy but
  partitioned UAV *can* be declared dead by a quorum. The defence is not
  privileged knowledge but quorum, reversible declarations, and ownership
  reconciliation — and the cost of being wrong is measured, not hidden.
- **A declaration is belief, not a kill switch.** It never touches
  `responsive`: a wrongly declared UAV keeps flying, keeps transmitting, and
  keeps the task it does not know it lost. First-hand contact retracts the
  declaration when the link returns.
- **Quorum-based failure declaration.** For `N` configured UAVs the threshold is
  `required_votes = max(2, floor((N - 1) / 2) + 1)` — a strict majority of the
  possible voters with a two-vote floor. Swarms of fewer than three UAVs cannot
  declare a failure. The declaring replica must be one of the matching voters.
- **Arbitrary blocked links and balanced partitions.** The graph accepts a
  canonical set of undirected `blocked_links` alongside the existing whole-UAV
  fault; `can_deliver()` drives deterministic single-link, balanced 2+2, and
  reconnection cases.
- **Reconnect handling.** Restoring a link is world truth and changes no local
  belief by itself. A successfully delivered heartbeat restarts that receiver's
  silence interval. Failure-vote age is measured from receiver-local receipt, so
  an old partial quorum does not live forever and a duplicate does not refresh it.
- **No authoritative remote-position leakage.** Non-participating UAV software
  does not advance or receive updates to its private freshness state, and the
  connectivity task utility scores only from `HEARD` peer snapshots.
- **Six task-ownership states locked in.** `TaskOwnershipState` fixes the
  vocabulary — `UNCLAIMED`, `OWNED_BY_SELF`, `CLAIMED_BY_PEER_FRESH`,
  `CLAIMED_BY_PEER_STALE`, `CONTESTED`, `COMPLETE` — used by Milestone 2.

### Milestone 2 — Distributed Task Ownership

Modules: [`task_claims.py`](../src/eudis_swarm/task_claims.py),
[`messaging.py`](../src/eudis_swarm/messaging.py) (`TaskClaimTransport`),
[`task_claim_trace.py`](../src/eudis_swarm/task_claim_trace.py),
[`task_claim_demo.py`](../src/eudis_swarm/task_claim_demo.py).

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
  store reports `COMPLETE`. Later claims and releases may remain recorded for
  audit, but none can change that terminal interpretation.
- **Message-order independence and duplicate tolerance.** Applying a claim is
  idempotent; an obsolete or lower epoch is stored for audit without becoming
  current again. Delivery order does not change the converged result.
- **Receiver-local claim evidence only.** The observer-only claim trace records
  every agent-by-task view without ever emitting `Task.assigned_agent`.
- **Connected-component dissemination.** Claims, releases, and completions can
  traverse intermediate UAVs. Structural IDs and receiver-local seen state make
  retransmission duplicate-tolerant and loop-safe; forwarding never rewrites the
  owner or claim generation.

**Lowest-owner-ID reconciliation is a correctness mechanism, not a
utility-optimal allocator.** It guarantees that replicas with the same valid
evidence select one owner; stable multi-hop connectivity lets that evidence
converge. Utility decides which unclaimed objective a UAV tries first;
reconciliation decides which simultaneous claimant remains authorized.

### Milestone 3 — Receiver-local Task Control

Modules: [`task_utility.py`](../src/eudis_swarm/task_utility.py),
[`simulation.py`](../src/eudis_swarm/simulation.py), and
[`mission.py`](../src/eudis_swarm/mission.py).

- **Immutable task discovery boundary.** Mission setup converts the configured
  task IDs and positions into frozen `TaskObjective` values. Every UAV ranks this
  catalogue without consulting mutable `Task.status` or `Task.assigned_agent`.
  Dynamic sensor discovery is not modeled yet; the current catalogue is
  preloaded mission input.
- **Receiver-local additive utility.** `ReceiverLocalTaskUtility` ranks by
  weighted travel, resource, communication, and role costs, with `(total_cost,
  task_id)` as the deterministic order. The default is distance only. Resource
  and role terms are validated extension points and default to zero.
- **Local connectivity option.** `--allocation-policy connectivity` changes the
  local utility weights and derives communication cost only from that UAV's
  `HEARD` snapshots and their delivered positions. It never queries
  `CommunicationGraph` or another live `Agent`.
- **Claim before bind.** Each responsive idle UAV chooses from its own
  locally-claimable objectives. Choices are batched from pre-mutation views,
  materialized as claims, gossiped, reconciled, and only then bound as execution
  intent when the same local store reports `OWNED_BY_SELF`.
- **One execution predicate.** `Mission.task_is_executable()` requires local
  `owns_task()`. Actual motion, projected motion used by communication-only
  boundaries, renewal, and completion all pass through that predicate. A bare
  `Agent.current_task` cannot move, project, renew, or complete anything;
  `current_task` is only a cache of the currently selected local intent.
- **Stand down and replan.** Release, strict lease expiry, losing deterministic
  reconciliation, or learned completion removes execution authority. The UAV
  clears the cached intent before another physical action and may choose another
  locally available objective in the same deterministic claim round.
- **Failure respects leases.** A fail-stop UAV stops participating and therefore
  stops renewing. Peers retain its claim through the stale-but-valid interval;
  a replacement UAV may claim only after its own received copy crosses strict
  lease expiry.
- **Projection is not authority.** Mutable `Task` fields are maintained for
  mission reporting and visualization. Corrupting that projection cannot create
  a claim, authorize execution, defeat terminal completion evidence, or choose a
  claimant.
- **Centralized policies are baselines.** `TaskAllocator` and
  `CommunicationAwareTaskAllocator` remain available for comparison. The normal
  `Simulation` enables distributed task control and never calls
  `Mission.allocate_tasks()`.

The simulation still runs all replicas in one deterministic process, but that
orchestrator is a physics/test harness: it batches independent local choices and
does not supply global task ownership or graph reachability to those choices.

### Milestone 4 — Formal Receiver-local Autonomy EFSMs

Modules: [`autonomy.py`](../src/eudis_swarm/autonomy.py),
[`trace.py`](../src/eudis_swarm/trace.py), and the generated
[`autonomy_efsm.md`](autonomy_efsm.md) reference.

- **Pure transition relation.** Each transition has finite control state, frozen
  typed extended variables, a typed event, an explicit guard/reason, and ordered
  requested effects. Calling a reducer never moves an aircraft, changes network
  truth, sends a packet, mutates a peer, or executes an effect.
- **Orthogonal machines.** Every UAV composes one `ContactEFSM` and one preserved
  `PeerAvailabilityEFSM` mapping per peer, one `TaskOwnershipEFSM` conformance
  view per task, and one `CoordinationModeEFSM`. There is no global cross-product
  or requirement that different UAVs occupy the same mode.
- **Receiver-local contact inference.** `UNKNOWN`, `ACTIVE`, `DEGRADED`, `LOST`,
  and `RECOVERING` are derived from successful local receipts and elapsed local
  time. Recovery requires observations at distinct local times. A physical link
  restoration, simulator SNR value, component change, or failed delivery is not
  a contact event.
- **Epistemic coordination posture.** Each UAV independently moves among
  `COOPERATIVE`, `DEGRADED`, `LOCAL_AUTONOMY`, and `RECONCILING` from its composed
  local contact states and locally visible task contests. The mode is advisory
  in this milestone; it does not override the existing claim authority.
- **Effects are requests.** Values such as `ENTER_LOCAL_AUTONOMY`,
  `REQUEST_RECONCILIATION`, `STAND_DOWN_TASK`, and `RESUME_COOPERATION` are data
  returned by transitions and recorded for inspection. No dispatcher consumes
  them yet; existing claim orchestration continues its already-established
  reconciliation and stand-down actions independently.
- **Structured observability.** Trace schema 3 records per-peer contact variables,
  per-UAV coordination mode, and ordered state changes with timestamp, local
  sequence, previous state, event, next state, guard, reason, and effects. The
  dashboard displays these observer-only records without feeding them back into
  control.
- **Bounded checking.** Tests enumerate bounded event sequences, replay reducers,
  reach every control state, enforce terminal completion, and inspect the
  autonomy module for forbidden world/observer dependencies. Existing multi-hop,
  split-brain, radio, jammer, and distributed-execution tests remain intact.

The physical `RadioModel` still generates world-level delivery outcomes. The
`ContactEFSM` interprets only the evidence a receiving process obtains from
those outcomes; it never receives distance, SNR, BER, jammer state, or graph
reachability.

### Milestone 5 — Mission Contract, Wasted-work Measurement, and Comparison

Modules: [`mission_contract.py`](../src/eudis_swarm/mission_contract.py),
[`evaluation.py`](../src/eudis_swarm/evaluation.py),
[`sweep.py`](../src/eudis_swarm/sweep.py), and the `MissionPhaseEFSM` in
[`autonomy.py`](../src/eudis_swarm/autonomy.py).

- **Versioned operator intent.** A `MissionContract` carries objective
  priorities, a minimum-participants floor, and a degradation policy of
  `CONTINUE`, `HOLD` or `ABORT`. It is deliberately not a plan: nothing in it
  says who does what, because the operator is not on the network when the
  partition happens. Supersession is strictly by increasing version, so a
  replayed or reordered amendment cannot roll intent backwards.
- **Per-UAV mission posture.** `MissionPhaseEFSM` gives each aircraft a
  `BRIEFED`, `EXECUTING`, `HOLDING`, `ABORTED` or `COMPLETE` posture derived from
  its own coordination mode and the contract version it actually holds. Two
  aircraft either side of a partition may legitimately disagree. A UAV holding
  no contract is unconstrained rather than forbidden: a contract only ever adds
  restraint.
- **Intent propagates as evidence.** `MissionContractTransport` floods
  amendments over the same deterministic store-and-forward gossip that carries
  ownership evidence, so a partition splits intent exactly as it splits
  everything else.
- **Wasted work is measured, not asserted.** `evaluation.py` integrates
  concurrent ownership, abandoned time, dropped ownership and contested time
  over logical time by reading every replica's private store at one instant —
  a comparison no UAV can make. These are labelled evaluator-computed ground
  truth wherever they are displayed.
- **Losses the swarm never notices.** `physically_failed_count` and
  `undetected_failure_count` separate aircraft that actually stopped from
  aircraft the swarm managed to declare. Detection has a connectivity floor:
  see [`measured_results.md`](measured_results.md).
- **Selectable control mode.** `--task-control` chooses replicated distributed
  ownership or the centralized coordinator baseline, which makes the two
  directly comparable rather than merely described.
