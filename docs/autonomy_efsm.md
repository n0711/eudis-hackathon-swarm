# Receiver-local autonomy EFSM kernel

## Implemented boundary

`src/eudis_swarm/autonomy.py` implements a composed extended finite-state
machine (EFSM) kernel for each UAV. It turns evidence available to that UAV
into deterministic local state and declarative requested effects. It does not
read the communication graph, inspect mutable remote agents, execute effects,
or replace the existing peer and task evidence stores.

For a machine configuration `(q, x)` and a typed event `e`, each pure
transition has the shape:

```text
delta((q, x), e, policy) -> (q', x', requested_effects, guard, reason)
```

`q` is the finite control state and `x` is typed extended state. The frozen
`TransitionResult` returned by a transition contains the next values plus an
explanation; it does not mutate the input configuration. `LocalAutonomyKernel`
is the small stateful adapter that applies those results for one observer and
records finite control-state changes.

The composition deliberately avoids one large product automaton:

| Scope | Machine | Cardinality per UAV | Role |
| --- | --- | --- | --- |
| Direct evidence | `ContactEFSM` | One per peer | Infer the health of a direct evidence path from receipts and local time. |
| Peer availability | `PeerAvailabilityEFSM` | One per peer | Mirror the receiver's already-derived `PeerStatus`. |
| Task ownership | `TaskOwnershipEFSM` | One per task | Check and expose the local `TaskClaimStore` ownership view. |
| Coordination posture | `CoordinationModeEFSM` | One | Summarize that UAV's contact and contest evidence. |

Consequently two UAVs may be in different contact, peer-availability, task,
and coordination states at the same simulation time. That disagreement is an
expected consequence of partial information, not an inconsistency to conceal.

## World radio model and receiver contact inference

These are separate layers with different knowledge:

- `RadioModel` is the simulation's world-level physics abstraction. Distance,
  path loss, SNR, bit-error probability, blocking, and sampled delivery decide
  whether a transmission reaches a receiver.
- `ContactEFSM` is a receiver-local inference. Its only event classes are a
  successful direct-hop receipt (`PEER_EVIDENCE_RECEIVED`) and passage of that
  receiver's local time (`LOCAL_TIME_ADVANCED`). It never consumes graph
  connectivity, a `can_link` result, jammer truth, remote `responsive` flags,
  or a synthetic `LINK_RESTORED` event.

For an immutable protocol message, the contact subject is the immediate
forwarder that successfully delivered the hop, not the message's logical
origin. A heartbeat additionally supplies a peer observation to
`PeerAvailabilityEFSM`; an ordinary forwarded protocol receipt does not.
`ContactState.LOST` therefore means "this receiver lacks timely direct
evidence," not "the peer is physically failed," and it never changes an
agent's `responsive` property.

### `ContactEFSM`

The finite states are `UNKNOWN`, `ACTIVE`, `DEGRADED`, `LOST`, and
`RECOVERING`. Its policy defines a positive expected interval, degradation and
loss thresholds, and the number of recovery receipts. The degradation
threshold must cover at least one expected interval, the loss threshold must
be greater than the degradation threshold, and the recovery count is bounded
by `MAX_EFSM_COUNTER` (65,535).

Extended variables retain initialization/event times, the last two distinct
receipt times, bounded successful-receipt and expected-miss counts, and a
bounded recovery count. On local-time advancement, age is measured from the
last receipt, or from initialization before the first receipt. The threshold
guards are strict: degradation occurs when `age > degraded_after` and loss
when `age > lost_after`. A receipt after deterioration enters `RECOVERING`;
distinct-time receipts must reach `recovery_receipts` before returning to
`ACTIVE`. Repeated evidence with the same timestamp cannot manufacture
recovery progress.

## Peer availability and `PeerStatus`

`PeerAvailabilityEFSM` uses the repository's existing `PeerStatus` values as
its finite states:

| `PeerStatus` | Local meaning |
| --- | --- |
| `HEARD` | The receiver has a current first-hand observation. |
| `SILENT` | The receiver's local evidence has crossed its silence policy; it also represents the never-heard initial state. |
| `DECLARED_FAILED` | The receiver has accepted a locally valid quorum-backed failure certificate. |

Its typed events are `OBSERVATION_RECEIVED`, `SILENCE_OBSERVED`, and
`FAILURE_CERTIFICATE_RECEIVED`. Extended variables retain the last event and
observation times, a bounded observation count, and whether a declaration is
active. A fresh first-hand observation can move a declared peer back to
`HEARD`; silence alone does not revoke `DECLARED_FAILED`.

The EFSM does not implement heartbeat aging, witness collection, quorum
validation, or certificate construction. `PeerStateStore` still performs that
work, and `LocalAutonomyKernel.synchronize_peer_status()` maps its already
derived local status into the corresponding typed event. In particular,
`ContactState.LOST` does not directly declare a peer failed. The current
mission-wide failure and retraction projections are also not inputs to this
receiver-local machine.

## Task claim ledger and ownership conformance

`TaskClaimStore` remains the authoritative immutable evidence ledger. It
creates, validates, merges, expires, releases, and reconciles task claims and
completion evidence; its `view()` determines the selected local ownership
view, and its `owns_task()` remains the operational authorization used by the
simulation.

`TaskOwnershipEFSM` is a projection and conformance layer over that view, not a
replacement ledger. `task_ownership_event_from_view()` maps each receiver-local
`TaskOwnershipView` to one of these typed evidence events:

| View evidence | Event | Projected state |
| --- | --- | --- |
| No lease-valid claim | `NO_VALID_CLAIM` | `UNCLAIMED` |
| Selected claim owned by this UAV | `LOCAL_VALID_CLAIM` | `OWNED_BY_SELF` |
| Fresh selected peer claim | `FRESH_PEER_VALID_CLAIM` | `CLAIMED_BY_PEER_FRESH` |
| Stale but lease-valid selected peer claim | `STALE_PEER_VALID_CLAIM` | `CLAIMED_BY_PEER_STALE` |
| Multiple lease-valid owners visible | `CONTEST_VISIBLE` | `CONTESTED` |
| Valid completion evidence | `COMPLETION_EVIDENCE` | `COMPLETE` |

Extended variables retain the last event time, known owner, claim identifier,
claim freshness, contest flag, and completion flag. The adapter rejects a
"local" view whose owner is not self and a "peer" view whose owner is self.
`COMPLETE` is absorbing for all later ownership evidence. A visible contest
requests reconciliation; leaving `OWNED_BY_SELF` requests standing down; and
becoming `UNCLAIMED` from another state requests task selection.

Those requests are observations of a transition. The EFSM does not itself
claim, release, reconcile, complete, or authorize work. The simulation
synchronizes the projection around reconciliation so that a visible
`CONTESTED` state and its eventual resolution can both be traced.

## Per-UAV coordination mode

`CoordinationModeEFSM` has `COOPERATIVE`, `DEGRADED`, `LOCAL_AUTONOMY`, and
`RECONCILING` states. It consumes only `LOCAL_EVIDENCE_UPDATED`, whose payload
contains the observer's contact-state tuple, its number of unresolved task
contests, and a boolean indicating newly received remote evidence.

Its extended variables retain state-entry and evidence times; degradation,
unavailability, and stability timers; counts of known, active, recovering,
degraded/unknown, and lost peers; the current contest count; and a bounded
remote-evidence count. A zero-peer view, or an all-`ACTIVE` peer view, is
healthy. Evidence is severe when peers exist and either none is active or a
strict majority is `LOST`. Policy grace periods make degradation, local
autonomy, and recovery depend on sustained local evidence instead of a single
sample.

A visible task contest takes precedence and requests `RECONCILING`.
`LOCAL_AUTONOMY` begins reconciliation only after new remote evidence arrives;
the one-evaluation flag is then cleared. Stable all-active contact with no
contest permits cooperation to resume, while renewed severe evidence can move
reconciliation back to local autonomy.

The mode belongs to one UAV and is currently advisory. It is exposed to traces
and the dashboard, but it does not yet gate task selection or execution,
change flight behavior, or command relay motion. The pure machine emits
requests such as `REDUCE_REMOTE_DEPENDENCE`, `ENTER_LOCAL_AUTONOMY`,
`REQUEST_RECONCILIATION`, and `RESUME_COOPERATION`; no effect dispatcher yet
turns those requests into control actions.

## Typed events, guards, variables, and effects

All event kinds, finite states, effect kinds, configurations, and extended
variables are explicit enums or frozen dataclasses. Constructors validate
identifiers, timestamps, policy intervals, and bounded counters. Transition
guards are deterministic predicates over the current configuration, the typed
event, and (where applicable) policy. A `TransitionResult` always supplies a
non-empty guard and reason alongside its next configuration.

Effects are declarative `RequestedEffect` values with optional peer or task
identifiers. They describe work an integration layer may perform; transitions
never perform that work. The stateful kernel applies every valid result, but
adds a `TransitionRecord` only when the finite control state actually changes.
Extended variables may therefore change without a new transition record.

## Simulation integration

The simulation constructs one `LocalAutonomyKernel` per UAV, with sorted peer
and task identifiers and policies derived from existing heartbeat/staleness
settings. At the integration boundary:

1. A participating receiver advances its own contact clocks.
2. A delivered heartbeat supplies direct-hop contact evidence and a first-hand
   peer observation.
3. Another delivered immutable protocol message supplies direct-hop contact
   evidence for its immediate forwarder and marks new remote evidence.
4. Receiver-local `PeerStateStore` statuses and `TaskClaimStore` views are
   synchronized into their conformance machines.
5. The per-UAV coordination mode is evaluated from that composed local state.

Only participating, responsive UAV software performs those local steps. This
is a scheduling fact at the simulator boundary, not evidence made available to
another receiver's EFSM. `SimulationResult.autonomy_kernels` exposes the final
local kernels for inspection.

## Trace and replay semantics

`TransitionRecord` is observer-only audit data. It includes timestamp,
observer, machine, previous and next finite state, event kind, guard, reason,
requested-effect kinds, optional peer/task subject, and a monotonically
increasing per-observer `sequence`. Sequence is essential because several
machines can change at the same timestamp. Trace collection preserves unseen
records and orders them by `(timestamp, observer_agent_id, sequence)`.

Trace frames expose each UAV's coordination mode, contact snapshots (including
receipt times and bounded counters), and serialized state-changing
transitions. Repeated capture at the same timestamp merges earlier and newly
observed records rather than discarding either.

The pure transition functions make a complete typed input-event stream plus
initial configuration and policy deterministically replayable. The transition
records alone are intentionally not a lossless event-sourcing log: they omit
self-transitions and do not serialize every machine's complete next extended
state. They support audit, explanation, tests, and visualization; exact
re-execution requires the original typed events (or a future complete event
log).

## Local-knowledge boundary and invariants

The kernel maintains these boundaries:

- no import of the simulator, mission, communication graph, trace subsystem,
  or mutable agent model;
- no guard based on graph components, omniscient reachability, remote
  responsiveness, global assignment, or another UAV's neighbor set;
- no conversion of missing delivery into knowledge of its physical cause;
- no use of global mission failure/retraction projections as local EFSM input;
- deterministic, non-mutating transition functions with bounded counters and
  monotonic event-time validation;
- receiver identity attached to every transition record, with separate peer
  and task subjects where applicable; and
- independent per-receiver configurations, so asymmetric evidence remains
  representable.

## Generated transition tables

The content between the markers below is generated verbatim by
`render_transition_tables()`. It is the canonical summary of transition
families; the pure transition functions remain the executable definition,
including precedence, timing, extended-variable updates, and no-op paths.

<!-- BEGIN GENERATED TRANSITION TABLES -->
### ContactEFSM

Initial state: `UNKNOWN`

| From | Event | Guard | To | Requested effects |
| --- | --- | --- | --- | --- |
| `UNKNOWN` | `PEER_EVIDENCE_RECEIVED` | receipt | `ACTIVE` | `CONTACT_ACTIVE` |
| `ACTIVE` | `LOCAL_TIME_ADVANCED` | age > degraded_after | `DEGRADED` | `CONTACT_DEGRADED` |
| `DEGRADED` | `LOCAL_TIME_ADVANCED` | age > lost_after | `LOST` | `CONTACT_LOST` |
| `LOST` | `PEER_EVIDENCE_RECEIVED` | first recovery receipt | `RECOVERING` | `CONTACT_RECOVERING` |
| `DEGRADED` | `PEER_EVIDENCE_RECEIVED` | first recovery receipt | `RECOVERING` | `CONTACT_RECOVERING` |
| `RECOVERING` | `PEER_EVIDENCE_RECEIVED` | recovery_count >= required | `ACTIVE` | `CONTACT_ACTIVE` |
| `RECOVERING` | `LOCAL_TIME_ADVANCED` | age > degraded_after | `DEGRADED` | `CONTACT_DEGRADED` |
| `*` | `LOCAL_TIME_ADVANCED` | age > lost_after | `LOST` | `CONTACT_LOST` |

### PeerAvailabilityEFSM

Initial state: `SILENT`

| From | Event | Guard | To | Requested effects |
| --- | --- | --- | --- | --- |
| `SILENT` | `OBSERVATION_RECEIVED` | first-hand receipt | `HEARD` | `PEER_HEARD` |
| `HEARD` | `SILENCE_OBSERVED` | silence threshold crossed | `SILENT` | `PEER_SILENT` |
| `*` | `FAILURE_CERTIFICATE_RECEIVED` | valid quorum certificate | `DECLARED_FAILED` | `PEER_DECLARED_UNAVAILABLE` |
| `DECLARED_FAILED` | `OBSERVATION_RECEIVED` | new first-hand receipt | `HEARD` | `PEER_HEARD` |

### TaskOwnershipEFSM

Initial state: `UNCLAIMED`

| From | Event | Guard | To | Requested effects |
| --- | --- | --- | --- | --- |
| `NONCOMPLETE` | `NO_VALID_CLAIM` | no lease-valid claim | `UNCLAIMED` | `REQUEST_TASK_SELECTION` |
| `NONCOMPLETE` | `LOCAL_VALID_CLAIM` | selected owner is self | `OWNED_BY_SELF` | — |
| `NONCOMPLETE` | `FRESH_PEER_VALID_CLAIM` | fresh selected peer claim | `CLAIMED_BY_PEER_FRESH` | — |
| `NONCOMPLETE` | `STALE_PEER_VALID_CLAIM` | stale but lease-valid peer claim | `CLAIMED_BY_PEER_STALE` | — |
| `NONCOMPLETE` | `CONTEST_VISIBLE` | multiple lease-valid owners | `CONTESTED` | `REQUEST_RECONCILIATION` |
| `NONCOMPLETE` | `COMPLETION_EVIDENCE` | valid completion evidence | `COMPLETE` | — |
| `COMPLETE` | `*` | completion already known | `COMPLETE` | — |

### CoordinationModeEFSM

Initial state: `COOPERATIVE`

| From | Event | Guard | To | Requested effects |
| --- | --- | --- | --- | --- |
| `COOPERATIVE` | `LOCAL_EVIDENCE_UPDATED` | sustained contact deterioration | `DEGRADED` | `REDUCE_REMOTE_DEPENDENCE` |
| `DEGRADED` | `LOCAL_EVIDENCE_UPDATED` | sustained insufficient peer evidence | `LOCAL_AUTONOMY` | `ENTER_LOCAL_AUTONOMY` |
| `LOCAL_AUTONOMY` | `LOCAL_EVIDENCE_UPDATED` | new remote evidence | `RECONCILING` | `REQUEST_RECONCILIATION` |
| `*` | `LOCAL_EVIDENCE_UPDATED` | visible task contest | `RECONCILING` | `REQUEST_RECONCILIATION` |
| `RECONCILING` | `LOCAL_EVIDENCE_UPDATED` | stable contact and no contests | `COOPERATIVE` | `RESUME_COOPERATION` |
| `RECONCILING` | `LOCAL_EVIDENCE_UPDATED` | contact deteriorates again | `LOCAL_AUTONOMY` | `ENTER_LOCAL_AUTONOMY` |
| `DEGRADED` | `LOCAL_EVIDENCE_UPDATED` | stable active contact | `COOPERATIVE` | `RESUME_COOPERATION` |
<!-- END GENERATED TRANSITION TABLES -->

## Current limitations

- Requested effects have no dispatcher and do not yet change mission or flight
  behavior.
- Coordination mode is advisory rather than an execution gate.
- Contact evidence establishes receipt, not the physical cause of absence or
  recovery.
- Peer availability relies on the existing store for silence and certificate
  semantics; the EFSM does not independently verify proofs.
- Task ownership mirrors a local ledger view and cannot replace claim-store
  authorization or convergence.
- The `new_remote_evidence` input is a coarse flag for one coordination
  evaluation, not a typed inventory of everything learned.
- State-change traces are not complete event logs, and trace snapshots do not
  include every peer, task, and coordination extended variable.
- No Mission Contract or Mission EFSM exists yet, and the autonomy kernel does
  not implement coordinated search, relay optimization, quantum optimization,
  hardware-in-the-loop, or SITL behavior.

## Exact next milestone: Milestone 5 — Mission Contract + Mission EFSM

The next milestone is to add an authorized, versioned Mission Contract that
expresses operator intent, operating area, constraints, priorities, and
degradation policy rather than per-aircraft commands. Above distributed task
execution, a Mission EFSM should formalize phases such as `INITIALIZE`,
`SEARCH`, `OBSERVE`, `RECOVER`, `RETURN`, and `COMPLETE`, and consume existing
requested effects where that is semantically appropriate.

That milestone must preserve the receiver-local evidence boundary and the
existing claim ledger's authority. Coordinated search, relay behavior, QAOA,
and SITL are later milestones, not implied by the kernel implemented here.
