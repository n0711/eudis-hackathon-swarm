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

TBD — written once Phase 1's commit lands.
