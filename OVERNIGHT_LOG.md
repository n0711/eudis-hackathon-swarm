# Overnight session log

Session start: 2026-09-19, branch `claude/inventory`, HEAD `439280c` at start.
Operator instructions: maximum *safe, verifiable* progress; never push; never
touch a remote; commit locally only, prefixed `overnight:`; never weaken an
existing test; do not modify the coordination core beyond what a task strictly
requires.

This file is updated as I go, not only at the end, so the state can be
reconstructed if the session stops mid-task.

## Phase 1 — ground truth (no behavior changes)

- Repo identity: package name `eudis-swarm`, working-tree directory and git
  remote are named `eudis-hackathon-swarm` (`https://github.com/n0711/eudis-hackathon-swarm.git`).
  `pyproject.toml`'s `[project.urls]` still point at `github.com/n0711/eudis-swarm`
  (the old name) — a stale-URL hygiene item, not fixed (see baseline doc).
- Branch `claude/inventory`, HEAD `439280c557b4b358514e2b01ca1b50e9219d7417`.
  `origin/main`, local `main`, and `claude/inventory` are all the same commit —
  nothing local is unpushed.
- EFSM work near commit `a9a7206` (`chore: remove automated dependency update
  metadata`) is an ancestor of HEAD, 30 commits behind. The actual EFSM commits
  (`b7245fd feat: add receiver-local EFSM autonomy kernel`,
  `bb88c88 feat: add versioned operator intent and the mission phase machine`)
  are already merged into `main` and already pushed. **Nothing unpushed here.**
- `.venv` ships Python 3.10.12; `pyproject.toml` requires `>=3.11`. No Python
  3.11+ interpreter exists on this machine (checked `python3.11/3.12/3.13`,
  `pyenv`, `uv`, `conda`, `asdf`; `apt-cache policy python3.11` only offers an
  old `3.11.0~rc1` from the distro's universe repo, not installed). Did **not**
  install a new interpreter or touch system packages — flagged as a decision
  for the operator in `PRE_HACKATHON_BASELINE.md` instead.
  - `pip install -e ".[dev,dashboard]"` cannot succeed on this interpreter:
    pip enforces `requires-python` and refuses; also, without that flag, an
    old pip (22.0.2) shipped in the venv pathologically backtracked for 12+
    minutes across `pytest`/`ruff`/`pytest-cov` versions before I killed it.
  - Upgraded pip in `.venv` only (22.0.2 → 26.2.1) — this is a tooling upgrade
    inside the project's own venv, not a system change.
  - Installed `.[dev]` with `--ignore-requires-python`: succeeded in ~15s once
    pip was modern.
  - Installed the `dashboard` extra's `streamlit` requirement: **failed**.
    Streamlit's transitive dependency chain pulls in a `numpy` build that
    itself requires Python `>=3.12` (fails at the `meson-python` metadata
    stage, not a wheel availability issue — it does not build from source on
    3.10 either). Installed `plotly` alone instead (`plotly>=5.24,<8` per
    pyproject, resolved to `7.1.0`) — this succeeded and unblocks every test
    gated only on plotly.
- Quality gate results (exact commands, Python 3.10.12, `.venv`):
  - `ruff check .` — **All checks passed!**
  - `ruff format --check .` — **84 files already formatted.**
  - `pyright` — **1 error**: `dashboard_app.py:14` `Import "streamlit" could
    not be resolved`. This is the direct, sole consequence of streamlit not
    being installable on this interpreter (see above) — not a code defect.
    CI runs pyright on 3.11/3.13 with the full `dashboard` extra and would not
    see this.
  - `pytest --cov=eudis_swarm --cov-report=term-missing` — **2 collection
    errors** before any test runs, both purely version-driven:
    - `tests/test_packaging.py` — `import tomllib` (that module is Python
      3.11+ stdlib; does not exist on 3.10).
    - `tests/test_dashboard_comparison.py` — imports `eudis_swarm.dashboard_app`
      unconditionally at module scope (guarded only by
      `pytest.importorskip("plotly")`, not by a streamlit guard), which then
      hard-imports `streamlit` and fails to import it.
  - Re-ran `pytest --cov=eudis_swarm --cov-report=term-missing
    --ignore=tests/test_dashboard_comparison.py --ignore=tests/test_packaging.py`
    to get a real signal on everything the interpreter can actually run:
    **482 passed, 90.46% branch coverage** (floor 85%).
  - `python -m build` succeeded (sdist + wheel). Installed the wheel and ran
    `eudis-swarm --help` / `--version` — both succeeded. Re-installed editable
    mode afterward to resume development.
  - Spot-checked documented deterministic claims directly against `README.md`:
    `--scenario demo-quorum-floor` (3/3, `t=10.75s`, exit 0),
    `--scenario baseline-quorum-floor` (2/3, `t=40.00s`, exit 1),
    default `python -m eudis_swarm.simulation` (20/20, `t=17.25s`,
    `174/132/42` messages), and `task_claim_demo` (exact summary line). All
    matched exactly.
- **I did not change, skip, xfail, or loosen any test to make this pass.**
  Both excluded files are excluded from the *command*, not modified on disk,
  and both exclusions are logged here with cause. `PRE_HACKATHON_BASELINE.md`
  lists this as a decision for the operator (get a 3.11+ interpreter onto this
  machine so CI parity is possible locally) rather than something I fixed.

## Files changed this session

| File | Why |
| --- | --- |
| `OVERNIGHT_LOG.md` (new) | this log |
| `CLAUDE.md` (new) | `/init`-generated architecture/commands reference for future Claude Code sessions |
| `PRE_HACKATHON_BASELINE.md` (new) | Phase 1 frozen ground-truth report: commit, gate results, capability map, hygiene decisions |
| `.venv/` (tooling only, not committed — `.gitignore`d) | upgraded `pip` 22.0.2 → 26.2.1; installed `.[dev]` and standalone `plotly` with `--ignore-requires-python` (see baseline doc for why) |

No file under `src/` or `tests/` was modified in Phase 1.

## Coordination core — read-only unless a task explicitly requires a change

Per the operator's invariants (receiver-local truth; no global/privileged
simulation truth may feed an agent's decisions; ownership/lease/gossip/
reconciliation semantics and the distributed protocol must not change), the
following are treated as **read-only** for the rest of this session unless a
Phase 2 task explicitly requires touching one — in which case I will explain
why *before* editing it, here:

- `src/eudis_swarm/peer_state.py` — receiver-local peer/freshness state
- `src/eudis_swarm/failure_manager.py` — quorum failure detection/declaration
- `src/eudis_swarm/task_claims.py` — claim/lease/epoch/reconciliation ledger
- `src/eudis_swarm/task_utility.py` — receiver-local claim-intent utility
- `src/eudis_swarm/autonomy.py` — the composed receiver-local EFSM kernel
- `src/eudis_swarm/task.py` — six-state ownership vocabulary / observer projection
- `src/eudis_swarm/mission.py` — claim-authorized execution, invariants
- `src/eudis_swarm/messaging.py` — one-hop heartbeats + multi-hop gossip transport
- `src/eudis_swarm/communication.py` — world-truth topology / `can_deliver()`
- `src/eudis_swarm/mission_contract.py` — operator intent / mission phase machine
- `src/eudis_swarm/task_claim_trace.py`, `src/eudis_swarm/trace.py` — observer-only
  protocol/ownership trace schemas
- `src/eudis_swarm/simulation_events.py` — structured event types crossing the boundary
- `src/eudis_swarm/simulation.py` — orchestration/scheduler that ties the above together
- `src/eudis_swarm/agent.py` — world-truth position/motion (`advance_point_mass`)

**Planned, explicitly-justified exception for Phase 2B only** (not yet started):
introducing a `VehicleBackend` seam requires wrapping `agent.py`'s
`advance_point_mass` and `simulation.py`'s call sites behind an interface, while
leaving the existing point-mass path and all existing tests passing. This is
additive (new `PointMassBackend` conforming to a new `VehicleBackend` protocol)
and does not change ownership, lease, gossip, reconciliation, or any decision
an agent makes — only *how* world-truth position updates are dispatched. I will
not touch `mission.py`, `task_claims.py`, `peer_state.py`, `failure_manager.py`,
`autonomy.py`, `messaging.py`, or `communication.py` for this. Full stop if it
turns out to need more than that.

## Phase 2 plan (filled in before any Phase 2 edit; see also decisions above)

Phase 1 committed as `aa88504`. Starting Phase 2A: additive, new modules only,
nothing in the coordination-core list above is touched. Everything reuses
`Simulation`/`SimulationConfig`/`SimulationMetrics` read-only, the same way
`sweep.py` already does — no new entry point into agent decision logic, so
this cannot affect receiver-local truth.

**2A.1 — `src/eudis_swarm/lease_timeout_study.py` (new).**
Runs the existing deterministic mission across a range of
`claim_lease_duration` values over a fixed set of seeds (reusing
`sweep.parameter_values`, a pure helper, read-only import) and records, per
`(lease_timeout, seed)`: `concurrent_ownership_duration` (this is the
project's existing name for double-booked/duplicate work, task-seconds),
`abandoned_task_time`, `mission_duration`, `mission_completed`, and the mean of
`metrics.task_reassignment_latencies.values()` when non-empty (this property
already exists on `SimulationMetrics` but `sweep.py`'s CSV never surfaces it —
that omission is why this needs its own runner rather than reusing
`sweep.run_sweep` directly). Aggregates across seeds per lease value into:
mean duplicate-work rate (defined here as `concurrent_ownership_duration /
mission_duration`, a new, explicitly-documented derived quantity — the raw
task-second counters in this codebase are absolute, not rates, so I am
introducing and naming this ratio rather than assuming prior art defines it),
mean abandoned-task time, mean mission duration, fraction of seeds completing
the mission, and mean reassignment latency with an explicit sample count (0
samples reported honestly, not silently coerced to 0.0). Deterministic CSV
writer mirroring `sweep.py`'s fixed-precision, sorted-order style.

**2A.2 — `src/eudis_swarm/lease_timeout_chart.py` (new).**
Reads a `lease_timeout_study` aggregate CSV and renders one self-contained,
fully offline HTML chart (`fig.write_html(path, include_plotlyjs=True)` — no
CDN, no network at render or view time) plotting duplicate-work rate and
abandoned-task time on dual y-axes against lease timeout on the x-axis.
Guarded with `importlib.util.find_spec("plotly") is None` exactly like
`dashboard.py` already does, so this is optional and does not add a hard
dependency. `plotly` is already a project optional extra; nothing new is
added to `pyproject.toml`'s dependency graph for this piece.

**2A.3 — `src/eudis_swarm/control_mode_comparison.py` (new).**
Same-seed comparison runner: for each seed in a fixed set, runs `Simulation`
twice with identical config except `task_control` (`distributed` vs
`centralized`), and emits one row per seed with both modes' values for the
wasted-work counters (`concurrent_ownership_duration`, `abandoned_task_time`,
`dropped_ownership_time`, `contested_duration`, `mission_completed`,
`mission_duration`, `tasks_completed`, `declared_failed`,
`undetected_failures`) side by side, plus a `delta` (centralized − distributed).
**Correction after checking `dashboard_app.py` directly:** only two of these
nine (`concurrent_ownership_duration` / "Double-booked" and
`dropped_ownership_time` / "Stranded work") are literally what the
belief-comparison dashboard view renders (`_wasted_work_counters`); the rest
are the other evaluator counters `sweep.py`'s own CSV already tracks. Fixed
the module's docstring to say this precisely instead of overclaiming "the
metrics the dashboard uses" for all nine
per metric. Deterministic CSV writer, same style as above.

**2A.4 — Tests.** `tests/test_lease_timeout_study.py`,
`tests/test_lease_timeout_chart.py` (`pytest.importorskip("plotly")`, same
guard `test_dashboard_comparison.py` already uses for the parts that don't
need streamlit), `tests/test_control_mode_comparison.py`. Every existing test
stays untouched and green; new tests follow this repo's stated convention of
exact-value assertions over tolerances where the simulation is deterministic.

**2A.5 — packaging, only after 2A.1-2A.4 are green.** Consider adding
`eudis-swarm-lease-study` / `eudis-swarm-mode-compare` console-script entries
to `pyproject.toml`, mirroring the three that already exist. If added, rerun
the full build + wheel-install smoke test before committing, so a packaging
mistake cannot leave the branch broken.

Explicitly out of scope for 2A: re-deriving or restating any number in
`docs/measured_results.md`, touching `sweep.py` itself, or adding any new
runtime dependency.

## 2A build log

- Wrote `src/eudis_swarm/lease_timeout_study.py`, `lease_timeout_chart.py`,
  `control_mode_comparison.py` plus their three test files, exactly as
  planned above. Only new files; nothing under the coordination-core list was
  touched.
- **Found, not fixed: a pre-existing crash in the coordination core.**
  `Mission._assert_distributed_consistent()` (`mission.py:774`) raises
  `RuntimeError: a responsive UAV has an execution intent without local
  ownership` when `claim_lease_duration` is set below roughly `0.3` (crashes
  at `0.1` and `0.2`, fine from `0.3` up), with the existing `sweep.py`, not
  just my new module — reproduced directly:
  `list(sweep.run_sweep("lease-timeout", (0.1,), (1,), config))` crashes on
  unmodified `main` at commit `aa88504`. This is **below** every lease-timeout
  range `docs/measured_results.md` actually publishes (`0.5` to `6.0`), so no
  published number is affected, and I did not touch `mission.py` to
  investigate further — that module is on the coordination-core list, and
  CONTRIBUTING.md's Scope section requires discussion first for any change to
  distributed autonomy/ownership. Flagging this as a genuine robustness gap
  for the operator to decide whether it's worth a follow-up bug report/fix.
  My own new test suite avoids the pathological range (uses the documented
  `>= 0.5` floor) so this does not block Phase 2A.
- All three new modules: `ruff check`, `ruff format --check`, and `pyright`
  clean. 30 new tests, all green. Two narrow `# type: ignore[...]` comments
  in `test_lease_timeout_chart.py` for plotly's untyped `Figure.data` (no
  `py.typed` marker in the installed `plotly` package) — same pattern
  `test_dashboard_comparison.py` already uses elsewhere in this repo for
  third-party/dynamic typing gaps.
- Manually spot-checked (outside the test suite) that the lease-timeout study
  reproduces the *direction* `docs/measured_results.md` section 2 reports
  (shorter lease -> more duplicate work under a partition) using the same
  jammer/communication-range/task-count as that doc's own sweep, at a smaller
  seed count for speed. This is corroborating, not a re-derivation of the
  published numbers.
- Full suite + coverage re-run, same exclusions as Phase 1
  (`--ignore=tests/test_dashboard_comparison.py --ignore=tests/test_packaging.py`,
  both still excluded only for the pre-existing Python-3.10-vs-3.11 reason
  logged in Phase 1, unrelated to this work): **515 passed, 0 failed, 0
  errors, 90.75% branch coverage** (floor 85%). `ruff check .` and
  `ruff format --check .` both clean across the whole repo. `pyright`: same
  single pre-existing `dashboard_app.py` streamlit-import error as Phase 1,
  nothing new. Committing Phase 2A now as `overnight: <msg>`.
- **2A.5 (packaging) — deliberately skipped.** Adding console-script entries
  to `pyproject.toml` is package-metadata surface the operator may want to
  name/curate themselves (existing scripts are `eudis-swarm`,
  `eudis-swarm-dashboard`, `eudis-swarm-sweep` — short, curated names, not
  literal module names), and it is not required for either new module to be
  usable (both already run fine as `python -m
  eudis_swarm.lease_timeout_study` / `python -m
  eudis_swarm.control_mode_comparison`, verified below). Left as a suggestion
  for the operator rather than done unilaterally.
- Ran all three new CLIs end-to-end as a smoke test (in the scratchpad
  directory, output not committed, all default-config unless noted so this
  is not a re-derivation of `docs/measured_results.md`'s own settings):
  - `python -m eudis_swarm.lease_timeout_study --from 0.5 --to 3.0 --steps 6
    --seeds 5` — exit 0, well-formed 6-row aggregate CSV, header matches
    `AGGREGATE_COLUMNS`. At this module's own defaults (`--agents 4
    --tasks 20 --communication-range 130`, i.e. no partition), duplicate-work
    rate is 0.0 at every lease value and mission always completes — expected,
    since full connectivity means no concurrent ownership is possible
    regardless of lease length; this smoke test only confirms the plumbing
    runs and writes a well-formed CSV, not any resilience claim.
  - `python -m eudis_swarm.lease_timeout_chart <that CSV> --output
    lts.html` — exit 0; output starts with a valid `<!doctype html>` and
    inlines `plotly.js` (`v4.1.1` banner present in the file), so it opens
    with no network access.
  - `python -m eudis_swarm.control_mode_comparison --seeds 5` (`--seeds` is
    a *count*, not a list — `--seed-base` shifts the starting seed; my first
    draft of this log entry wrongly wrote a comma-separated list, caught and
    fixed here before committing) — exit 0, 5-row CSV, `delta_*` columns
    equal `centralized - distributed` on every row, confirming the arithmetic
    described in the plan above.
