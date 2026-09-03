# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are prototype
milestones rather than semantic API guarantees — this is a research simulator
and interfaces change between prototypes.

## [Unreleased]

### Added

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
- `PeerStatus` with the receiver-local `HEARD`, `SILENT` and quorum-backed
  `DECLARED_FAILED` meanings, while retaining `PeerKnowledgeState` for snapshot
  freshness.
- Graph-mediated `FailureVote` and `FailureDeclaration` exchange with isolated
  receiver mailboxes, retryable votes and certificates, timeout-bounded vote
  evidence, and a strict-majority quorum with a two-voter minimum.
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
- `TaskClaimTransport`, which routes ownership evidence only across active
  one-hop `CommunicationGraph` links and records per-receiver delivery results.
- A deterministic `{1,2}` / `{3,4}` split-brain demonstration in
  `eudis_swarm.task_claim_demo`, including reconnect, unanimous reconciliation,
  losing release, and continued work.
- A standalone JSON claim trace containing every agent-by-task local view,
  known claim age/freshness/version, contested state, winner, releases, and
  completion without authoritative ownership fields.

### Changed

- Heartbeat creation no longer records authoritative source state directly in a
  centralized failure cache. Heartbeats, failure votes, and declarations must
  pass through the modeled one-hop transport before becoming remote evidence.
- `Mission` now applies failure recovery only after receiving a validated
  quorum-backed declaration; silence and link loss remain non-authoritative.
- Failure detection no longer consults link ground truth. `PeerStatus.UNREACHABLE`,
  `observe_link_state()` and `PeerStateTransport.synchronize_link_evidence()` are
  removed: a receiver knows only what it heard and when, so a healthy but
  partitioned UAV can now be wrongly declared dead.
- A declaration is belief, not a kill switch. It sets `status` only; motion,
  heartbeats and failure injection depend on `responsive` alone.
- Declarations are reversible: first-hand contact retracts one locally, and a
  quorum of retractions withdraws it in the world.
- `Mission` is no longer the single writer of ownership. Allocation is refused
  for any task whose lease is still valid in the assigning UAV's claim store,
  and a UAV that loses reconciliation yields the task.
- The playback trace (schema version 2) carries world truth, per-agent belief
  and every replica's ownership view in one frame, with disputed tasks flagged.
- Non-participating UAV software no longer advances or receives updates to its
  private freshness and link-evidence state.
- Connectivity scoring now consumes only complete `HEARD` status, so raw-fresh
  snapshots do not influence decisions after silence, link loss, or declaration.
- Communication graph updates may combine the existing whole-agent block with
  explicit link-level blocks without changing the existing API defaults.
- `HeartbeatTimeout` remains available as an import-compatible alias for the
  now-explicit `FailureDeclaration` semantics.
- The distributed-state guide now specifies the task-ownership EFSM, strict
  stale-versus-expired boundary, owner-local epoch meaning, deterministic
  cross-owner rule, and absorbing completion semantics.

### Fixed

- A rejoining UAV no longer keeps a pointer to work reassigned while it was
  believed dead. `Mission.retract_declaration` surrenders the stale task and
  releases the matching claim, so `assert_consistent()` cannot raise
  `agent/task ownership links do not match` when a wrongly declared UAV fails
  to reach its task before communications return.
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
