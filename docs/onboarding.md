# Onboarding

Read this first. It should take about an hour to get from nothing to changing
code with confidence.

## What this is, in three sentences

A deterministic simulator for a UAV swarm that loses radio contact. Each
aircraft decides what to do from **only what it has actually received** — never
from world truth — and the simulator measures what that costs when the network
splits. The central result is that distributed task ownership keeps completing
missions in conditions where a central coordinator cannot, *without* either one
being any better informed.

If you read nothing else, read [the result](../README.md#the-result-in-two-commands).

## Get it running

On Windows, in the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,charts]"
```

On Linux or macOS, substitute `.venv/bin/python`. Either way, **call the
interpreter inside `.venv` explicitly** rather than relying on an activated
shell. Nearly every "command not found" in this project has been an unactivated
environment.

Then see the whole point of the project in two commands:

```powershell
.\.venv\Scripts\eudis-swarm.exe --scenario demo-quorum-floor
.\.venv\Scripts\eudis-swarm.exe --scenario baseline-quorum-floor
```

The first completes 3/3 tasks. The second completes 2/3 and never finishes.
Both print the *same* three evidence lines — neither swarm knows an aircraft is
gone. That difference, with identical awareness, is the project.

On Windows, `.\scripts\pitch-demo.ps1` runs both back to back.

### If something fails

| Symptom | Cause |
| --- | --- |
| `ModuleNotFoundError: eudis_swarm` in tests | Stale editable install, usually after moving the repo. Re-run the `pip install -e` line. |
| `eudis-swarm: command not found` | The environment is not activated. Call `.\.venv\Scripts\eudis-swarm.exe` directly. |
| `baseline-quorum-floor` exits 1 | That is correct. It is the one preset that deliberately fails. |

## Read the code in this order

Twenty-six modules is a lot. These six, in order, are enough to understand
everything else. Every module has a docstring saying what it is for; those are
accurate and worth trusting.

| Order | Module | Lines | Why this one |
| ---: | --- | ---: | --- |
| 1 | [`config.py`](../src/eudis_swarm/config.py) | ~240 | Every knob in the simulation, in one place. The docstring groups them; read it as a table of contents. |
| 2 | [`agent.py`](../src/eudis_swarm/agent.py) | ~210 | Authoritative world truth: where an aircraft *actually* is. Small, and the boundary everything else is defined against. |
| 3 | [`peer_state.py`](../src/eudis_swarm/peer_state.py) | ~330 | What one UAV has actually received about its peers. This is the idea of the project made concrete. |
| 4 | [`task_claims.py`](../src/eudis_swarm/task_claims.py) | ~1050 | Ownership without a coordinator: leases, epochs, contested reconciliation. The docstring has the lifecycle diagram. |
| 5 | [`autonomy.py`](../src/eudis_swarm/autonomy.py) | ~1900 | The pure decision kernel. Big, but it is five small state machines side by side, each with the same five-part shape (see the docstring). |
| 6 | [`simulation.py`](../src/eudis_swarm/simulation.py) | ~1650 | The loop that drives all of the above. Read last: it makes sense only once the pieces do. The docstring lists the per-tick pipeline. |

Everything else is support: `messaging.py` delivers evidence, `metrics.py` and
`evaluation.py` measure, `trace.py` records replayable frames, `sweep.py` runs
experiments.

## Three rules you must not break

These are not style preferences. Each is enforced by a test that fails the
build, and each exists because breaking it silently invalidates published
results.

**1. Determinism.** The same seed produces byte-identical output. No wall-clock
time, no unseeded randomness, no iteration over unordered collections where the
order reaches a result.

**2. The knowledge boundary.** A UAV may act only on evidence it has received.
`autonomy.py` and `task_utility.py` import a deliberately short list of modules,
and none of them can reach world truth. Any future consumer of a recorded
trace must obey the same restriction: `tests/test_view_boundary.py` fails the
build if such a module imports `Mission`, `Simulation`, or authoritative
`Agent` state. **If a change seems to need one of these imports, stop and ask
— that is the signal for a design problem, not a missing import.**

**3. Published numbers are regression-tested.** `docs/measured_results.md` and
the README quote figures that tests pin. Moving one is allowed, but it must be
deliberate, explained, and updated in the same commit.

There is a corollary worth internalising: **label things by evidence, never by
conclusion.** No panel says "UAV-1 ALIVE"; it says `SILENT 8.7s`. The difference
is the whole project.

## Before you push

CI runs exactly this. Run it locally first — the full suite takes about five
minutes.

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pyright
.\.venv\Scripts\python.exe -m pytest
```

Coverage is gated at 85% branch-aware. New behaviour needs new tests — that is
not negotiable here, because the project's claims are the tests.

[`CONTRIBUTING.md`](../CONTRIBUTING.md) has the detail, especially the
architectural boundaries section.

## Where the work is

| Lane | Start at | Notes |
| --- | --- | --- |
| Vehicle dynamics and energy | [`vehicle_model.md`](vehicle_model.md) | Self-contained, needs no knowledge of the coordination layer. Three independently mergeable pieces. |
| Software in the loop | [`sitl.md`](sitl.md) | Phases 1-3 done: a real ArduPilot vehicle flies commands from the simulator's frame. Phase 4 is multi-vehicle. |
| Quantum allocation | not started | The one thing the challenge text names that is not built. Plugs in behind the existing allocation policy interface. |

Ask before starting a large change. The architecture has a small number of
load-bearing decisions and they are all written down — in `CONTRIBUTING.md`,
[`distributed_state_foundation.md`](distributed_state_foundation.md), and the
docstrings.
