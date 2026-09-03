# Prototype 0.1 technical design

> Historical scope: this document records the original centralized physical
> failure/recovery baseline and its acceptance results. The normal mission now
> uses receiver-local utility, replicated task claims, and `owns_task()` as
> execution authority. The 0.1 allocator remains a comparison baseline; see
> [the distributed-state foundation](distributed_state_foundation.md).

## Scope and acceptance criterion

Prototype 0.1 is the minimum executable resilience proof for the EUDIS Swarm
project:

```text
agent failure
    -> heartbeat timeout
    -> failed-agent declaration
    -> orphaned-task release
    -> healthy-agent reassignment
    -> mission completion
```

The acceptance scenario uses four agents, a two-dimensional mission area,
multiple exploration tasks, automatic task allocation, movement, periodic state
exchange, one deterministic failure, and zero intervention after mission start.
Success means that the failed UAV remains failed and taskless, its unfinished
task is eventually completed by another UAV, and every mission task reaches
`COMPLETED`.

This is a logical simulation, not an aircraft controller or communications
system. Its purpose is to make the resilience transition explicit, repeatable,
measurable, and testable before introducing robotics or networking complexity.

## Component boundaries

### `config.py`

`SimulationConfig` is an immutable, validated collection of simulation
parameters. It prevents invalid counts, non-finite positions/times, non-positive
steps and speeds, a timeout shorter than the heartbeat interval, and a failure
agent outside the configured ID range.

### `agent.py`

`Agent` contains its ID, 2D position, constant speed, coordinator-visible status,
current task, last heartbeat time, and physical responsiveness flag. Its states
are:

- `IDLE`: healthy and currently available;
- `ACTIVE`: healthy from the coordinator's perspective and owns one task; and
- `FAILED`: declared failed and permanently unavailable.

Failure injection first sets `responsive=False` without prematurely changing the
coordinator-visible status. The agent then stops both movement and heartbeat
generation. This distinction allows timeout detection, rather than the injector,
to drive the `FAILED` transition.

`Heartbeat` is an immutable snapshot of agent ID, position, status, current task,
and simulated timestamp.

### `task.py`

`Task` contains an ID, 2D target position, status, and optional owner. Its states
are `UNASSIGNED`, `ASSIGNED`, and `COMPLETED`. State methods reject invalid
assignment, release, and completion transitions.

### `task_allocator.py`

`TaskAllocator` is a stateless policy. For every currently available agent `i`
and unassigned task `j`, it considers:

```text
C(i, j) = EuclideanDistance(agent_i.position, task_j.position)
```

It repeatedly selects the globally smallest candidate pair, removes that agent
and task from the current allocation batch, and continues until one candidate
set is empty. Numeric agent ID and task ID provide deterministic tie-breaking.
The allocator returns proposals and does not mutate agents or tasks.

This is intentionally a greedy baseline rather than global route optimization.
The narrow policy interface is the extension point for later
communications-aware or QUBO allocation.

### `failure_manager.py`

`FailureManager` stores the latest monotonic heartbeat per agent. At a simulation
timestamp it detects an agent only when:

```text
current_time - last_heartbeat_time > heartbeat_timeout
```

The comparison is strict: equality does not declare a failure. Each agent is
emitted as timed out at most once. `FailureManager` reports the observation but
does not own agent/task transitions.

### `mission.py`

`Mission` is the authority for transitions spanning agents and tasks. It:

- starts the mission and initial heartbeat/allocation exchange;
- applies allocator proposals atomically to both sides of ownership;
- records event objects and concise logs;
- declares timed-out agents failed;
- releases unfinished work held by a newly failed agent;
- invokes allocation after recovery and applies normal scheduling proposals;
- completes tasks and frees healthy agents; and
- validates ownership invariants throughout the run.

Important invariants include:

- a failed UAV owns no task;
- an active UAV owns exactly one assigned task;
- no two UAVs own the same task;
- an unassigned task has no owner; and
- each assigned task and its owning agent reference one another.

### `simulation.py`

`Simulation` owns scenario construction and mechanics: seeded task generation,
initial UAV placement, logical-clock ticks, scheduled failure injection,
heartbeat scheduling, movement, task-arrival detection, position history, and
the mission time limit. A `Simulation` instance may be run only once.

At each scheduled event boundary, the run loop performs these operations in
order:

1. advance active responsive agents from the previous boundary;
2. inject the configured failure when its exact time is reached;
3. exchange heartbeats when an exact interval is due;
4. detect and recover heartbeat timeouts;
5. complete healthy arrivals within the configured tolerance;
6. offer idle agents any unassigned work;
7. record positions and assert mission consistency; and
8. finish successfully or time out at the configured upper bound.

Regular movement boundaries are no farther apart than `time_step`. Heartbeat
and failure boundaries are inserted at their exact configured times, so a
coarse movement step cannot skip several healthy heartbeats. A final partial
interval is included when the mission time limit is not divisible by the time
step.

Injection precedes heartbeat exchange. Therefore, when failure time falls on a
heartbeat tick, the injected UAV does not send a heartbeat at that timestamp.

### `metrics.py`

`SimulationMetrics` derives results from actual mission transitions. It records:

- logical simulation duration and completion status;
- completed and total tasks;
- started and detected-failed agent counts;
- injected/detected timestamps and failure position;
- orphaned and successfully reassigned task counts;
- orphaned tasks actually completed after reassignment (recovered tasks);
- failure detection latency (`detected_at - injected_at`);
- heartbeat staleness at detection (`detected_at - last_heartbeat`);
- task reassignment latency (`reassigned_at - orphaned_at`); and
- human interventions, initialized to zero for this autonomous run.

The terminal summary reports averages when multiple records exist, even though
the default scenario injects one failure. All times are logical simulated time.

### `visualization.py`

The optional renderer consumes the completed `SimulationResult`. It plots each
recorded UAV path, completed/incomplete tasks, healthy final positions, and the
failed UAV. Importing or running the core does not import matplotlib.

## Default configuration

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `agent_count` | 4 | Number of simulated UAVs |
| `task_count` | 20 | Number of point exploration tasks |
| `random_seed` | 2026 | Seed used only by local scenario generation |
| `area_width` | 100.0 | Mission-area x extent |
| `area_height` | 100.0 | Mission-area y extent |
| `agent_speed` | 10.0 | Point-mass distance units per simulated second |
| `time_step` | 0.25 s | Logical simulation tick |
| `completion_tolerance` | 0.75 | Maximum remaining distance for completion |
| `heartbeat_interval` | 1.0 s | Healthy heartbeat period |
| `failure_timeout` | 2.5 s | Strict heartbeat-age threshold |
| `failure_agent_id` | 2 | Agent selected for injection |
| `failure_time` | 4.0 s | Scheduled injection time |
| `max_simulation_time` | 300.0 s | Mission time limit |

Initial positions use the corners of the mission area with a five-percent
margin. Task positions come from a private `random.Random(random_seed)` instance
inside a ten-percent boundary margin, so the scenario does not depend on global
random state.

The CLI exposes the parameters most useful for demonstrations:

| Argument | Default | Notes |
| --- | ---: | --- |
| `--agents` | 4 | Positive integer |
| `--tasks` | 20 | Positive integer |
| `--seed` | 2026 | Integer random seed |
| `--failure-agent` | 2 | Must identify a configured agent |
| `--failure-time` | 4.0 | Non-negative simulated seconds |
| `--failure-timeout` | 2.5 | Must be at least the 1.0 s heartbeat interval |
| `--visualize` | off | Show final matplotlib view |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |

Other values can be changed through `SimulationConfig` when using the package
programmatically.

## Deterministic default timeline

The current seeded scenario follows this recovery path:

| Simulated time | Event |
| ---: | --- |
| 0.00 s | Mission starts; all four agents heartbeat and receive initial tasks |
| 0.00 s | UAV 2 receives Task 19 |
| 3.00 s | UAV 2 sends its last heartbeat |
| 4.00 s | Failure is injected; UAV 2 becomes silent and immobile |
| 5.50 s | Heartbeat age equals the 2.5 s timeout; strict comparison does not fire |
| 5.75 s | Heartbeat age is 2.75 s; UAV 2 is declared failed and Task 19 is released |
| 10.75 s | UAV 1 becomes available and receives orphaned Task 19 |
| 17.25 s | UAV 1 completes Task 19; all 20 tasks are complete |

Reassignment is not forced onto a busy UAV. Recovery invokes the allocator
immediately at 5.75 s, but every healthy UAV already owns a task, so Task 19
remains safely `UNASSIGNED` until capacity is available. This yields a 5.00 s
task-reassignment latency and does not prevent mission completion.

## Install and execute

Python 3.11 or newer is required. From the repository root:

```console
python -m venv .venv
```

Activate the environment on PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on a POSIX shell:

```bash
source .venv/bin/activate
```

Install the package and tests:

```console
python -m pip install -e ".[dev]"
```

Run Prototype 0.1:

```console
python -m eudis_swarm.simulation
```

Equivalent installed command:

```console
eudis-swarm
```

Run with custom demonstration parameters:

```console
python -m eudis_swarm.simulation --agents 6 --tasks 30 --seed 7 --failure-agent 4 --failure-time 5 --failure-timeout 3
```

Install and request the optional final plot:

```console
python -m pip install -e ".[visualization]"
python -m eudis_swarm.simulation --visualize
```

For a CI/headless run, omit `--visualize`. On success the command returns exit
status 0; a mission time-out returns 1.

## Validation

Install the development extra, then run all automated tests:

```console
python -m pytest
```

Verbose test names are useful when presenting the acceptance evidence:

```console
python -m pytest -v
```

The test modules cover the required behaviors:

- `tests/test_task_allocator.py` verifies unique ownership, exclusion of a
  declared-failed UAV, no over-allocation, and deterministic ID tie-breaking.
- `tests/test_failure_detection.py` verifies that equality with the threshold
  does not fail an agent, a strictly stale heartbeat does, the unfinished task
  is released, and the same failure is not emitted again.
- `tests/test_mission_recovery.py` runs a smaller deterministic end-to-end
  scenario. It verifies failure injection and detection timestamps, the stopped
  UAV's frozen position, orphan release, reassignment to a different healthy
  agent, completion of the recovered task, all-task mission completion, zero
  interventions, and the required event order.

Then run the complete default demonstration separately:

```console
python -m eudis_swarm.simulation
```

The acceptance evidence in its log and final summary is:

- `[FAULT]` appears for UAV 2;
- `[HEARTBEAT]` and `[FAILURE]` show timeout-based detection;
- `[RECOVERY]` releases UAV 2's unfinished Task 19;
- `[ALLOC] Task 19 reassigned` names a healthy replacement;
- `[MISSION] Mission completed` appears; and
- the summary reports 20/20 completed, one failed agent, one orphan, one
  recovered task, and zero human interventions.

Do not substitute wall-clock duration for the simulation duration in the
summary. Exact output remains configuration-dependent; the values above describe
the documented defaults.

## Known limitations and non-goals

Prototype 0.1 intentionally omits:

- ROS 2, ArduPilot, PX4, MAVLink, Gazebo, AirSim, and real UAV dynamics;
- sockets, networking middleware, changing topology, RF/link models, delay,
  jitter, congestion, packet loss, partitions, and distributed clocks;
- collision avoidance, vehicle envelopes, energy/range constraints, terrain,
  weather, geodetic coordinates, and no-fly zones;
- sensors, computer vision, SLAM, GNSS-denied navigation, and relay behavior;
- quantum optimization, reinforcement learning, and global route optimization;
- persistence, databases, web interfaces, authentication, and operator control;
- recovery/rejoin of a failed agent, intermittent or Byzantine faults, and
  deliberate simultaneous multi-agent failure scenarios.

The coordinator and every simulated agent share memory and a perfect logical
clock. A central process is therefore a single point of failure. The heartbeat
model validates timeout-driven control flow, not communications performance.

The greedy allocator minimizes one pair at a time; it does not minimize total
mission travel. Availability also means one active task per agent, so queued work
is represented by unassigned tasks rather than local agent plans.

No operational safety, flight-readiness, or real-time guarantees are provided.

## Planned evolution

### Prototype 0.2 — communications graph

Represent agents and links explicitly, then inject deterministic link
degradation and topology changes. Preserve the Prototype 0.1 failure/recovery
tests as regression coverage.

### Prototype 0.3 — communications-aware behavior

Use reachability and link quality as inputs to task allocation and swarm
behavior, with measurable comparison against the distance-only baseline.

### Prototype 0.4 — QUBO / quantum-simulated optimization

Formulate selected allocation decisions as QUBO experiments and compare quality,
runtime, and robustness with classical baselines. This is an optimization study,
not a dependency of the resilience core.

### Prototype 0.5 — ROS 2 distribution

Move state exchange and coordination behind ROS 2 interfaces, define message and
node boundaries, and repeat the failure tests under distributed execution.

### Prototype 0.6 — ArduPilot multi-UAV SITL

Connect the distributed implementation to multiple software-in-the-loop
vehicles, retaining safety boundaries between mission decisions and autopilot
control.

### Prototype 1.0 — EUDIS demonstration baseline

Integrate the proven stages into a measurable, repeatable demonstration with
documented scenarios, degradation modes, and evidence collection.

These stages are documentation only. None is implemented by Prototype 0.1.
