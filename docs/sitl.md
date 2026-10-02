# Software in the loop

This document records the standalone ArduPilot experiment and the
software-in-the-loop (SITL) integration boundary. `Simulation` runs the
deterministic point-mass backend by default. `SitlVehicle` can command and
observe one ArduPilot vehicle, and [`MavlinkBackend`](../src/eudis_swarm/mavlink_backend.py)
now adapts it to the `VehicleBackend` interface so one agent in an otherwise
normal mission can be driven by live telemetry -- see "Next: integrate one
vehicle" below for what that does and does not demonstrate.

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
`MavlinkBackend` (`src/eudis_swarm/mavlink_backend.py`) adapts `SitlVehicle`'s
separate `command_toward`/`observe` operations to the single `move_toward`
call `Simulation` makes; any agent not named in its vehicle mapping still
moves through `PointMassBackend`, unchanged.

Against the contract specified before it was built:

1. **Met.** `Simulation` alone calls `move_toward`; the adapter commands a
   target and reports only `SitlVehicle.position`, the last *observed*
   telemetry. It never substitutes the analytical prediction.
2. **Met.** The adapter's only effect is `agent.position = vehicle.position`.
   It imports nothing from `task_claims.py`, `peer_state.py`, or `autonomy.py`.
3. **Partly met.** A physically stopped (`responsive=False`) agent is never
   commanded or advanced here, and a merely *declared* failure does not stop
   it — both tested. What is **not** built: the doc's own caveat that setting
   `responsive=False` "cannot stop previously commanded motion" on a real
   autopilot still holds. A UAV injected with a physical failure mid-flight
   stops receiving new setpoints but keeps flying toward its last one; a
   genuine fail-stop still needs the separate, safe SITL-only action
   (e.g. commanding RTL or a loiter) this adapter does not send.
4. **Met.** Lock-step on `vehicle.vehicle_time_s`, never wall-clock; a stalled
   step raises `TimeoutError` rather than reporting a stale position as fresh.
5. **Met structurally, not by a runtime guard.** No CLI or `sweep.py` option
   selects a backend, so there is nothing for a runtime check to reject yet.
   `MavlinkBackend` is reachable only from `scripts/sitl_mission_demo.py`,
   never from `eudis_swarm.simulation`'s or `eudis_swarm.sweep`'s import graph
   (enforced by `tests/test_mavlink_backend.py`, mirroring `test_sitl.py`'s
   own check for `sitl.py`). If a backend-selection flag is ever added to
   either CLI, it must carry this guard explicitly.

`tests/test_mavlink_backend.py` covers command/observation ordering, stale or
missing telemetry and timeout, physical stop versus declaration, no backend
call on communication-only boundaries, and one full `Simulation` mission with
one integrated agent converging exactly as the all-point-mass case does. None
of it changes task allocation or autonomy semantics, and none of it needs a
live autopilot — the same discipline `test_sitl.py` already follows.

## Time

The simulator advances in fixed logical steps. SITL runs in wall-clock, and its
vehicles keep moving whether or not anything is asking. Two options:

1. **Lock-step.** Command, then block until telemetry shows the step's worth of
   movement. Preserves the existing loop structure exactly. Slow, and it stalls
   if a vehicle never reaches its target — which is a case that must be handled,
   because it is also a case that happens.
2. **Free-running.** Sample telemetry on a timer; logical time tracks wall-clock.
   Closer to reality, and it discards the reproducibility of step boundaries.

`MavlinkBackend` implements lock-step: run ArduPilot with `--speedup` to make
it bearable, and it raises after a hard wall-clock timeout per step so a
stalled vehicle fails loudly rather than hanging. Free-running remains the
better long-term answer and was deliberately not attempted first.

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

**Phase 4 — integrate one vehicle.** *(adapter done; not yet flown)*
`MavlinkBackend` and `scripts/sitl_mission_demo.py` implement the contract
above and are verified against `tests/test_mavlink_backend.py`'s fakes,
including one full `Simulation` mission converging with the same evidence
semantics as the all-point-mass case. **No one has yet run the script against
a live ArduPilot instance** — that needs the WSL2/ArduPilot environment
described above, which this change was written without access to. Treat it as
implemented and tested, not as flown, until someone runs it and records what
happened here.

**Later — more than one.** Multiple SITL instances, distinct MAVLink ports.
This is where it becomes expensive: process management, port allocation, and
startup time per vehicle.

## Honest assessment

For the October hackathon, use the deterministic simulator and its trace replay
as the primary demonstration. The standalone one-vehicle SITL result supports a
model-error discussion; by itself it still does not demonstrate swarm
coordination on ArduPilot. See the [demo runbook](demo_runbook.md) for the
repeatable presentation path.

Phase 1 is worth doing regardless: it removes a duplicated motion model and
names a distinction the code was blurring.

Phase 4 closes the specific gap Phase 2/3 left open — a real autopilot's
telemetry now can drive one agent inside an actual coordination mission — but
it is a coded, tested capability waiting on a live run, not a result. Before
presenting it as one, run `scripts/sitl_mission_demo.py` against a real SITL
instance at least once and record what actually happened, the same way Phase
2 and Phase 3's numbers above were recorded rather than assumed. It also does
not model a true fail-stop on a real vehicle (see point 3 above) — say so if
asked, rather than letting the distributed-failure story imply it does.

Several SITL instances add process management, port allocation, and startup
time; multi-vehicle SITL stays out of scope for this hackathon. Keep the
deterministic simulator as the primary demonstration and use the one-vehicle
integration, once actually flown, as a second, narrower proof point.
