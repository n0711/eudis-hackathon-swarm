# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are prototype
milestones rather than semantic API guarantees — this is a research simulator
and interfaces change between prototypes.

## [Unreleased]

### Added

- A composed, pure EFSM autonomy kernel with explicit typed events, bounded
  extended variables, guards, deterministic transition results, and requested
  effects for receiver-local contact, peer availability, task ownership, and
  per-UAV coordination mode. The machines remain independent rather than
  constructing one cross-product swarm state.
- One `LocalAutonomyKernel` per UAV. Contact changes are driven only by
  successfully received heartbeats/protocol hops and receiver-local time;
  forwarded evidence is attributed to the immediate forwarding UAV rather than
  its immutable logical origin. Graph transitions, radio equations, mutable
  remote agents, and observer state are not machine inputs.
- Canonical machine definitions with generated Markdown transition tables,
  exhaustive bounded event-sequence tests, deterministic replay checks, local
  knowledge dependency audits, and scenario tests for contact loss/recovery,
  state divergence, transient task contests, and reconciliation.
- Playback trace schema version 3, recording each UAV's contact and coordination
  state plus ordered transition records containing timestamp, local sequence,
  machine, prior state, event, next state, guard, reason, and requested effects.
- Dashboard panels for simultaneous per-UAV coordination modes, per-peer contact
  state/age/misses/recovery, and a structured autonomy-transition timeline.
- `docs/autonomy_efsm.md` and `docs/prior_art.md`, separating implemented
  machinery from research comparators and planned mission/search/role work.
- `RadioModel`: a frozen free-space line-of-sight link model implementing the
  path-loss, SNR and BPSK bit-error-rate relations of Hu, Ren & Cheng
  (arXiv:2407.11531, eqs. 5-10), with `can_link()` for the hard
  `BER <= P_e0` rule and `link_quality()` for a per-frame delivery probability.
- `SimulationConfig.link_model` (`range` default, or `radio`),
  `SimulationConfig.stochastic_delivery`, and `SimulationConfig.radio_model`,
  with matching `--link-model` and `--stochastic-delivery` CLI flags. Under
  `radio` the `CommunicationGraph` ignores `communication_range` and derives
  links from the model; with stochastic delivery each link is sampled from its
  quality using a seeded stream, so one seed reproduces byte-identical traces.
- Explicit "all rights reserved" copyright notice and a "not flight software"
  statement in the README. The repository carries no open-source licence and is
  published for review only.
- `SECURITY.md`, `CONTRIBUTING.md` and this changelog, documenting the working
  agreement and the architectural boundaries that must be preserved.
- `--version` flag on the `eudis-swarm` CLI.
- Packaging metadata: project URLs, classifiers and keywords.
- Issue forms, a pull-request template, and badges.
- `PeerStatus` with the receiver-local `HEARD`, `SILENT` and quorum-backed
  `DECLARED_FAILED` meanings, while retaining `PeerKnowledgeState` for snapshot
  freshness.
- Graph-mediated `FailureVote` and `FailureDeclaration` exchange with isolated
  receiver mailboxes, retryable votes and certificates, receiver-local
  timeout-bounded vote evidence, and a strict-majority quorum with a two-voter
  minimum.
- Canonical undirected `blocked_links` and `CommunicationGraph.can_deliver()`
  for deterministic single-link, balanced-partition, and reconnection cases.
- The exact six-state `TaskOwnershipState` vocabulary and a classifier that
  reads self state, local completion evidence, and receiver-local delivered
  observations only.
- A newcomer-oriented distributed-state architecture guide covering world
  truth, agent belief, observer access, scheduling, and deferred work.
- Immutable task claims, exact-generation releases, and terminal completion
  evidence with no authoritative `Agent` or `Task` references.
- One `TaskClaimStore` per UAV with distinct freshness and lease boundaries,
  owner-scoped renewal epochs, explicit `CONTESTED` reconciliation, replay
  tombstones, voluntary release, and monotonic `COMPLETE`.
- Frozen `TaskObjective` catalogue entries and a pure
  `ReceiverLocalTaskUtility` that ranks weighted distance, resource,
  communication, and role costs without accepting `Mission`, mutable `Task`,
  topology, or remote live-agent inputs.
- The authoritative distributed task-control round: batch receiver-local claim
  intents, create claims before binding work, gossip and reconcile evidence,
  activate only local owners, stand down losers, and replan to a fixed point.
- Deterministic store-and-forward flooding for `TaskClaim`,
  `TaskClaimRelease`, `TaskCompletionEvidence`, `FailureVote`, and
  `FailureDeclaration`. Structural message IDs, per-receiver duplicate
  suppression, stable traversal order, persistent retry of unsuccessful routes,
  and finite seen state make propagation loop-safe without a TTL.
- Transport receipts that distinguish logical origin, forwarding UAV,
  receiver, and hop count, plus delivery/forwarding/duplicate-suppression
  counters for observer-only diagnostics.
- Classified protocol counters for logical forwarding attempts, successful and
  useful first deliveries, unavailable-link attempts, duplicate source
  publications, duplicate-route suppressions, and unique inactive-endpoint
  deferrals.
- `TaskClaimTransport`, which propagates ownership evidence across connected
  multi-hop `CommunicationGraph` components while preserving immutable claim
  ownership and generation.
- A deterministic `{1,2}` / `{3,4}` split-brain demonstration in
  `eudis_swarm.task_claim_demo`, including reconnect, unanimous reconciliation,
  losing release, and continued work.
- A standalone JSON claim trace containing every agent-by-task local view,
  known claim age/freshness/version, contested state, winner, releases, and
  completion without authoritative ownership fields.

### Changed

- Heartbeat creation no longer records authoritative source state directly in a
  centralized failure cache. Heartbeats remain one-hop observations; failure
  votes and declarations must pass through modeled store-and-forward delivery
  before becoming remote evidence.
- `Mission` now applies failure recovery only after receiving a validated
  quorum-backed declaration; silence and link loss remain non-authoritative.
- Failure detection no longer consults link ground truth. `PeerStatus.UNREACHABLE`,
  `observe_link_state()` and `PeerStateTransport.synchronize_link_evidence()` are
  removed: a receiver knows only what it heard and when, so a healthy but
  partitioned UAV can now be wrongly declared dead.
- A declaration is belief, not a kill switch. It sets `status` only; heartbeats
  and failure injection depend on `responsive`, while operational motion also
  requires the cached intent to match a locally owned claim.
- Declarations are reversible: first-hand contact retracts one locally, and a
  quorum of retractions withdraws it in the world.
- Normal `Simulation` task control no longer calls the centralized allocator.
  Each responsive idle UAV ranks its immutable objective catalogue from local
  inputs, creates a claim, and binds work only after its own store reports
  `OWNED_BY_SELF`; the legacy allocators remain comparison baselines.
- `Agent.current_task` is now an execution-intent cache rather than ownership
  authority. Actual and projected motion, renewal, and completion all require
  `TaskClaimStore.owns_task()`; loss, release, expiry, or completion stands the
  UAV down before it replans.
- Mutable `Task.status` and `Task.assigned_agent` are maintained as observer
  projections only and are not read to select, claim, or authorize local work.
- A fail-stop owner no longer triggers immediate claim reassignment. It stops
  renewing, and another receiver may claim the task only after its locally
  received lease has strictly expired.
- Claim renewal is paced at the local freshness threshold instead of every
  simulation tick.
- The playback trace (now schema version 3) carries world truth, per-agent
  belief, every replica's ownership view, composed autonomy state, and ordered
  EFSM transitions in one frame, with disputed tasks flagged.
- Non-participating UAV software no longer advances or receives updates to its
  private freshness state.
- The connectivity task-control option derives its communication utility only
  from that receiver's complete `HEARD` snapshots, so raw-fresh or live remote
  state cannot influence intent after silence, link loss, or declaration.
- Communication graph updates may combine the existing whole-agent block with
  explicit link-level blocks without changing the existing API defaults.
- `HeartbeatTimeout` remains available as an import-compatible alias for the
  now-explicit `FailureDeclaration` semantics.
- The distributed-state guide now specifies the task-ownership EFSM, strict
  stale-versus-expired boundary, owner-local epoch meaning, deterministic
  cross-owner rule, and absorbing completion semantics.
- Protocol control decisions now distinguish source-clock metadata
  (`created_at`, `detected_at`, and heartbeat emission timestamps) from
  receiver-local `received_at`. Silence, vote retention, freshness, and lease
  expiry use receiver-local receipt age; duplicate forwardable evidence does not
  refresh it, while each delivered heartbeat remains new first-hand contact.
- Connected topology now means protocol evidence can converge without direct
  all-to-all links. Physical link availability remains world truth and is never
  copied into `PeerStateStore`.

### Fixed

- Protocol telemetry no longer counts repeated evaluations toward inactive
  endpoints as link attempts. In the audited pre-fix default run, the entire
  `15,075 - 474 = 14,601` attempt/success gap targeted failed, inactive UAV 2
  even though the graph stayed a clique; instrumentation classified 97.1% of
  those evaluations as repeats. Deduplicating the same outstanding work exposed
  141 unique `(message, inactive receiver)` obligations. The transport now
  defers those obligations and excludes them from the link-attempt denominator.
- In the legacy non-distributed path, a rejoining UAV no longer keeps a pointer
  to work reassigned while it was believed dead. The normal distributed path
  now resolves that situation from claim evidence rather than an observer task
  pointer.
- Declaration certificates are retransmitted, so a certificate built from
  evidence older than a snapshot the receiver has since accepted is now
  rejected. Previously such a replay could silently re-declare a UAV the swarm
  had already heard from, and a retraction witness could persist across a new
  declaration and withdraw it on the next tick.
- An orphaned task may return to its original owner once that owner's
  declaration has been retracted; the metrics guard now applies only to UAVs
  still believed failed.
- Documented recovery for a stale editable install after the repository is
  moved, which previously surfaced as `ModuleNotFoundError: No module named
  'eudis_swarm'` across the whole test suite.

## [0.3.0a0] — Prototype 0.3A

### Added

- `CommunicationAwareTaskAllocator`: an optional greedy policy that scores
  candidate UAV/task pairs using only the deciding UAV's own `FRESH` peer
  snapshots, minimising
  `(predicted_isolation, -predicted_peer_degree, distance, agent_id, task_id)`.
- `--allocation-policy {distance,connectivity}` CLI option, defaulting to the
  preserved `distance` baseline.
- `ALLOCATION (PROTOTYPE 0.3A)` metrics block reporting policy, connectivity-aware
  assignment count, isolation predictions, and mean/minimum predicted degree.
- Trace-driven playback dashboard (`eudis-swarm-dashboard`) with mission and
  topology views, peer status, allocation explanations and an event timeline.
- `--record-trace PATH` for versioned JSON playback capture.

### Changed

- Allocators are now defined by the minimal `AllocationPolicy` protocol.
  `Mission` remains the sole authority that applies proposals.

## [0.2.0b0] — Prototype 0.2B

### Added

- `PeerStateTransport`: instantaneous one-hop delivery of immutable state
  snapshots across active direct links only.
- `PeerStateStore`: receiver-local peer observations with strict
  `UNKNOWN` / `FRESH` / `STALE` freshness transitions.
- `--peer-state-stale-after` CLI option and the `PEER STATE` metrics block.

### Changed

- Peer knowledge is explicitly separated from physical ground truth and from
  graph reachability. `STALE`, `UNKNOWN` and `UNREACHABLE` never imply `FAILED`.

## [0.2.0a1] — Prototype 0.2A.1

### Added

- Configuration validation rejecting booleans in integer fields, non-finite
  values, invalid ranges and illogical fault windows.
- Mission lifecycle states with operations legal only while `RUNNING`.
- Monotonic logical-time validation and invariant testing.
- Ruff, Pyright, coverage gating and GitHub Actions CI on Python 3.11 and 3.13.

## [0.2.0a0] — Prototype 0.2A

### Added

- `CommunicationGraph`: a fixed-vertex, time-varying undirected graph derived
  from UAV positions with an inclusive Euclidean range threshold.
- Link, isolation, partition and reconnection transition reporting, emitted once
  per state change rather than per tick.
- Explicit per-UAV communication fault injection
  (`--comm-fault-agent`, `--comm-fault-start`, `--comm-fault-end`).
- `NETWORK` metrics block.

### Changed

- Communication state is observational only in this prototype: it does not
  affect heartbeat delivery, failure detection, allocation or path planning.

## [0.1.0] — Prototype 0.1

### Added

- Point-mass UAV agents, constant-speed 2D movement and periodic heartbeats.
- Nearest-distance `TaskAllocator` baseline.
- `FailureManager` with strict `now - last_heartbeat > timeout` detection.
- Autonomous fail-stop recovery: detect, release, reallocate, complete —
  reproducing the `4.00 -> 5.75 -> 10.75 -> 17.25 s` sequence with zero human
  interventions.

[Unreleased]: https://github.com/n0711/eudis-swarm/compare/main...HEAD
