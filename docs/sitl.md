# Software in the loop

This document prepares a software-in-the-loop path: running real autopilot
firmware (ArduPilot or PX4) underneath the coordination layer, in place of the
point-mass model.

It is written before any of it is built, because the risky decisions are
architectural and the expensive mistake would be to let SITL leak into the core.

## What this must not cost

The project's claims rest on three properties. SITL threatens all three, so each
gets an explicit rule rather than good intentions.

**Determinism.** The simulator reproduces byte-identical output from a seed.
Real firmware, a real network and wall-clock scheduling do none of that. The
rule: *a SITL run may never produce a measured result.* `eudis-swarm-sweep`,
`docs/measured_results.md`, and every table in the README stay on the
deterministic backend. SITL answers "does this behave plausibly against real
firmware", never "what is the number".

**No runtime dependencies.** The core imports nothing outside the standard
library. MAVLink bindings arrive as an optional extra (`pip install -e ".[sitl]"`)
and are imported only inside the SITL backend module, so that a missing
dependency disables one backend rather than breaking the package.

**The knowledge boundary.** Telemetry from a SITL vehicle is *world truth* — the
same category as `Agent.position`. It enters through the vehicle backend into
authoritative state, and reaches a UAV's beliefs only by the existing evidence
path: a heartbeat that has to be delivered over a link that may be down.
`autonomy.py` and `task_utility.py` must gain no import as a result of this
work. If a step appears to need one, that is a design error, not a dependency.

## The seam is two-sided, and that is the interesting part

Vehicle motion enters the system in exactly two places:

| | Call site | Question it answers |
| --- | --- | --- |
| Truth | `Agent.move_toward` | Where the aircraft *is* now |
| Prediction | `Simulation._project_positions` | Where it *would* be in `elapsed` seconds |

Both call `advance_point_mass` in [`agent.py`](../src/eudis_swarm/agent.py), so
today they are the same arithmetic and cannot disagree. The connectivity-aware
allocation policy depends on the predictive one: it chooses assignments by
projecting where aircraft would end up and evaluating the resulting link graph.

**A real autopilot cannot answer the predictive question.** There is no formula
that says where a commanded vehicle will be in 1.25 seconds; you find out by
waiting. So SITL does not simply replace the motion model — it splits it:

- the **backend** reports where the vehicle actually is, from telemetry;
- the **motion model** stays analytical, and becomes what the planner *believes*
  about flight rather than what flight is.

That divergence is not a defect to minimise. It is a new measurable, and it is
the same shape as everything else here: a planner acting on a model of the world
that the world does not honour. "How wrong is the motion model, and does that
error change coordination outcomes?" is a question the current simulator cannot
even ask.

## Interface

```python
class VehicleBackend(Protocol):
    def position(self, agent_id: int) -> Position: ...
    def command_toward(self, agent_id: int, target: Position) -> None: ...
    def advance(self, elapsed: float) -> None: ...
```

`PointMassBackend` wraps `advance_point_mass` and is the default. `MavlinkBackend`
sends position setpoints and reads telemetry. `advance` is where the two
disagree most: for the point mass it is arithmetic, for MAVLink it means letting
wall-clock time pass and re-reading state.

## Time

The simulator advances in fixed logical steps. SITL runs in wall-clock, and its
vehicles keep moving whether or not anything is asking. Two options:

1. **Lock-step.** Command, then block until telemetry shows the step's worth of
   movement. Preserves the existing loop structure exactly. Slow, and it stalls
   if a vehicle never reaches its target — which is a case that must be handled,
   because it is also a case that happens.
2. **Free-running.** Sample telemetry on a timer; logical time tracks wall-clock.
   Closer to reality, and it discards the reproducibility of step boundaries.

Start with lock-step, with ArduPilot's `--speedup` to make it bearable, and a
hard timeout per step so a stalled vehicle fails loudly. Free-running is the
better long-term answer and the wrong thing to attempt first.

## Environment on this machine

- WSL2 with Ubuntu 22.04, holding an ArduPilot checkout with `sim_vehicle.py`,
  MAVProxy and `pymavlink` already present. `ardurover` was already built;
  `arducopter` built from the warm ccache in 32 seconds.
- `pymavlink` is installed on the Windows side too, from the `sitl` extra.
- WSL2 networking is NAT here: the vehicle is reached at the WSL address rather
  than `localhost`, and the SITL binary listens on `0.0.0.0:5760` so Windows can
  reach it directly. No UDP forwarding or address discovery from inside WSL is
  needed.

## Phase 2 is done: it flies

An ArduCopter SITL instance in WSL, commanded from the Windows virtual
environment through `eudis_swarm.sitl.SitlVehicle`, armed, climbed to 20 m and
flew a three-waypoint circuit given entirely in the simulator's local metres:

```text
connected at origin 35.1263999,33.4298998
airborne at 19.3 m after 1.3s
  -> target (40.0,  0.0): reached=True  at ( 37.33, -0.01)  err=2.67 m
  -> target (40.0, 40.0): reached=True  at ( 40.15, 37.86)  err=2.15 m
  -> target ( 0.0,  0.0): reached=True  at (  2.15,  2.07)  err=2.98 m
```

Reproduce it in two terminals:

```bash
# WSL
./scripts/sitl-copter.sh

# Windows, in the virtual environment
python -c "from eudis_swarm.sitl import SitlVehicle; v = SitlVehicle('tcp:<wsl-ip>:5760'); v.connect(); v.arm_and_takeoff(20.0); v.command_toward((40.0, 0.0)); print(v.wait_until_within((40.0, 0.0), 3.0), v.position)"
```

Find `<wsl-ip>` with `wsl -d Ubuntu-22.04 -- hostname -I`. It changes when WSL
restarts, and that is the first thing to check when a link that worked
yesterday times out today.

Two things worth recording from doing it:

- **SITL streams nothing until asked.** A connection succeeds, heartbeats
  arrive, and no position ever does, because a ground station is expected to
  request the streams. `SitlVehicle.connect` requests them.
- **The arrival tolerance is real.** The vehicle settles 2-3 m from a commanded
  point rather than on it. That is not error to be tuned away; it is the
  difference between a point mass and an aircraft, and it is the first thing
  phase 3 measures.

## Phases

Each phase ends somewhere useful, so the work can stop at any boundary without
leaving a half-integration behind.

**Phase 1 — the seam, no SITL.** *(done)* Introduce `VehicleBackend`, make the existing
point-mass model an implementation of it, and prove by test that behaviour is
unchanged. Zero new dependencies, zero risk to determinism. *This is the phase
that makes the rest additive rather than invasive.*

**Phase 2 — one vehicle.** *(done)* ArduPilot SITL in WSL, `pymavlink` behind the `sitl`
extra, one aircraft flying to commanded waypoints under `MavlinkBackend`, with
the coordination layer untouched. Deliverable: a recording of one UAV completing
one task against real firmware.

**Phase 3 — measure the model error.** Fly the same scenario on both backends
and record the divergence between predicted and actual position over time. This
is the first result SITL produces that the pure simulator could not.

**Phase 4 — more than one.** Multiple SITL instances, distinct MAVLink ports.
This is where it becomes expensive: process management, port allocation, and
startup time per vehicle.

## Honest assessment

Phase 1 is worth doing regardless: it removes a duplicated motion model and
names a distinction the code was blurring.

Phases 2 and 3 are achievable and produce a genuinely new result. Phase 4 is not
a five-week task alongside everything else, and a demo that depends on four SITL
instances starting reliably in a conference room is a demo that will fail. The
deterministic simulator stays the thing that gets demonstrated; SITL is
supporting evidence that the abstraction is not a toy.
