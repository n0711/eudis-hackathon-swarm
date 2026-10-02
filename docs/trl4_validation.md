# TRL4 validation plan and evidence

This page decides one question: **what may this project honestly claim about
technology readiness, and what laboratory evidence backs it?** It is written so
that a judge can check every claim against a command and a recorded result, and
so that the team can run the laboratory tests on equipment day without
improvising.

## The scale used

The EU technology readiness scale, as defined in the general annexes of the EU
research framework programmes (the scale European defence-innovation calls use):

| TRL | Definition |
| --- | --- |
| 3 | Experimental proof of concept |
| **4** | **Technology validated in lab** |
| 5 | Technology validated in relevant environment |
| 6 | Technology demonstrated in relevant environment |

The step from 3 to 4 is the one this page is about. A proof of concept shows
that the idea works *inside a model the team wrote*. Laboratory validation
shows that the key components work **together, outside that model**: against
subsystems the team did not author (a real autopilot, a real network stack, real
radios, real sensor output), in a controlled setting. TRL5 would move the same
integrated system into a relevant environment — outdoor flight, real RF
interference — which is outside the scope of this hackathon.

"Laboratory" here means: an indoor bench, ArduPilot software-in-the-loop (SITL)
vehicles running the real flight-control firmware, ordinary laptops and
Raspberry Pis running the coordination software as separate processes, and real
Wi-Fi/Ethernet links that can be cut and restored on purpose.

## System under test

The technology claimed is the **distributed coordination function**: each UAV
decides from its own delivered evidence; ownership is replicated as immutable,
leased claims; failure is declared only by quorum; conflicting ownership after
a partition is reconciled deterministically; work continues without a central
planner. It is vehicle-agnostic by construction (`VehicleBackend`).

| Component | What it does | Where |
| --- | --- | --- |
| C1 Task ownership | claims, leases (fresh/stale/expired), reconciliation, claim-before-bind | `task_claims.py`, `simulation.py` |
| C2 Failure handling | silence is not failure; quorum declarations; reversible | `peer_state.py`, `failure_manager.py` |
| C3 Communication | one-hop heartbeats, store-and-forward evidence flooding, partitions | `messaging.py`, `communication.py` |
| C4 Vehicle integration | the physical movement boundary and its MAVLink adapter | `vehicle_backend.py`, `mavlink_backend.py`, `sitl.py` |
| C5 ISR task discovery | observations create bounded inspection tasks; descriptions are shared | `task_discovery.py`, `observation_*.py` |
| C6 Operator intent | versioned mission contract; continue/hold/abort under degradation | `mission_contract.py`, `autonomy.py` |

## Where each component stands

Status as of the commit this page ships with. "Evidence" links to something a
reader can run. Update the right-hand column only from a recorded result in the
[evidence log](#evidence-log) below.

| Component | Evidence today | TRL today | Laboratory test that raises it |
| --- | --- | --- | --- |
| C1 Task ownership | deterministic simulation, exact-pinned tests incl. chain/cyclic/clique topologies and negative assertions; 440-run and 960-run paired studies against a central baseline ([`measured_results.md`](measured_results.md)) | 3 | LT2/LT3 (inside a mission flown by real firmware), LT4/LT5 (as separate processes over a real network) |
| C2 Failure handling | as C1; quorum floor demonstrated ([README](../README.md)) | 3 | LT3 (physical fail-stop of a SITL vehicle), LT5 (a host physically switched off) |
| C3 Communication | synthetic radio model, jammer, blocked links | 3 | LT5 (real Wi-Fi partition and reconnection), LT6 (measured delivery ratio) |
| C4 Vehicle integration | one ArduCopter SITL vehicle flown standalone with measured model error ([`sitl.md`](sitl.md)); **LT2 passed**: one SITL copter flew inside a distributed coordination mission, 2/2 tasks, with a measured, not-yet-fixed 1.6× vehicle-vs-logical clock drift | 4 for one integrated vehicle | LT3 (all vehicles live, with fail-stop) |
| C5 ISR task discovery | synthetic publications; paired relay-recovery demonstration ([`observation_integration.md`](observation_integration.md)) | 3 | LT7 (real perception output) |
| C6 Operator intent | deterministic simulation and tests | 3 | not targeted for this event |

**Honest summary today:** strong TRL3 for the coordination function; TRL4 for
vehicle integration with one live SITL vehicle inside a coordination mission
(LT2). LT3 and LT5 are what turn that into a TRL4 claim for the coordination
function itself.

## When the TRL4 claim is justified

Claim **"TRL4 — distributed swarm coordination validated in a laboratory
environment"** only when all three of these have a recorded PASS in the
evidence log, on the same commit:

1. **LT1** — the deterministic baseline still reproduces exactly (no regression).
2. **LT3** — a coordination mission flown by several ArduPilot SITL vehicles
   completes, a physically failed vehicle is put into a safe hold, and its work
   is recovered through lease expiry by the others.
3. **LT5** — the coordination protocol, running as separate processes on at
   least three networked hosts, is partitioned by cutting real links and
   converges on one owner per task after reconnection, with the losing claimant
   standing down.

LT2 and LT4 are stepping stones toward LT3 and LT5. LT6 and LT7 strengthen
C3 and C5 but are not required for the claim. If LT3 passes but LT5 does not
(or the reverse), claim TRL4 only for the part that passed and say which.

Always state the limits with the claim: SITL is firmware on a simulated
airframe, not flight; a lab Wi-Fi partition is not contested RF; no result here
measures flight safety, endurance, or detection accuracy.

## Laboratory test cards

Each card is self-contained. Record every run in the [evidence log](#evidence-log),
including failures — a failed run with a recorded cause is evidence too.

### LT1 — deterministic regression baseline

- **Purpose:** prove the simulator still reproduces its published numbers on
  the commit under test.
- **Equipment:** any laptop.
- **Procedure:**
  ```bash
  python -m pytest --cov=eudis_swarm
  eudis-swarm --scenario demo-quorum-floor
  eudis-swarm --scenario baseline-quorum-floor   # exit code 1 is expected
  ```
- **Pass:** all tests pass with coverage at or above 85%; the first scenario
  prints `Tasks completed: 3 / 3` and `Mission completed at t=10.75s`; the
  second prints 2/3 and exits 1.
- **Record:** commit SHA, test count, coverage, both summaries.

### LT2 — one live vehicle inside a coordination mission

- **Purpose:** first integration of C1 with real flight-control firmware: one
  agent's position comes from ArduCopter SITL telemetry, the rest are
  point-mass, and the claim/lease/completion protocol runs unchanged.
- **Equipment:** the WSL2 laptop with the ArduPilot checkout ([`sitl.md`](sitl.md),
  "Environment on this machine").
- **Procedure:**
  ```bash
  ./scripts/sitl-copter.sh                                  # in WSL
  python scripts/sitl_mission_demo.py tcp:<wsl-ip>:5760      # on Windows, in .venv
  ```
- **Pass:** the script completes all tasks of its two-UAV mission, reports agent
  1's final position from live telemetry, and raises no `TimeoutError`.
- **Record:** console output, ArduPilot version, `--speedup` used, wall-clock
  duration.

### LT3 — multi-vehicle SITL mission with a physical fail-stop

- **Purpose:** C1 + C2 + C4 integrated: every agent on live firmware, one
  vehicle physically failed mid-mission and put into a safe hold, its work
  recovered by the rest through lease expiry.
- **Equipment:** as LT2.
- **Procedure and pass criteria:** the multi-vehicle test card in
  [`sitl.md`](sitl.md) (launcher, mission script, report).
- **Record:** the JSONL evidence log, the side-by-side table against the
  deterministic backend, the vehicle that was failed and its observed hold.

### LT4 — the protocol as separate processes on one host

- **Purpose:** C1 + C3 out of the single-process simulator: one OS process per
  UAV, real UDP sockets on loopback, a scripted 2+2 partition and reconnection.
- **Equipment:** any laptop.
- **Procedure and pass criteria:** the localhost test card in
  [`field_node.md`](field_node.md).
- **Record:** node logs, the observer report and its exit code.

### LT5 — the protocol on networked hosts with a real partition

- **Purpose:** the decisive laboratory test for C1–C3: at least three hosts
  (Raspberry Pis and/or laptops) on a real Wi-Fi or Ethernet network, a real
  partition (a host's Wi-Fi switched off, or hosts moved out of range, or a
  switch port unplugged) and reconnection, and optionally a host powered off as
  a physical fail-stop.
- **Equipment:** three or more hosts with Python 3.11+, one shared network.
- **Procedure and pass criteria:** the multi-host test card in
  [`field_node.md`](field_node.md).
- **Record:** which host was cut and when (wall clock, by a second observer),
  every node log, the observer report, a photo or video of the bench.

### LT6 — measured link delivery

- **Purpose:** replace an assumption with a measurement for C3: packet delivery
  ratio and latency between two hosts at several separations or obstructions.
- **Procedure:** the link-probe section of [`field_node.md`](field_node.md).
- **Record:** the CSV per position, the summary table. Claim only the measured
  environment; this is not a calibration of the simulator's radio model unless
  the comparison is written up.

### LT7 — real perception output into task discovery (optional)

- **Purpose:** C5 with a non-synthetic producer: the perception application
  exports version-1 JSONL ([`observation_integration.md`](observation_integration.md))
  with explicit mission coordinates.
- **Procedure:** validate the export with
  `eudis-swarm-observation-replay <file> --at <t>`, convert it to a publication
  log, and run `python -m eudis_swarm.observation_demo <log> --output out.json`.
- **Pass:** every record validates; the inspection objective is discovered and
  completed.
- **Record:** producer software and version, hardware, the export, the output.
  Do not claim georeferencing that the producer did not actually perform.

## Evidence log

One row per run. Fill it on the day; never back-fill a result that was not
observed.

| ID | Date | Commit | Hardware / environment | Command | Result | Artefacts | PASS / FAIL |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LT1 | | | | | | | |
| LT2 | 2026-10-02 | `7c8a0fb` | WSL2 Ubuntu on the team laptop; ArduCopter SITL `ArduPilot-4.6.0-beta1-7231-g7d2d454eba`, quad, `--speedup 5`, fresh start | `python scripts/sitl_mission_demo.py tcp:127.0.0.1:5760` | 2/2 tasks, 4.50 logical s, 12 s wall incl. arm and takeoff; agent 1 final position from live telemetry. Limit measured on the same commit: vehicle clock ran 1.6× logical time (7.20 s vs 4.50 s over 18 steps), because 5 Hz telemetry quantises each 0.25 s lock-step to 0.40 s | [`evidence/20261002T040554Z-LT2`](../evidence/20261002T040554Z-LT2) | PASS |
| LT3 | | | | | | | |
| LT4 | | | | | | | |
| LT5 | | | | | | | |
| LT6 | | | | | | | |
| LT7 | | | | | | | |

## What to say if asked

- **"Is this TRL4?"** — Point to this page's claim criteria and the evidence log.
  If LT3 and LT5 passed: yes, for the coordination function, validated in a
  lab with real firmware and a real network. If not: TRL3 for coordination,
  with the specific lab tests run so far.
- **"Has it flown?"** — Only in SITL: real ArduPilot firmware on a simulated
  airframe. No physical flight has been performed.
- **"What happens under real jamming?"** — Not measured. The lab partition is a
  clean cut; contested RF is a TRL5 question.
