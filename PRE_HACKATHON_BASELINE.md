# Pre-hackathon baseline

Frozen ground-truth snapshot, taken before any behavior-changing work in this
session. Nothing in this document is aspirational — every claim below was
either read directly from the repository or reproduced by running the exact
command shown.

- **Commit:** `439280c557b4b358514e2b01ca1b50e9219d7417`
- **Branch:** `claude/inventory` (identical to local `main` and `origin/main`
  — nothing on this branch is unpushed)
- **Repo identity:** package name is `eudis-swarm`; this working directory and
  its git remote are named `eudis-hackathon-swarm`
  (`https://github.com/n0711/eudis-hackathon-swarm.git`). See Hygiene below.
- **EFSM work near `a9a7206`:** that commit (`chore: remove automated
  dependency update metadata`) is an ancestor of HEAD, 30 commits behind. The
  actual EFSM work (`b7245fd feat: add receiver-local EFSM autonomy kernel`,
  `bb88c88 feat: add versioned operator intent and the mission phase machine`)
  is already merged into `main` and already on `origin/main`. **No unpushed
  EFSM work exists in this clone.**

## Verification gate — exact results

Run on this machine's `.venv` (Python 3.10.12 — see Hygiene; the project
declares `requires-python = ">=3.11"` and CI runs 3.11/3.13, so this is not a
like-for-like substitute for CI, only the closest available local signal).

| Gate | Command | Result |
| --- | --- | --- |
| Lint | `python -m ruff check .` | **All checks passed** |
| Format | `python -m ruff format --check .` | **84 files already formatted** |
| Types | `python -m pyright` | **1 error**: `dashboard_app.py:14`, `Import "streamlit" could not be resolved` — caused solely by `streamlit` not being installable on Python 3.10 here (see Hygiene), not a code defect |
| Tests + coverage | `python -m pytest --cov=eudis_swarm --cov-report=term-missing --ignore=tests/test_dashboard_comparison.py --ignore=tests/test_packaging.py` | **482 passed**, 0 failed, 0 skipped-that-shouldn't-be; **90.46% branch coverage** (floor 85%) |
| Build | `python -m build` | sdist + wheel built cleanly |
| CLI smoke | `pip install --force-reinstall dist/*.whl && eudis-swarm --help && eudis-swarm --version` | both succeeded, printed `eudis-swarm 0.3.0a0` |

Two test modules could not be collected on this interpreter and were excluded
from the count above **by command-line `--ignore`, not by editing either
file**:

- `tests/test_packaging.py` — `import tomllib` (Python 3.11+ stdlib; absent on
  3.10).
- `tests/test_dashboard_comparison.py` — imports `eudis_swarm.dashboard_app`
  unconditionally at module scope (guarded only by
  `pytest.importorskip("plotly")`, which does not guard the module's own
  unconditional `import streamlit`), and `streamlit` could not be installed on
  this interpreter (see Hygiene).

No test was skipped, weakened, xfailed, or modified to reach the 482/90.46%
result above.

## Reproduced documented claims (spot-checked, byte/number exact)

All of the following matched `README.md` exactly on this commit, on this
machine:

- `eudis-swarm --scenario demo-quorum-floor` → 3/3 tasks, mission completed at
  `t=10.75s`, exit code 0.
- `eudis-swarm --scenario baseline-quorum-floor` → 2/3 tasks, mission timed out
  at `t=40.00s`, exit code 1 (documented as the one preset that must fail).
- `python -m eudis_swarm.simulation` (default config) → 20/20 tasks, mission
  completed at `t=17.25s`, peer messages `174 / 132 / 42`
  attempted/delivered/undelivered.
- `python -m eudis_swarm.task_claim_demo` → `Task-claim demo: 2+2 partition,
  Task 19 -> UAV 1, UAV 4 released and claimed Task 29; 10 trace frames.`

The sweep-derived tables in `docs/measured_results.md` (480/440/420/432-run
sweeps) were **not** re-run in full this session — they take substantially
longer than the smoke checks above and Phase 1 is a ground-truth pass, not a
re-validation of every published table. Nothing above contradicts them.

## Capability map

Everything in this table is implemented **and** covered by the test suite
already; "Milestone" references `docs/milestones.md`.

| Capability | Modules | Status |
| --- | --- | --- |
| Graph-mediated quorum failure detection, reversible declarations | `communication.py`, `failure_manager.py`, `peer_state.py` | Implemented, tested (Milestone 1) |
| Receiver-local peer state, freshness (`FRESH`/`STALE`/`UNKNOWN`) vs. status (`HEARD`/`SILENT`/`DECLARED_FAILED`) | `peer_state.py` | Implemented, tested (Milestone 1) |
| Six-state distributed task ownership (`TaskClaimStore`, leases, epochs, reconciliation) | `task_claims.py`, `task.py`, `messaging.py` (`TaskClaimTransport`) | Implemented, tested (Milestone 2) |
| Deterministic 2+2 split-brain/reconcile/continue demo | `task_claim_demo.py`, `task_claim_trace.py` | Implemented, tested (Milestone 2); reproduced above |
| Receiver-local additive task utility, claim-before-bind execution | `task_utility.py`, `mission.py`, `simulation.py` | Implemented, tested (Milestone 3) |
| Centralized comparison baselines (`TaskAllocator`, connectivity-aware) | `task_allocator.py` | Implemented, tested; explicitly never used by the normal distributed run loop |
| Composed receiver-local EFSMs (`ContactEFSM`, `PeerAvailabilityEFSM`, `TaskOwnershipEFSM`, `CoordinationModeEFSM`) with pure transitions and observer traces | `autonomy.py`, `trace.py`, `docs/autonomy_efsm.md` (generated) | Implemented, tested (Milestone 4) |
| Versioned operator `MissionContract` + degradation policy + per-UAV `MissionPhaseEFSM` | `mission_contract.py`, `autonomy.py` | Implemented, tested (Milestone 5) |
| Wasted-work measurement (concurrent ownership, abandoned/dropped/contested time) as evaluator-only ground truth | `evaluation.py`, `metrics.py` | Implemented, tested (Milestone 5) |
| Parameter sweep CLI (`eudis-swarm-sweep`), deterministic CSV | `sweep.py` | Implemented, tested; 100% coverage |
| Free-space line-of-sight radio model + seeded stochastic delivery | `communication.py` (`RadioModel`), tests | Implemented, tested; explicitly documented as an analytical abstraction, not calibrated RF |
| Region jammer | `communication.py`/config `RegionJammer` | Implemented, tested |
| Trace-driven Streamlit/Plotly playback dashboard, incl. side-by-side belief comparison view | `dashboard.py`, `dashboard_app.py` | Implemented; **not fully testable on this machine** — `dashboard_app.py` hard-imports `streamlit`, which cannot be installed on the local Python 3.10 `.venv` (see Hygiene). Architecture test asserting the dashboard never imports world-truth modules is part of the excluded-by-ignore test file. |
| SITL: one ArduCopter vehicle flown from the simulator's local frame via `pymavlink`, plus a measured-model-error script | `sitl.py`, `geodesy.py`, `scripts/sitl_model_error.py` | Implemented per `docs/sitl.md` ("Phase 2/3 — done"); **not independently re-verified this session** (needs a live SITL instance, out of scope for Phase 1) |
| `VehicleBackend` seam (`PointMassBackend`/`MavlinkBackend` behind a shared interface) | — | **`docs/sitl.md` claims "Phase 1 — the seam ... (done)", but no `VehicleBackend`, `PointMassBackend`, or `MavlinkBackend` symbol exists anywhere in `src/` or `tests/` (verified by grep), and no commit ever introduced one.** This is a documentation/implementation mismatch, not shipped work — flagged for the operator, and it is the reason Phase 2B (if reached) treats this as genuinely new work rather than a duplicate. |
| Vehicle kinematics/energy model (turn rate, endurance, calibration) | — | Not implemented; `docs/vehicle_model.md` describes it as a fully open, not-yet-started lane |

## Hygiene items — decisions for the operator, not fixed this session

1. **`.venv` is Python 3.10.12; the project requires `>=3.11`.** No 3.11+
   interpreter exists on this machine (checked `python3.11/.12/.13`, `pyenv`,
   `uv`, `conda`, `asdf`; `apt-cache policy python3.11` only offers an
   unpacked `3.11.0~rc1` from the distro repo). I did not install a new
   interpreter or touch system packages. Every result in this document was
   produced with `pip install -e ".[dev]" --ignore-requires-python`, which is
   not what CI does and is not a substitute for it. **Decision needed:**
   either provision Python 3.11+ on this machine, or accept that local
   verification here is best-effort until CI runs.
2. **The `dashboard` extra cannot install on this `.venv`.** `pip install -e
   ".[dashboard]"` fails while building `numpy` (pulled in transitively by
   `streamlit`/`pandas`): the resolved `numpy` version's build backend
   (`meson-python`) requires Python `>=3.12` and refuses to build on 3.10 —
   this is not a missing-wheel problem, it fails at metadata generation even
   from source. Only `plotly` (pinned range `>=5.24,<8`, resolved to `7.1.0`)
   was installed standalone to unblock plotly-only-gated tests.
   `tests/test_dashboard_comparison.py` still cannot collect because
   `dashboard_app.py` hard-imports `streamlit` unconditionally, with no
   `importorskip` guard for it (only one for `plotly`).
3. **A pre-upgrade `pip` (22.0.2, whatever this `.venv` shipped with)
   pathologically backtracks** on `pip install -e ".[dev,dashboard]"` —
   12+ minutes and counting, re-downloading many versions of `pytest`,
   `pytest-cov`, and `ruff` with no forward progress, before it was killed.
   Upgrading `pip` in-venv to 26.2.1 first made the same install (`.[dev]`
   alone) take ~15 seconds. Worth fixing in onboarding docs / CI cache
   assumptions if anyone else hits a fresh, unwarmed venv.
4. **`pyproject.toml`'s `[project.urls]`** (`Homepage`, `Repository`,
   `Issues`, `Changelog`) all point at `github.com/n0711/eudis-swarm`, which
   does not match this repo's actual name/remote
   (`github.com/n0711/eudis-hackathon-swarm`).
5. **Repo-name / package-name mismatch** noted above (`eudis-swarm` package,
   `eudis-hackathon-swarm` repo). Not fixed — may be intentional if the repo
   was renamed after the PyPI-style package name was chosen.
6. **Branch protection state on `origin`** was not checked. The hard rule for
   this session forbids `gh` and any remote/network operation against the
   repo, so this could not be inspected without violating that rule. Decision
   for the operator to check directly on GitHub.
7. **Uncommitted changes at session start:** none — working tree was clean
   before this session touched anything.
8. **No `LICENSE` file** — per `README.md`'s own "License / Status" section
   this is stated as deliberate ("all rights reserved... published for
   review"), not an oversight, so not flagged as an issue, just noted for
   completeness.

## What Phase 1 deliberately did not do

- Did not re-run the full multi-hundred-run sweeps behind `docs/measured_results.md`.
- Did not attempt to fix the Python-version mismatch, the dashboard-extra
  install failure, or the two uncollectable test modules — all are reported
  above as decisions, per the operator's explicit instruction to report
  hygiene issues rather than fix them.
- Changed no behavior anywhere in `src/` or `tests/`.
