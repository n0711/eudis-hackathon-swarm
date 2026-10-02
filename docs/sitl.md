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
3. **Met (Phase 5).** A physically stopped (`responsive=False`) agent is never
   commanded or advanced here, and a merely *declared* failure does not stop
   it — both tested. Setting `responsive=False` alone cannot stop motion a real
   autopilot was already commanded into, so `MavlinkBackend.apply_physical_faults`
   now sends a physically failed vehicle **exactly one** safe-hold mode change
   — ArduCopter `BRAKE` (default) or `LOITER`, ArduRover `HOLD` — and records
   it (`FailStopEvent`: mode, logical time, whether the autopilot confirmed it,
   the vehicle clock and position just before the command). It reads only
   `Agent.responsive`, never `Agent.status`, so a wrongly *declared* UAV keeps
   flying; it is never repeated, even if the confirmation does not arrive.
   `live_mission.InstrumentedSimulation` calls it immediately after the fault
   schedule is applied, before anything else at that boundary. Measured:
   the hold is a **stop command, not an instantaneous stop** — a copter at
   cruise speed travels about 16-19 m in BRAKE before it is at rest (Phase 5).
4. **Met.** Lock-step on `vehicle.vehicle_time_s`, never wall-clock; a stalled
   step raises `TimeoutError` rather than reporting a stale position as fresh.
   Since Phase 5 the target is anchored (see "Time"), every report is pumped
   fresh from a drained telemetry backlog, must be strictly newer than the
   previous one, and its drift is recorded.
5. **Met structurally, not by a runtime guard.** No CLI or `sweep.py` option
   selects a backend, so there is nothing for a runtime check to reject yet.
   `MavlinkBackend` is reachable only from `scripts/sitl_mission_demo.py` and
   the Phase 5 harness (`live_mission`, `sitl_swarm`, `live_report` and their
   scripts), never from `eudis_swarm.simulation`'s or `eudis_swarm.sweep`'s
   import graph (enforced by `tests/test_mavlink_backend.py` and
   `tests/test_live_mission.py`, mirroring `test_sitl.py`'s own check for
   `sitl.py`; none of the harness modules imports pymavlink at import time). If a backend-selection flag is ever added to
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

**Anchored lock-step (Phase 5).** Several independent SITL processes stepped
one after another keep running while the others are waited on. The original
rule — wait until `last observed + elapsed` — rounded every step up to the
next telemetry report, so its error accumulated (LT2 measured 1.6x). Now every
vehicle is anchored once, in a single pass at the first integrated step (the
telemetry backlog built up during takeoff is drained first, then every clock is
read back to back), and each step waits only until
`anchor + cumulative logical elapsed`. A vehicle already ahead is not waited
on; its lead is recorded as `drift_s` on every step. The bound is one
telemetry interval plus one poll (the stream rate is now a parameter,
`--stream-rate`, default 10 Hz). What anchoring **cannot** do is slow a
vehicle down: if one SITL instance runs faster than the others (CPU
contention at a high `--speedup`), its drift grows and the log shows it. In
one early run in the 4-vCPU cloud container (`--speedup 5`, before all anchors
were set in one pass) one instance's clock advanced about 17 % more than
another's over the mission and its drift grew to 3.3 s; after the fix, every
recorded Phase 5 run at `--speedup 5` stayed under 0.71 s. Use a speedup every
instance can sustain; the per-vehicle drift table in the report is the check.

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

**Phase 4 — integrate one vehicle.** *(done; flown in SITL)*
`MavlinkBackend` and `scripts/sitl_mission_demo.py` implement the contract
above and are verified against `tests/test_mavlink_backend.py`'s fakes,
including one full `Simulation` mission converging with the same evidence
semantics as the all-point-mass case. Flown on 2 October 2026 against a fresh
ArduCopter SITL instance (`ArduPilot-4.6.0-beta1-7231-g7d2d454eba`, quad,
`--speedup 5`) on this machine:

```text
connecting to tcp:127.0.0.1:5760 ...
airborne at 19.1 m; starting the mission
completed 2/2 tasks
mission duration (logical seconds): 4.50
agent 1 final position (from live telemetry): (33.71452158974091, 20.716557236686313)
```

Two things the flight taught that the fakes could not:

- **Arrival tolerance.** The simulator's 0.75 m default assumes a point mass;
  a copter settles 1.4-2.0 m away (phase 3), so no task would ever complete.
  The live mission uses a 3.0 m tolerance.
- **Clock drift.** Over 18 steps of 0.25 s logical time the vehicle clock
  advanced 0.40 s per step — 7.20 s against 4.50 s. Telemetry was fresh; the
  cause is quantisation: position arrives at 5 Hz (every 0.2 s of vehicle
  time), and waiting for "last observation + elapsed" overshoots to the next
  report on every step, so the error accumulates. Anchoring each vehicle's
  wait to its first step plus cumulative logical time bounds the error to one
  telemetry interval instead.

Evidence bundle: `evidence/20261002T040554Z-LT2` (see
[`trl4_validation.md`](trl4_validation.md)).

**Phase 5 — a multi-vehicle swarm with a safe fail-stop.** *(done; flown in
SITL, see below)* Every agent on live ArduPilot telemetry, copters and rovers
in one mission, one vehicle physically failed mid-mission and put into a safe
hold, its work recovered by the others through lease expiry.

## Phase 5: a multi-vehicle swarm on live firmware

### What was built

| Piece | Where | What it does |
| --- | --- | --- |
| Anchored lock-step | `mavlink_backend.py` | One anchor per vehicle clock, set for all vehicles in one pass; each step waits only while a vehicle is behind `anchor + cumulative logical time`, pumps fresh telemetry, refuses a report not newer than the last, records drift. `synchronize()` steps hovering vehicles too. |
| Safe fail-stop | `mavlink_backend.py`, `sitl.py` | `apply_physical_faults()` sends one `BRAKE`/`LOITER` (copter) or `HOLD` (rover) to a vehicle whose agent is physically non-responsive, never on a declaration; `SitlVehicle.set_mode`/`hold` confirm the mode from heartbeats. |
| Shared frame | `sitl.py` | `SitlVehicle(origin=...)`: every vehicle reports in one mission frame, so one local metre is the same place for all of them and a task maps to one global point. Without `origin` a single vehicle still pins to its own first fix. |
| Ground vehicles | `sitl.py`, `sitl_swarm.py` | `SitlVehicle.arm()` (GUIDED + arm, no takeoff; only the arm command's own ACK counts), `HOLD` as the rover's safe mode, per-class arrival tolerance. |
| Evidence log | `live_mission.py` | `InstrumentedSimulation` observes a normal `Simulation` and writes JSON lines: header (config, vehicle types, safe modes, tolerances), one record per mission boundary (logical time; each agent's truth; each live vehicle's observed position, altitude, clock and drift; every receiver's ownership view of every task; events incl. `FAIL_STOP`), and a result record. On the point-mass backend it reproduces a plain `Simulation` exactly and its log is byte-identical between runs (tested). |
| Report | `live_report.py`, `scripts/sitl_swarm_report.py` | Derives everything from the log alone: tasks, mission time, detection latency, orphaned tasks and recovery latency, concurrent ownership, minimum pairwise 3-D separation and steps below a threshold, per-vehicle drift, the observed hold (distance moved after the command, final speed). `--check` evaluates the LT3 test card's P1-P8 mechanically and exits non-zero unless every run passes; each log file is summarised on its own (`--combine` merges files). |
| Chart | `live_chart.py`, `scripts/sitl_swarm_chart.py` | One offline HTML page per log (needs the `charts` extra): live tracks beside point-mass tracks on the same axes, tasks, the failure point and where the failed vehicle actually came to rest, observed altitude bands over time, minimum separation against the threshold, plus the comparison table and checklist as text. |
| LT3 runner | `scripts/sitl-lt3.sh` | The whole test card in one command: for each run, fresh instances, fly, stop; then `--check` over every log; exit 0 only when every run passed. |
| Launcher | `scripts/sitl-swarm.sh` | N instances (`-I i`, TCP `5760+10i`, sysid `i+1`), copters and/or rovers, each in its own directory, each starting at its agent's simulated start position about a shared origin; `stop`, `status`, `DRY_RUN=1`. |
| Mission | `sitl_swarm.py`, `scripts/sitl_swarm_mission.py` | Connects N vehicles, checks each is at its simulated start, arms copters to layered altitudes and rovers on the ground, flies one distributed-control mission (all agents or `--live-agents`) with a scheduled physical failure and optional `--jammer`/`--comm-fault`, holds every vehicle if anything aborts, lands copters, writes the log, then flies the identical configuration on the point-mass backend and prints both side by side. |

Definitions the report uses:

- **Recovery latency after failure:** from the injected physical failure until
  every task the failed UAV owned *in its own replica* at that moment is
  `OWNED_BY_SELF` or `COMPLETE` in some surviving receiver's replica.
- **Concurrent-ownership time:** the simulator's own evaluator metric
  (task-seconds with more than one live owner across replicas).
- **Min 3-D separation:** per step, the smallest east/north/altitude distance
  between any two live vehicles as *observed* (held vehicles included);
  reported with the pair, the time, and how many steps fell below
  `--separation-threshold` (default 5 m).
- **Drift:** vehicle seconds the vehicle's own clock was ahead of the logical
  time it was stepped to. Never negative by construction.

**Deconfliction is altitude layering.** Copter *k* (0-based, in agent order)
takes off to and holds `--altitude + k * --altitude-step` (default 20, 25, 30,
35 m); every later setpoint keeps that altitude, so crossing tracks never share
an altitude. Rovers are on the ground and outside the layering. The separation
metric is the evidence that it held.

**Per-class arrival tolerance.** Copter 2.0 m (settles 1.4-2.0 m, phase 3;
LT2 used 3.0 m with the single-vehicle demo). Rover 1.5 m, from a measured
0.15-0.81 m settle over six legs
([`data/sitl/rover_settle_2026-10-02.txt`](data/sitl/rover_settle_2026-10-02.txt)).
The point-mass comparison run uses the same per-agent tolerances. A rover also
drives far slower than the simulator's 10 m/s (about 2.8 m/s to within 3 m of
a 20 m target); that is fine, since its motion comes from telemetry, but its
legs take longer than the point-mass run predicts.

### Results

All runs below are **non-deterministic** SITL runs. They belong here, never in
[`measured_results.md`](measured_results.md).

**Owner's laptop, 2026-10-02, three fresh runs** (WSL2,
`ArduPilot-4.6.0-beta1-7231-g7d2d454eba`, 4 x ArduCopter quad, `--speedup 5`,
commit `b6471d9` as merged into the integration branch at `5d4cf88`;
`scripts/sitl-swarm.sh 4 5`, then `scripts/sitl_swarm_mission.py --count 4`
with defaults: 8 tasks, UAV 2 physically failed at t = 4.0 s, BRAKE). This is
the LT3 record:

| run | tasks | mission completed | failure detection | recovery after failure (point-mass) | max drift | fail-stop |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 8/8 | yes | 1.75 s | 6.75 s (2.50 s) | 0.451 s | UAV 2 BRAKE at 4.00 s confirmed; moved 16.5 m over 12.7 s; final 0.03 m/s |
| 2 | 8/8 | yes | 1.75 s | 6.75 s (2.50 s) | 0.502 s | BRAKE confirmed; 15.7 m over 13.3 s; 0.03 m/s |
| 3 | 8/8 | yes | 1.75 s | 7.25 s (2.50 s) | 0.450 s | BRAKE confirmed; 15.8 m over 12.8 s; 0.00 m/s |

Takeoff altitudes 19.1 / 23.9 / 28.6 / 33.4 m (layering held); concurrent
ownership 0.00 task-seconds in every run; about 26 s wall per run including
arming and takeoff of all four.

**Cloud development container, 2026-10-02** (4 vCPU Linux, ArduPilot `master`
at `e8de77a` — ArduCopter / ArduRover V4.8.0-dev — built here with
`./waf configure --board sitl && ./waf copter && ./waf rover`; commit
`a96dad2`; `--speedup 5`; fresh instances every run; same defaults as above).
Console output and the full JSONL evidence logs are in
[`data/sitl/phase5/`](data/sitl/phase5/); re-derive any table with
`python scripts/sitl_swarm_report.py docs/data/sitl/phase5/<run>.jsonl`.

| run | vehicles | failure | tasks | mission time (point-mass) | detection | recovery (point-mass) | concurrent ownership (point-mass) | min 3-D separation | max drift | fail-stop |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `c5` | 4 copters | UAV 2 at 4 s | 8/8 | 14.25 s (8.50) | 1.75 s | 7.25 s (2.50) | 0.00 (0.00) | 14.6 m, 0/57 steps < 5 m | 0.700 s | BRAKE confirmed; 17.4 m over 10.2 s; 0.00 m/s |
| `c5b` | 4 copters | UAV 2 at 4 s | 8/8 | 14.75 s (8.50) | 1.75 s | 7.00 s (2.50) | 0.00 (0.00) | 14.9 m, 0/59 | 0.600 s | BRAKE confirmed; 15.6 m over 11.0 s; 0.03 m/s |
| `c5c` | 4 copters | UAV 2 at 4 s | 8/8 | 14.50 s (8.50) | 1.75 s | 7.25 s (2.50) | 0.00 (0.00) | 13.0 m, 0/58 | 0.652 s | BRAKE confirmed; 16.1 m over 10.4 s; 0.05 m/s |
| `mix5` | 3 copters + 1 rover | rover (UAV 4) at 8 s | 8/8 | 16.00 s (8.50) | 1.75 s | 2.25 s (nothing orphaned) | 0.00 (0.00) | 17.0 m, 0/64 | 0.450 s | HOLD confirmed; 10.5 m over 8.3 s; 0.11 m/s |
| `jam5` | 4 copters, jammer 50,50 r30 from 3 to 9 s | UAV 2 at 4 s | 8/8 | 15.50 s (7.75) | 5.00 s | 7.25 s (2.25) | 0.00 (1.50) | 15.2 m, 0/62 | 0.500 s | BRAKE confirmed; 15.7 m over 11.9 s; 0.03 m/s |

Every run meets every pass criterion of the test card below. Copter takeoff
altitudes were 19.0-19.1 / 23.8-23.9 / 28.5-28.6 / 33.3 m; wall time was
24-27 s per run including arming and takeoff. Two cells need reading with
care, and neither is a fault of the live run:

- In `mix5` the point-mass rover had already finished its work when it
  failed at 8 s (point-mass finishes the whole mission at 8.50 s), so there
  was nothing to orphan or detect there; the live rover, slower, still owned
  task 6. The recovery comparison is only like for like when both runs orphan
  the same task, which the default (UAV 2 at 4 s) does.
- In `jam5` the point-mass run shows 1.50 task-seconds of concurrent ownership
  and no detection within its 7.75 s mission; the live run, with different
  geometry under the jammer, shows none and a 5.00 s detection. These are two
  different trajectories through the same protocol, not one result and its
  error bar.

`scripts/sitl-lt3.sh 2 5` was itself run end to end in the same container
(two fresh 4-copter runs, both meeting P1-P8, `LT3 PASS over 2 run(s)`, exit 0).

The same mixed swarm was also flown at `--speedup 2` before the final commit
(copter UAV 2 failed at 4 s: 8/8, 21.00 s, recovery 10.25 s, BRAKE confirmed,
16.3 m to rest; min separation 14.4 m) with the same outcome.

### What the flights taught that the fakes could not

- **The safe hold is a stop command, not an instantaneous stop.** A copter
  failed at cruise speed travels about 16-19 m in BRAKE before it is at rest
  (final speeds 0.00-0.03 m/s); a rover in HOLD travelled 10.5 m. The
  simulator's failed agent stops dead where it failed. The difference is real
  separation the swarm does not know about; a deployment would need a margin
  for it.
- **Recovery takes longer on firmware than on the point-mass model** (about
  6.75-7.25 s against 2.50 s on the laptop). Detection latency is identical
  (1.75 s, it is protocol time); the gap is survivors' real flight time to the
  orphaned task, including acceleration the point-mass model does not have
  (phase 3).
- **Instances do not all run at the speedup you ask for.** Under CPU
  contention one instance can run faster than another; anchoring then records
  a growing drift for it instead of hiding it. Pick a `--speedup` every
  instance sustains, and check the drift table.
- **The SITL binary exits when its parent process exits.** The launcher keeps
  a small waiting parent per instance; starting the binary with `exec` from a
  short-lived script made instances die on first connection.
- **A telemetry backlog is the normal case with several vehicles.** Reading
  one message returns the oldest; `SitlVehicle` now drains to the newest. The
  first drain after a takeoff sequence takes about 0.5 s, which is why all
  anchors are set in one pass after draining.

### Lab run on WSL2 (exact commands)

Prerequisites (once):

```bash
# WSL2 Ubuntu 22.04, ArduPilot checkout at ~/ardupilot
cd ~/ardupilot && ./waf configure --board sitl && ./waf copter && ./waf rover
# this repository, Python 3.11+
cd ~/eudis-hackathon-swarm
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev,sitl]"
```

Run (all in WSL; the mission script can also run on Windows with
`--host <wsl-ip>`, see "Environment on this machine"):

```bash
# 1. start four copters (or: scripts/sitl-swarm.sh copter copter copter rover 5)
RUN_DIR=/tmp/lt3 scripts/sitl-swarm.sh 4 5
scripts/sitl-swarm.sh status            # RUN_DIR=/tmp/lt3 as above

# 2. fly the mission and the point-mass comparison
python scripts/sitl_swarm_mission.py --count 4 --log lt3-run1.jsonl \
    | tee lt3-run1.txt
#   mixed swarm: add  --vehicles copter,copter,copter,rover
#   rover failure:    --failure-agent 4 --failure-time 8
#   partition:        --jammer 50,50,30,3,9   or   --comm-fault 3,2,6

# 3. stop every instance (fresh instances for every run)
RUN_DIR=/tmp/lt3 scripts/sitl-swarm.sh stop

# 4. later, anywhere: the comparison table and the test-card verdict
python scripts/sitl_swarm_report.py --check lt3-run1.jsonl
# 5. a picture of the run for the slides (pip install -e ".[charts]")
python scripts/sitl_swarm_chart.py lt3-run1.jsonl      # writes lt3-run1.html
```

Or the whole test card, three fresh runs and the verdict, in one command:

```bash
scripts/sitl-lt3.sh 3 5                       # results in lt3-<UTC time>/
VEHICLES=copter,copter,copter,rover scripts/sitl-lt3.sh 3 5 -- \
    --failure-agent 4 --failure-time 8        # the mixed-swarm variant
```

It ends with `LT3 PASS over 3 run(s)` (exit 0) or `LT3 FAIL ...` (exit 1),
and leaves `runN.jsonl`, `runN.txt` and `report.txt` in the results folder.

The launcher's `ORIGIN` and the mission's `--origin` must agree (both default
to `35.1264,33.4299`); the mission aborts before arming if any vehicle is more
than `--start-tolerance` (8 m) from its simulated start, which is what a
mismatch looks like.

### Test card: multi-vehicle mission with a physical fail-stop (LT3)

**Purpose.** C1 + C2 + C4 together: every agent on live ArduPilot firmware,
one vehicle physically failed mid-mission and put into a safe hold, its work
recovered by the others through lease expiry, deconfliction evidenced.

**Prerequisites.** As "Lab run" above; `pytest` green on the commit under test
(LT1); nothing else heavy running (the drift check depends on it).

**Steps** (steps 2-5 are exactly what `scripts/sitl-lt3.sh 3 5` automates;
use it unless you need to watch each stage).

1. `git rev-parse HEAD` — record the commit. `git status` must be clean.
2. Start fresh instances: `RUN_DIR=/tmp/lt3 scripts/sitl-swarm.sh 4 5`.
   Expected: four `started instance i (agent i+1, copter) on TCP 57x0` lines;
   `status` shows four `running`.
3. Run `python scripts/sitl_swarm_mission.py --count 4 --log lt3-runN.jsonl | tee lt3-runN.txt`.
   Expected, in order: four `connecting` lines; four `airborne at` lines at
   about 19, 24, 29, 33 m; `starting the live mission (NON-deterministic)`;
   then the comparison table and the per-vehicle drift table; then
   `evidence log: lt3-runN.jsonl`. About 30 s wall at speedup 5.
4. Stop the instances: `RUN_DIR=/tmp/lt3 scripts/sitl-swarm.sh stop`.
5. Repeat steps 2-4 for three runs in total, fresh instances each time.
6. Optional variants, each recorded separately: mixed swarm
   (`scripts/sitl-swarm.sh copter copter copter rover 5` and
   `--vehicles copter,copter,copter,rover`); rover failure
   (`--failure-agent 4 --failure-time 8`); a partition (`--jammer 50,50,30,3,9`).
7. Verdict: `python scripts/sitl_swarm_report.py --check lt3-run*.jsonl`
   prints a checklist per run and `LT3 verdict: PASS (3/3 runs passed)`.
   Optional picture: `python scripts/sitl_swarm_chart.py lt3-run1.jsonl`.
8. Bundle the evidence:
   `python scripts/collect_evidence.py LT3 --result PASS|FAIL --note "..." --attach lt3-run*.jsonl lt3-run*.txt`.

**Record** for each run: the commit, ArduPilot version (first lines of
`/tmp/lt3/inst0/sitl.log`), speedup, the full table, the drift table, the
fail-stop cell, and the min-separation cell.

**Pass criteria — every run must show all of** (`--check` evaluates exactly
these; P8 there checks that the point-mass run completed, and the test suite
pins its default figures):

| # | Criterion | Where it shows |
| --- | --- | --- |
| P1 | Every task completed and the mission completed | `Tasks completed 8/8`, `Mission completed yes` (sitl column) |
| P2 | The failure was physical and the fail-stop was sent once and confirmed | `Fail-stop: UAV 2 BRAKE at 4.00 s (confirmed ...)`, exactly one entry |
| P3 | The failed vehicle actually stopped | the same cell's final speed < 0.5 m/s |
| P4 | Its work was recovered by the survivors | `Recovery latency after failure` is a time, not `NOT recovered` |
| P5 | No duplicate ownership | `Concurrent-ownership time 0.00 task-s` |
| P6 | Deconfliction held | `Min 3-D separation`: `0/... steps < 5 m` |
| P7 | Lock-step stayed honest | every vehicle's max drift < 1.0 s in the drift table |
| P8 | The deterministic baseline is unchanged | point-mass column: 8/8, 8.50 s, recovery 2.50 s |

**Fail** if any criterion fails in any run; record the run anyway with its
cause (a failed run with a recorded cause is evidence too). P7 failing alone
with everything else passing usually means the machine could not sustain the
speedup: rerun at a lower `--speedup` and say so. If the mission aborts, every
vehicle is put into BRAKE (copters) or HOLD (rovers) before the error is
printed; that is the expected abort behaviour, not a pass.

**Limits to state with the result.** SITL is firmware on a simulated
airframe, not flight. The radio, jammer and failure are the simulator's models.
Live numbers are non-deterministic and are not measured results.

### Integration notes

- `docs/trl4_validation.md` LT3 points here; the test card above is that
  procedure, and the laptop runs above are its first record.
- `VehicleTelemetry` gained `hold(mode)`; any fake or adapter passed to
  `MavlinkBackend` (e.g. `tests/test_field_node.py`) needs it.
- The evidence-log schema is version 2 (vehicle types, safe modes,
  tolerances, altitude); `live_report` refuses a log of another version.
  Headers written since also carry `task_positions` (additive; older logs
  still chart, with tasks placed where they were completed).
- Not done: free-running (non-lock-step) time; a calibrated motion model for
  the communication-only projection (still point-mass); real RF; hardware.
- README, CHANGELOG and milestones are deliberately untouched by this work;
  their summaries should be updated when this branch is integrated.

## Honest assessment

For the October hackathon, use the deterministic simulator and its trace replay
as the primary demonstration. The standalone one-vehicle SITL result supports a
model-error discussion; by itself it still does not demonstrate swarm
coordination on ArduPilot. See the [demo runbook](demo_runbook.md) for the
repeatable presentation path.

Phase 1 is worth doing regardless: it removes a duplicated motion model and
names a distinction the code was blurring.

Phase 4 closes the specific gap Phase 2/3 left open: real firmware's telemetry
has driven one agent through an actual coordination mission to completion.

Phase 5 closes the rest of the integration gap: the whole swarm on live
firmware, with a real fail-stop, flown repeatedly on two machines (Phase 5
results above). Say what it is: **SITL, not flight** — real autopilot firmware
on a simulated airframe; logical time lock-stepped to each vehicle's own clock;
the radio, the jammer and the failure are still the simulator's models, not RF
or hardware. The coordination numbers it produces are **non-deterministic** and
live here, never in [`measured_results.md`](measured_results.md). The
deterministic simulator stays the primary demonstration; the Phase 5 mission
is the evidence that the same coordination logic holds when the motion comes
from ArduPilot rather than from the point-mass model.
