# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are prototype
milestones rather than semantic API guarantees — this is a research simulator
and interfaces change between prototypes.

## [Unreleased]

### Added

- Explicit "all rights reserved" copyright notice and a "not flight software"
  statement in the README. The repository carries no open-source licence and is
  published for review only.
- `SECURITY.md`, `CONTRIBUTING.md` and this changelog, documenting the working
  agreement and the architectural boundaries that must be preserved.
- `--version` flag on the `eudis-swarm` CLI.
- Packaging metadata: project URLs, classifiers and keywords.

### Fixed

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
