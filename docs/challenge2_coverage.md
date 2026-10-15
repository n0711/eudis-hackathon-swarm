# Challenge 2 coverage

What the challenge asks for, what this repository does about each point, the
evidence a reader can check, and what is missing. Statuses change only when a
test or a recorded run changes them.

## The requirement

EUDIS Defence Hackathon 2026, Autumn edition, Challenge 2 —
[official text](https://eudis-hackathon.eu/challenges/):

> Develop a multi-agent drone or robot swarm that can collaborate to complete a
> mission (e.g. search and rescue, mapping or target tracking) with minimal
> human control and include optimisation using quantum simulation.

The Cyprus edition (Nicosia, 15–17 October 2026) adds:

> Build the coordination intelligence that turns several drones or robots into
> one effective mission system. The technical challenge spans resilient
> communication between agents, distributed task allocation, coordinated motion
> and deconfliction, shared situational awareness, and mission behaviours
> including coordinated search and target hand-off. The best solutions treat the
> swarm as vehicle-agnostic and give a single operator command of many vehicles
> by intent, rather than piloting each one.

and states two framing rules: the Cyprus edition targets **TRL4+** teams, and
"**we evaluate the module, not the platform**" — a module may be tested by
integrated flight, on a partner-provided platform, or in data-in-the-loop and
simulation.

The module this repository offers is the **coordination software**: it runs on
any vehicle that accepts position setpoints over MAVLink, and is demonstrated in
a deterministic simulator, against ArduPilot SITL firmware, and as separate
processes over a real network.

## Coverage

| Requirement | What exists | Evidence | Status |
| --- | --- | --- | --- |
| Multi-agent swarm completes a mission | N UAVs share a task catalogue; every task is claimed, executed and completed without a central planner | headline scenarios and 440-run study ([`measured_results.md`](measured_results.md)); LT2 live SITL mission ([`trl4_validation.md`](trl4_validation.md)) | done in simulation; one live SITL vehicle flown; all-live multi-vehicle mission in progress |
| Minimal human control | the operator issues a versioned `MissionContract` (priorities, minimum participants, continue/hold/abort under degradation) and is not needed afterwards; recovery after a lost UAV needs no human action | [`milestones.md`](milestones.md) milestone 5; quorum-floor demo: recovery without any declaration | done |
| Optimisation using quantum simulation | receiver-local task-assignment optimisation formulated as a QUBO and solved by simulated quantum algorithms (statevector QAOA with a constraint-preserving ansatz, simulated quantum annealing), benchmarked against exact and greedy baselines | [`quantum_optimisation.md`](quantum_optimisation.md): 480 paired runs — constraint-preserving QAOA with 64-shot sampling returned the optimum in 915 of 923 solves (14× random-sampling probability at 12 qubits); mission time against distance not significantly changed (33 faster, 27 slower); about 1.4 % less flight distance and less contested time under jamming | done as classical simulation; no quantum speedup claimed, no hardware run, one-step objective |
| Resilient communication | heartbeats one-hop; protocol evidence flooded store-and-forward across the connected component; partitions, blocked links, jammer; silence is never treated as failure | simulator tests; preliminary LT4 runs of the field-node work (separate processes, real UDP sockets on one laptop, not yet merged): 10/10 partition runs converge after reconnection in 7–16 ms, 5/5 with 20% packet loss | done in simulation and on localhost; multi-host LAN test (LT5) pending hardware |
| Distributed task allocation | receiver-local utility → claim before binding → leases → deterministic reconciliation; centralized allocator kept only as a comparison baseline | 440-run study: central control failed 68 missions, distributed none; preliminary LT4 runs: a real split brain on every partition run, resolved after heal with the loser standing down | done |
| Coordinated motion and deconfliction | none in the 2-D simulator; altitude layering per vehicle plus a measured minimum separation in the multi-vehicle SITL mission | multi-vehicle SITL test card (LT3) | **gap in simulation**; in progress for SITL |
| Shared situational awareness | each UAV keeps its own evidence-based view: peer freshness and status from delivered heartbeats; ISR observations; accepted inspection objectives shared by description gossip. There is deliberately no single shared map — awareness is per receiver | [`distributed_state_foundation.md`](distributed_state_foundation.md); [`observation_integration.md`](observation_integration.md) | done, per-receiver by design |
| Coordinated search | the area is cut into sectors that are allocated, leased and reconciled by the existing protocol; coverage over time is measured by the observer | [`search_mission.md`](search_mission.md): 64 paired runs — under jamming the distributed search covered every sector in 16/16 runs, the central coordinator in 14/16; with clean links the central coordinator is 0.8 s faster | done for sector coverage; no sensor or detection-probability model |
| Target hand-off | an accepted inspection objective survives the loss of the UAV that observed it and is completed by another UAV that never saw the target | relay-recovery scenario: 3/3 with description sharing vs 2/3 without ([`demo_runbook.md`](demo_runbook.md)) | done (synthetic observations) |
| Vehicle-agnostic | coordination never imports vehicle code; motion goes through `VehicleBackend`; `MavlinkBackend` drives any ArduPilot vehicle in GUIDED | LT2: ArduCopter SITL in a coordination mission; ArduRover SITL follows the same setpoints, settle 0.15–0.81 m ([`data/sitl/rover_settle_2026-10-02.txt`](data/sitl/rover_settle_2026-10-02.txt)) | done for ArduPilot; mixed copter + rover mission in progress; PX4 not supported |
| Single operator, command by intent | the contract is intent (what matters, what floor, what to do when degraded), never a per-vehicle plan; it propagates as evidence, so a partition splits intent exactly as it splits everything else | `mission_contract.py`, milestone 5 | done; there is no operator user interface |
| TRL4+ | per-component readiness, lab test cards and an evidence log | [`trl4_validation.md`](trl4_validation.md) | LT1 and LT2 passed; LT3/LT4 in progress; LT5 needs hardware |

## Gaps, stated plainly

- **Coordinated search** is sector coverage: a sector counts as searched when
  its centre is reached. There is no sensor footprint or probability of
  detection, and detections during a search do not yet create inspection tasks
  (hand-off is demonstrated separately).
- **Deconfliction** exists only as altitude layering in the live SITL mission;
  the simulator's point-mass vehicles can share airspace.
- **PX4** vehicles use OFFBOARD rather than GUIDED and are not supported by the
  adapter.
- **Physical flight** has not been performed. SITL runs real firmware on a
  simulated airframe.

## For registration and the airfield day

The registration form asks how each team will test. For this module:

- **Testing mode:** simulation and data-in-the-loop for the coordination layer;
  software-in-the-loop with ArduPilot; and, if offered, running the
  coordination software against a partner-provided **ArduPilot** multirotor (or
  ground robot) over a MAVLink telemetry link or companion computer, with the
  partner's pilot holding RC override.
- **Resources to request:** one or more ArduPilot vehicles in GUIDED mode
  reachable over MAVLink (TCP/UDP), their home positions, and a laptop-to-
  vehicle telemetry link. PX4-only platforms would need an OFFBOARD adapter
  first.
- **Safety:** every live command path keeps the vehicle's own failsafes and the
  pilot's RC override in charge. Putting a physically failed vehicle into the
  autopilot's own hold mode, rather than leaving it on its last setpoint, is
  part of the multi-vehicle SITL work still in progress; until it lands, a
  live run must not rely on it.
