# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A deterministic, dependency-free Python simulator for a communications-aware
autonomous UAV swarm (package `eudis_swarm`, repo directory/remote named
`eudis-hackathon-swarm` — a naming mismatch, see Hygiene below). It models what
happens when radio contact splits a swarm into disconnected groups: each UAV
acts only on its own local knowledge, and conflicting task ownership is
reconciled autonomously once connectivity returns. There is no wall-clock time,
no unseeded randomness, and no order-dependent iteration anywhere in the
simulator — the same command line always produces the same numbers.

`README.md` is the front page (measured results, architecture diagram,
reproduction commands). `CONTRIBUTING.md` has the full boundary/invariant
rationale. `docs/onboarding.md` is the fastest path in for a new contributor;
`docs/README.md` indexes the rest of `docs/`.

## Commands

Environment (`.venv` requires **Python 3.11+**; see Hygiene below if your
interpreter is older):

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,dashboard]"
```

Full local gate (same as CI, which runs it on Python 3.11 and 3.13):

```bash
python -m ruff check .
python -m ruff format --check .
python -m pyright
python -m pytest --cov=eudis_swarm --cov-report=term-missing   # gated at 85% branch coverage
python -m build
```

Single test / single file:

```bash
python -m pytest tests/test_task_claims.py
python -m pytest tests/test_task_claims.py::test_name_of_the_test -q
```

Run the simulator directly:

```bash
python -m eudis_swarm.simulation                      # default: 4 UAVs, 20 tasks, 20/20 in 17.25s
python -m eudis_swarm.simulation --help
eudis-swarm --scenario demo-quorum-floor               # named presets, see README "Named demonstration scenarios"
python -m eudis_swarm.task_claim_demo                  # fixed 2+2 partition/reconcile/continue demo
eudis-swarm-sweep --param lease-timeout --from 0.5 --to 6.0 --steps 12 --seeds 20 --output sweep.csv
eudis-swarm-dashboard trace.json                       # optional Streamlit/Plotly playback (needs the `dashboard` extra)
```

`eudis-swarm`, `eudis-swarm-dashboard`, and `eudis-swarm-sweep` are the
installed console-script equivalents of the `python -m` invocations above.
Named scenario presets (`--scenario ...`) override the matching flags and
leave every other default untouched — `baseline-quorum-floor` is the one
preset that deliberately fails and exits non-zero (asserted by test).

## Architecture

Three layers, kept strictly separate — this is the load-bearing design
decision and almost every bug class in this codebase is a violation of one of
these boundaries:

| Layer | Holds | Must never |
| --- | --- | --- |
| **World / physics** (`agent.py`, `communication.py`, `mission.py`'s `Task` records, `simulation.py`) | real `Agent` positions/responsiveness, the live `CommunicationGraph`, mutable observer-facing `Task` projections | authorize ownership from `Task.assigned_agent`, or hand a remote UAV's live state to local decision logic |
| **Receiver-local control** (`autonomy.py`, `peer_state.py`, `task_claims.py`, `task_utility.py`) | own pose, the immutable `TaskObjective` catalogue, one `PeerStateStore`, one `TaskClaimStore`, a local execution-intent cache | infer failure from silence alone, copy graph/projected-owner truth in, or act without `owns_task()` |
| **Observer / evaluation** (`metrics.py`, `trace.py`, `task_claim_trace.py`, the dashboard) | metrics, traces, test assertions comparing belief against truth | feed its privileged view back into allocation or failure decisions |

Per-agent composition (one of each per UAV):

```
Agent
 +-- immutable TaskObjective catalogue
 +-- ReceiverLocalTaskUtility        (task.py's utility scorer; additive, distance by default)
 +-- PeerStateStore                  (peer_state.py: delivered heartbeats, freshness, declarations)
 +-- TaskClaimStore                  (task_claims.py: immutable claims, leases, epochs, reconciliation)
 +-- LocalAutonomyKernel             (autonomy.py: composed pure EFSMs)
 |     +-- ContactEFSM               (one per peer)
 |     +-- PeerAvailabilityEFSM      (one per peer)
 |     +-- TaskOwnershipEFSM         (one per task; conformance projection of TaskClaimStore, not a second source of truth)
 |     +-- CoordinationModeEFSM      (one per UAV)
 +-- current_task                    (local intent cache; authorizes nothing by itself)
```

Normal task control follows exactly one path — never call `Mission.allocate_tasks()`
from the normal run loop, that is the centralized baseline only:

```
immutable TaskObjective catalogue -> receiver-local additive utility
  -> create claim before binding work -> gossip and reconcile
  -> bind/execute only while owns_task()
```

Key invariants (each enforced by a test that will fail the build if broken —
read `CONTRIBUTING.md`'s boundary table before touching any of this):

- **Silence is not failure.** `SILENT`/`STALE`/`UNKNOWN` must never imply
  `FAILED`. A `FailureDeclaration` requires a quorum
  (`max(2, floor((N-1)/2)+1)`) of votes carried over modelled links — never a
  missed heartbeat alone — and it never touches `responsive`, movement, or
  transmission. It is reversible: first-hand contact from a "declared dead"
  peer retracts the declaration locally.
- **No topology leakage.** `CommunicationGraph` may use authoritative
  positions (it's the world-level network model), but no topology fact may be
  copied into a `PeerStateStore`. A receiver knows only what it actually
  received, and when.
- **Source clock vs. receiver clock.** `created_at`/`detected_at`/`Heartbeat.timestamp`
  are immutable source metadata. Silence, freshness, vote retention, and lease
  expiry all use receiver-local age (`local_now - received_at`), never a
  cross-agent clock comparison.
- **Claim lease semantics**: for claim age `a`, `FRESH` while `a <=
  freshness_timeout`, `STALE` while `freshness_timeout < a <=
  lease_timeout`, `EXPIRED` once `a > lease_timeout`. `STALE != UNCLAIMED` — a
  replica may not create a competing claim until its own copy is `EXPIRED`.
  Epochs are scoped to one `(task_id, owner_agent_id)` stream and are never
  compared across owners; reconciliation supersedes older publications per
  owner, then picks the lowest validated owner ID.
- **Heartbeats vs. protocol evidence.** Heartbeats are one-hop only and never
  forwarded. `FailureVote`, `FailureDeclaration`, `TaskClaim`,
  `TaskClaimRelease`, and `TaskCompletionEvidence` are immutable and flood
  across the current connected component via deterministic store-and-forward
  (stable message/neighbour ordering, per-receiver duplicate suppression, no
  TTL, no random backoff).
- Two control modes exist for comparison: `task_control="distributed"`
  (replicated claim ownership; the operational model) and `"centralized"`
  (single-coordinator baseline via `AllocationPolicy` in `task_allocator.py`).
  Never route a centralized proposal back into the normal execution path.

`Mission.assert_consistent()` is called at every scheduler checkpoint and
enforces the selected ownership model — preserve that call after adding any
new compound mutation path.

## Determinism requirements for any change

- No wall-clock time, no unseeded randomness (the one RNG seeds task
  placement only), no dependence on unordered-set/dict iteration reaching an
  output.
- A change that moves a published figure (scenario time, ownership
  transition, message count) is not automatically wrong, but it must be
  deliberate, explained in the PR, and the relevant docs/tests updated in the
  same commit — see `docs/measured_results.md` and `README.md`'s "two
  commands" result.
- Prefer exact assertions (`assert duration == 17.25`) over tolerances — this
  is a deterministic simulator, so exact pins are legal and more useful.
- New distributed-state tests should include a negative assertion (the
  forbidden shortcut cannot occur), covering chain/cyclic topologies, not just
  a full clique.

## Coverage config

`pyproject.toml` deliberately omits `dashboard.py`, `dashboard_app.py`, and
`sitl.py` from coverage — the first two need a live Streamlit session to
exercise, and `sitl.py` needs a live autopilot; everything in `sitl.py`
testable without one is covered by `tests/test_sitl.py` with a mocked
`pymavlink` connection.

## SITL / vehicle work

`sitl.py` and `geodesy.py` are optional, gated behind the `sitl` extra
(`pymavlink`), imported only inside that module so a missing dependency
disables one backend rather than breaking the package. `docs/sitl.md` and
`docs/vehicle_model.md` describe planned/in-progress work here — check the
actual module contents before trusting either doc's status claims, since they
can describe a design that was written up but not (yet) merged.

## Hygiene notes for whoever is developing here

- The checked-in `.venv` may not match `requires-python = ">=3.11"` on this
  machine — check `.venv/bin/python --version` before assuming `pip install
  -e ".[dev]"` will succeed; an old `pip` in a stale venv can also backtrack
  for a very long time resolving `streamlit`'s dependency tree. Upgrade `pip`
  first (`python -m pip install --upgrade pip`) if a resolve seems stuck.
- `pyproject.toml`'s `[project.urls]` still point at `github.com/n0711/eudis-swarm`,
  not this repo's actual name/remote (`eudis-hackathon-swarm`).
