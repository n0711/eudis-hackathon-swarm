# Software in the loop

This document records the standalone ArduPilot experiment and the remaining
software-in-the-loop (SITL) integration boundary. `Simulation` currently runs
the deterministic point-mass backend. `SitlVehicle` can command and observe one
ArduPilot vehicle, but it is not a `VehicleBackend` and is not wired into the
simulation loop.

## What this must not cost

The project's claims rest on three properties. SITL threatens all three, so each
gets an explicit rule rather than good intentions.

**Determinism.** The simulator reproduces byte-identical output from a seed.
Real firmware, a real network and wall-clock scheduling do none of that. The
rule: *a SITL run may never produce a deterministic coordination sweep result.*
`eudis-swarm-sweep`, `docs/measured_results.md`, and every coordination table
in the README stay on the deterministic backend. The standalone SITL experiment
below reports observed model error with its sample and repeatability limits.

**No runtime dependencies.** The core imports nothing outside the standard
library. MAVLink bindings arrive as an optional extra (`pip install -e ".[sitl]"`)
and are imported only inside the SITL backend module, so that a missing
dependency disables one backend rather than breaking the package.

**The knowledge boundary.** Telemetry from a SITL vehicle is *world truth* — the
same category as `Agent.position`. A future adapter must put observations into
authoritative state; they may reach a UAV's beliefs only by the existing evidence
path: a heartbeat that has to be delivered over a link that may be down.
`autonomy.py` and `task_utility.py` must gain no import as a result of this
work. If a step appears to need one, that is a design error, not a dependency.

## The seam is two-sided, and that is the interesting part

Vehicle motion enters the system in exactly two places:

| | Call site | Question it answers |
| --- | --- | --- |
| Truth | `VehicleBackend.move_toward` | Where the aircraft *is* now |
| Prediction | `Simulation._project_positions` | Where it *would* be in `elapsed` seconds |

The default [`PointMassBackend`](../src/eudis_swarm/vehicle_backend.py) and the
communication-only projection both use `advance_point_mass`, so today they are
the same arithmetic. That projection supplies an observational graph snapshot;
it does not choose or authorize a task. The connectivity-aware task scorer
instead evaluates each task position against *delivered* peer positions.

**A real autopilot cannot answer the predictive question.** There is no formula
that says where a commanded vehicle will be in 1.25 seconds; you find out by
waiting. So SITL does not simply replace the motion model — it splits it:

- the **backend** reports where the vehicle actually is, from telemetry;
- the **communication-only graph projection** remains analytical until an
  observation arrives. It must not be mistaken for telemetry or fed directly
  into receiver-local task choice.

That divergence is measurable. How it affects delivered peer evidence and
subsequent task choices remains an open experiment; the six-leg observation
below alone cannot answer it.

## Implemented interface and remaining contract

```python
class VehicleBackend(Protocol):
    def move_toward(self, agent: Agent, target: Position, elapsed: float) -> None: ...
```

`PointMassBackend` owns the current arithmetic and is the default. The call
mutates authoritative `Agent.position` in place. `Agent.move_toward` delegates
to it for compatibility with callers outside `Simulation`. Communication-only
boundaries still call the analytical predictor without invoking the backend.
There is no `MavlinkBackend` yet; `SitlVehicle` in `sitl.py` offers separate
`command_toward` and `observe` operations.

Before adapting `SitlVehicle`, specify and test the following contract:

1. `Simulation` alone decides when a physical update is due. An adapter may
   command a target and report only a timestamped *observed* position; it must
   never silently substitute the analytical prediction for missing telemetry.
2. The adapter updates physical position only. It must not update claims,
   ownership, `status`, peer stores, or autonomy. Existing transport remains
   the only route from world truth to receiver-local beliefs.
3. A physically stopped vehicle must not be commanded or advanced. A *declared*
   failure must not itself issue a vehicle-stop command. For a real autopilot,
   merely setting `responsive=False` cannot stop previously commanded motion;
   fault injection therefore needs a separate, safe SITL-only action before
   a mixed run can claim to model a fail-stop.
4. Logical simulator time and autopilot `time_boot_ms` need an explicit mapping.
   Each observation must have a bounded wait and a defined timeout/error path;
   no last-known position may be presented as a fresh observation.
5. Nondeterministic backends must be rejected by the sweep and measured-results
   paths. The current sweep constructs its own default `Simulation`, so it is
   deterministic today; a new backend-selection option must enforce this guard.

Acceptance tests for that future adapter should cover command/observation
ordering, stale or missing telemetry, timeout, physical stop versus declaration,
no backend call on communication-only boundaries, and evidence delivery across
a partition. None requires changing task allocation or autonomy semantics.

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

## Phase 3: how wrong is the motion model?

The planner predicts motion with `advance_point_mass`: constant speed, instant
acceleration, exact arrival. Flying the same 60 m leg repeatedly under
ArduCopter SITL, with the model given the autopilot's own 10 m/s so it gets its
best case:

```bash
./scripts/sitl-copter.sh                                    # in WSL
python scripts/sitl_model_error.py tcp:<wsl-ip>:5760 --legs 6
```

| | min | max | mean |
| --- | ---: | ---: | ---: |
| Times longer than the model predicts | 1.53 | 1.76 | **1.71** |
| Worst position error during the leg (m) | 25.25 | 37.26 | **34.43** |
| Arrival error (m) | 1.40 | 1.99 | 1.67 |

**The model is wrong by more than half a leg length, at the worst possible
moment.** Peak divergence lands at 5.8 s on a leg the model says takes 5.8 s:
the predicted aircraft has arrived and stopped while the real one is still
decelerating a third of the way back. Error is smallest at the start and end;
its effect on a coordination decision has not been tested.

### Why this could matter to coordination

`Simulation._project_positions` predicts positions for the communication graph
at observational boundaries. It does not feed the connectivity-aware scorer
directly. That scorer uses a candidate task's position and receiver-local
snapshots of peer positions. A real vehicle's slower flight could still change
which links exist, which snapshots arrive, and what a UAV later chooses; the
direction and size of that effect have **not** been measured. One integrated
SITL vehicle and a matched deterministic comparison would test the hypothesis.

### What this measurement is not

Six legs on one vehicle, one airframe, one speed, one leg length. The spread
above is reported as a range rather than a mean-with-error because **SITL is not
reproducible**: the same commands give 1.53 and 1.76 on different legs of the
same flight. That is the reason these numbers live here and not in
[`measured_results.md`](measured_results.md), and the reason no coordination
claim rests on them.

## Phases

Each phase ends somewhere useful, so the work can stop at any boundary without
leaving a half-integration behind.

**Phase 1 — the seam, no SITL.** *(done)* Introduce `VehicleBackend`, make the existing
point-mass model an implementation of it, and prove by test that behaviour is
unchanged. Zero new dependencies, zero risk to determinism. *This is the phase
that makes the rest additive rather than invasive.*

**Phase 2 — one standalone vehicle.** *(done)* ArduPilot SITL in WSL, `pymavlink`
behind the `sitl` extra, one aircraft flying to commanded waypoints under
`SitlVehicle`, with the coordination layer untouched. This did not run a
`Simulation` mission against firmware.

**Phase 3 — measure the model error.** *(done as a standalone comparison)* Fly
repeated legs on one SITL vehicle and compare observations with the point-mass
prediction. This is not a comparison of two `Simulation` backends.

**Next — integrate one vehicle.** Implement the contract above behind an adapter
and verify one `Simulation` mission without changing its evidence semantics.

**Later — more than one.** Multiple SITL instances, distinct MAVLink ports.
This is where it becomes expensive: process management, port allocation, and
startup time per vehicle.

## Honest assessment

Phase 1 is worth doing regardless: it removes a duplicated motion model and
names a distinction the code was blurring.

The standalone flights and comparison produce a useful observation. Integrating
one vehicle with `Simulation` should precede any multi-vehicle attempt. Several
SITL instances add process management, port allocation, and startup time; keep
the deterministic simulator as the primary demonstration and use SITL as
supporting evidence.
