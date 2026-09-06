# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are prototype
milestones rather than semantic API guarantees — this is a research simulator
and interfaces change between prototypes.

## [Unreleased]

### Added
- **The motion model measured against real firmware.**
  `scripts/sitl_model_error.py` flies a leg repeatedly under ArduCopter SITL and
  compares each sample against what `advance_point_mass` said would be there.
  The vehicle takes 1.71x longer than the model predicts, and peak divergence --
  34 m on a 60 m leg -- lands exactly when the model says the aircraft has
  arrived and stopped. Reported as a range rather than a mean because SITL is
  not reproducible, and kept out of `measured_results.md` for the same reason.
  The consequence is not cosmetic: the connectivity-aware allocator chooses
  assignments from that projection, so it evaluates link graphs for positions no
  aircraft will occupy.
- `SitlVehicle.vehicle_time_s`, the autopilot's own clock. SITL is routinely run
  faster than real time, and a wall-clock stopwatch reports a 60 m leg taking
  one second under `--speedup 10`. The first version of the experiment measured
  exactly that and produced nonsense.
- **Software in the loop, flying.** `eudis_swarm.sitl.SitlVehicle` commands a
  real ArduPilot instance in the simulator's own local metres. An ArduCopter
  SITL vehicle armed, climbed to 20 m and flew a three-waypoint circuit,
  settling 2-3 m from each commanded point. `pymavlink` arrives through a new
  `sitl` optional extra, imported on demand, and a test asserts in a fresh
  interpreter that importing the simulator pulls in neither the backend nor
  pymavlink -- so a missing extra disables one backend rather than the package.
- `eudis_swarm.geodesy`, the local east/north frame that conversion crosses:
  pure, dependency-free, and tested without a vehicle anywhere near it. It
  round-trips exactly over the working area and reports its own projection
  error rather than leaving the reader to assume it.
- `scripts/sitl-copter.sh`, starting one headless ArduCopter instance, and the
  two things that actually cost time when doing this: SITL streams no telemetry
  until a ground station requests it, and the WSL address changes on restart.
- `SitlVehicle.deterministic` is `False`, readable off the class, so anything
  producing published numbers can refuse a non-reproducible backend without
  first connecting to one.
- A 440-run population result in `docs/measured_results.md`: eleven
  communication ranges, twenty seeds, both control modes, with a jammer. Central
  control failed 68 of 220 missions and distributed failed none, while the two
  modes detected losses identically at every range. It is the four-aircraft
  population version of the two-aircraft demonstration, and it shows the same
  thing: identical awareness, opposite outcomes.
- A qualification on the detection cliff. The original sweep had no jammer; with
  one the same detector degrades as a smooth gradient rather than a cliff. Both
  shapes are real, and the cliff belongs to the scenario rather than to the
  detector.
- `docs/sitl.md`, preparing a software-in-the-loop path against real autopilot
  firmware. It fixes the rules first: a SITL run may never produce a measured
  result, MAVLink bindings live behind an optional extra, and telemetry enters
  as world truth through the vehicle layer rather than into any UAV's beliefs.
- `advance_point_mass`, now the single definition of how a vehicle moves.
  Movement and prediction previously carried the same arithmetic in two places.
  They are the same question only while the motion model *is* the vehicle; under
  a real autopilot they separate, and the divergence between them becomes
  measurable rather than impossible.
- `tests/test_vehicle_model.py`, pinning that prediction and movement agree
  today, so that when SITL ends that equivalence it ends visibly.
- `docs/threat_context.md`, citing the operating environment the project is
  aimed at rather than asserting it: EASA currently lists `LCCC NICOSIA` among
  the FIRs affected by GNSS jamming and spoofing, and Osechas et al.
  (NAVIGATION 69(2), doi:10.33012/navi.516) measured a chirp jammer in that FIR
  leaving a receiver without positioning for 80% of the time aloft. The
  document states as prominently what those sources do *not* support: GNSS
  denial is not comms jamming, and none of it calibrates this simulator.
- `docs/vehicle_model.md`, specifying the vehicle and energy work as a
  self-contained lane, with the constraints it must not break and the questions
  left deliberately open. Energy depletion is specified to present as ordinary
  silence, so peers cannot distinguish an exhausted aircraft from a jammed one.
- `scripts/pitch-demo.ps1`, running both quorum-floor scenarios back to back
  without requiring an activated environment. Tests assert it names only
  scenarios that exist and greps only for labels the summary actually emits,
  because a filter that silently matches nothing looks like success.
- A regression pinning the comparison view when one run ends long before the
  other: the finished side must hold its final frame rather than disappear,
  which is what puts a completed panel beside a still-failing one.
- `demo-quorum-floor` and `baseline-quorum-floor` presets, reducing the
  project's sharpest result to two commands. Two aircraft, one of which really
  stops: neither control mode can declare the loss, distributed ownership
  completes 3/3 at 10.75 s, and central control holds a task assigned to a
  stopped aircraft and never finishes. The presets differ by exactly one
  setting, asserted by test.
- The README now leads with that result rather than with an architecture
  summary.
- `MissionContractTransport`, flooding amendments of operator intent across
  active links with the same deterministic store-and-forward gossip used for
  ownership evidence. A partition therefore does to intent exactly what it
  does to everything else: one half learns the amendment and the other does
  not, until the network heals.
- `SimulationConfig.contract_amendment`, issued mid-mission to one aircraft.
  The `demo-amendment` preset briefs UAV 1 with v2 at t=6.00 s while the swarm
  is split: UAV 1 holds v2 and the other three hold v1 for eight frames, then
  all four converge on v2 once the partition heals, completing 20/20.
- The dashboard belief panel names each UAV's contracted posture and the
  intent version behind it, so divergent operator intent is visible on screen.
- `SimulationConfig.mission_contract` briefs every UAV before launch, and the
  contracted posture gates whether a UAV seeks new work. A UAV holding no
  contract is unconstrained rather than forbidden, so existing scenarios are
  unchanged. Under the `demo-partition` scenario the operator's degradation
  policy becomes a dial on the measured trade-off: `CONTINUE` completes 20/20
  in 11.75 s with 1.50 task-seconds of double-booking, while `HOLD` completes
  20/20 with none at all and takes 19.00 s.
- A mission ends as soon as every responsive UAV has been contracted to stand
  down, rather than running to the simulation timeout. Under `ABORT` the
  partition run now ends at 2.25 s instead of 300 s, which reports an operator
  decision as such instead of disguising it as a simulation limit.
- Playback trace schema version 5, recording each UAV's mission phase and the
  contract version it holds.
- `MissionContract`: versioned, immutable operator intent carrying objective
  priorities, a minimum-participants floor, and a degradation policy
  (`CONTINUE`, `HOLD`, `ABORT`). Intent never rolls backwards, so a replayed
  or reordered delivery cannot resurrect an older version. It is deliberately
  not a plan: nothing in it says who does what, because the operator is not on
  the network when the partition happens.
- `MissionPhaseEFSM`, a fifth pure autonomy machine giving each UAV a mission
  posture (`BRIEFED`, `EXECUTING`, `HOLDING`, `ABORTED`, `COMPLETE`) derived
  from its own coordination evidence and the contract version it actually
  holds. The phase is per-UAV: two aircraft either side of a partition may
  legitimately disagree, and an abort is absorbing until the operator issues a
  superseding contract.
- An allowlist test on the knowledge boundary's transitive closure, replacing
  reliance on a blocklist that cannot anticipate a newly added module.
- A side-by-side belief comparison in the dashboard, replaying two runs of one
  scenario aligned by logical time. Each side shows vehicle positions and
  links, every UAV's evidence about every peer, its ownership belief per task,
  and the evaluator-computed wasted-work counters. Peers are labelled by
  evidence and its age (`HEARD 0.4s`, `SILENT 8.7s`,
  `DECLARED FAILED (quorum)`), never by conclusion, and the view states that
  it is a post-mission replay reconstructed from each UAV's own log.
- `SimulationConfig.task_control` and `--task-control`, selecting replicated
  distributed ownership or the centralized coordinator baseline. `Mission`
  already contained the complete centralized path behind
  `distributed_task_control`; nothing could reach it from `Simulation`, so no
  side-by-side comparison was possible.
- A `baseline` scenario preset: the `demo-partition` scenario under central
  control, for the left-hand panel of the comparison view.
- `--scenario` presets so each demonstration is one command: `demo-failstop`
  (range 130, UAV 2 dies at t=4, quorum detects it) and `demo-partition`
  (range 50, a region jammer that splits the swarm into two components of two
  UAVs each from t=3.75 to t=7.25 and heals before the mission ends). The
  jammer geometry was searched for rather than guessed. A preset overrides the
  matching command-line options and leaves the defaults untouched.
- Playback trace schema version 4, carrying the running
  `concurrent_ownership_duration` and `dropped_ownership_time` per frame so a
  viewer can show wasted work accumulating, labelled as evaluator-computed
  ground truth.
- An architecture test that fails if the dashboard modules import `Mission`,
  `Simulation`, `Agent` or any other module carrying authoritative world
  state, enforcing structurally that belief panels are reconstructed only from
  recorded per-agent trace fields.
- Evaluator-side wasted-work measurement in `eudis_swarm.evaluation`, reading
  every replica's private claim store at one instant -- a comparison no UAV can
  make. `concurrent_ownership_duration` integrates the number of tasks two or
  more UAVs simultaneously believe they own; `abandoned_task_time` integrates
  unowned incomplete work; `dropped_ownership_time` is the discriminating
  subset counting only tasks somebody owned and then let go, since abandoned
  time is otherwise dominated by queue depth; `contested_duration` integrates
  visible contests. Totals, per-task breakdowns and worst-offender lines appear
  in the mission summary, labelled as evaluator-computed ground truth.
- `eudis-swarm-sweep`, a deterministic parameter sweep emitting one CSV row per
  (parameter value, seed). The same seeds reproduce byte-identical output.
- `physically_failed_count` and `undetected_failure_count` metrics, separating
  aircraft that actually stopped from aircraft the swarm managed to declare. The
  mission summary now reports both plus "Losses never detected".
- Regression tests pinning the connectivity floor of failure detection. On the
  default scenario a genuinely dead UAV is declared at `communication_range`
  78.0 and above, and never declared at 75.0 and below, where its coordinator
  status stays `ACTIVE` for the whole mission while all twenty tasks still
  complete.

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
- `Agent.move_toward` carried a comment saying a UAV believed dead "keeps
  flying" directly above the guard that stops an unresponsive one from moving.
  The guard keys on physical truth and is correct; the comment now says so
  without reading as its own opposite.
- **Trace schema 5 to 6.** `TraceMetadata` now records `task_control`, so a
  replay knows which control model produced it. Traces recorded under schema 5
  are rejected with the usual unsupported-version error and must be re-recorded.
- The comparison view titles each panel from the control mode in that panel's
  own trace instead of from fixed per-slot labels. Loading the two files in the
  wrong order previously showed a distributed run under the heading
  "Centralised coordinator"; the labels now follow the evidence, an unfamiliar
  mode is reported verbatim rather than guessed at, and the sidebar no longer
  implies an order. This is the same rule the belief panels already followed.
- `duplicated_task_completion_count` is renamed `redundant_completion_count`
  and its summary line "Duplicated completions" becomes "Redundant
  completions". It counts completions of already-complete work: a real but
  rare event, and not the same thing as wasted flight time.

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
