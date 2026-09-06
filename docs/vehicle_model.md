# Vehicle and energy model

This document defines a work lane that is deliberately self-contained: it needs
no knowledge of the coordination layer, touches none of it, and can be built and
merged in independent pieces.

## What exists today

Motion is a constant-speed point update. `Agent.move_toward` in
[`agent.py`](../src/eudis_swarm/agent.py) advances an aircraft straight at the
target at `agent_speed` (default `10.0`), clamped so it never overshoots. There
is no turn constraint, no acceleration, no energy, and no endurance. An aircraft
can reverse direction instantaneously and fly forever.

That was the right simplification while the question was *who owns which task*.
It is now the weakest assumption in the project, and it is the reason
[`measured_results.md`](measured_results.md) has to say that durations are
logical simulation seconds rather than flight times.

## Why this lane is worth doing

**It retires a stated limitation.** "Absolute timings" is currently listed under
*What is not measured*. A defensible vehicle model converts every existing
result from "logical seconds" to "flight seconds for an airframe of this class"
without re-running the argument.

**It creates a second, independent cause of silence.** Today an aircraft goes
quiet because it was jammed or because it was scripted to stop. Energy depletion
adds a third cause that is *indistinguishable to its peers* — the same silence,
a different truth. That is not a complication, it is the thesis: it gives the
failure-detection layer a case it cannot resolve by design rather than by
scenario construction.

## Constraints that must hold

These are not style preferences. Each is enforced by a test that will fail the
build.

- **The knowledge boundary.** `agent.py` is authoritative world truth. Nothing
  in this lane may be imported by `autonomy.py` or `task_utility.py`, and
  nothing in the dashboard package may import `Agent`, `Mission` or
  `Simulation`. If a UAV is to *believe* something about its own remaining
  energy, that belief travels as a per-agent trace field, exactly as peer
  evidence does.
- **Determinism.** The same seed must produce byte-identical output. No
  wall-clock, no unseeded randomness, no iteration over unordered sets in a way
  that reaches results.
- **No new runtime dependencies.** The package depends on nothing outside the
  standard library, and that claim is load-bearing. Numerical work belongs in
  plain Python; if something genuinely needs NumPy it goes behind an optional
  extra, never in the core path.
- **All gates green.** `pytest`, `ruff check`, `ruff format --check`, `pyright`,
  and 85% coverage, after every commit. New behaviour needs new tests.

## Deliverables, in order

Each is independently mergeable and independently useful. Do not start the next
before the previous is green.

### 1. Kinematic feasibility

Replace the instantaneous-turn assumption with a bounded turn rate, so a track
is something the airframe could actually fly. Minimum viable version: carry a
heading on the aircraft, limit heading change per step, and turn toward the
target rather than snapping to it.

The interesting consequence to measure: a bounded turn radius means the cost of
re-tasking is no longer symmetric. Reassigning a task to an aircraft that has
just passed it is expensive in a way the current model cannot express — which
bears directly on whether the coordination layer's choices are good ones.

### 2. Energy

A consumption model as a function of speed and manoeuvre, an endurance budget,
and depletion as a terminal state. Depletion must present to the rest of the
system as an ordinary stop: no announcement, no special-casing in the failure
detector. If peers can tell an exhausted aircraft from a jammed one, the model
has leaked.

Add a scenario preset where an aircraft runs out of energy mid-mission, and
assert that its peers reach the same evidence state they would for any other
silence.

### 3. Calibration

Pick a real airframe class — a fixed-wing tactical UAS or a multirotor, your
call — and justify `agent_speed`, the turn rate, the energy curve, and the
100×100 area against published figures. Cite sources in this document. The
output is a paragraph that lets us say "these are flight seconds for a vehicle
of this class" and defend it.

## Open questions for whoever takes this lane

These are genuinely open. Decide them and record the decision here.

- **Fixed-wing or multirotor?** They give opposite answers about whether
  loitering is cheap, which changes what a good allocation looks like.
- **Is energy observable to the aircraft itself?** A UAV plausibly knows its own
  state of charge. If it does, does it get to decline a task it cannot reach and
  return? That is a coordination question hiding inside a mechanical one, and it
  is the natural place where this lane starts to interact with the rest.
- **2D or 2.5D?** Altitude affects both energy and radio range. Adding it is a
  larger change than it looks.

## Acceptance

The lane is done when a run of `eudis-swarm --scenario baseline` produces a
mission duration we are willing to describe in seconds of flight, with the
reasoning written down, and when the *What is not measured* section of
[`measured_results.md`](measured_results.md) is one bullet shorter.
