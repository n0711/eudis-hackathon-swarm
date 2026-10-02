# Hackathon demonstration runbook

This is the repeatable presentation path for the current EUDIS simulator. It demonstrates how locally owned task claims recover work after a physical UAV failure that the swarm cannot declare, how replicas that disagree during a partition converge afterwards, and how an accepted ISR inspection objective survives the loss of the UAV that saw it. The centralized comparison and distributed run use the same named scenario except for task-control mode. All times are logical simulation seconds.

For the laboratory evidence behind a TRL4 claim — live autopilot, real network, real partitions — follow [`trl4_validation.md`](trl4_validation.md). This runbook is the short, deterministic presentation path; the lab tests are the supporting evidence.

## Prepare

Use Python 3.11 or newer. On Windows PowerShell from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest --cov=eudis_swarm --cov-report=term-missing
```

On Linux or macOS, create the virtual environment with `python3.11 -m venv .venv`, then use `.venv/bin/python` and `.venv/bin/eudis-swarm`. Install and test before the event; keep the console output of a rehearsal run as a fallback.

## Two-minute live sequence

1. State the problem: a quiet UAV may be physically stopped or simply unreachable. A quorum prevents one witness from declaring it failed.
2. Run the distributed case:

   ```powershell
   .\.venv\Scripts\eudis-swarm.exe --scenario demo-quorum-floor
   ```

   Check for 3/3 completed tasks, no declaration, one undetected physical loss, and completion at logical time 10.75 s in the repository's pinned scenario.

3. Run the central comparison:

   ```powershell
   .\.venv\Scripts\eudis-swarm.exe --scenario baseline-quorum-floor
   ```

   Exit code 1 is expected for this deliberately incomplete scenario. Check for 2/3 tasks and no declaration.

4. Show temporary disagreement and convergence:

   ```powershell
   .\.venv\Scripts\python.exe -m eudis_swarm.task_claim_demo
   ```

   The single output line names the 2+2 partition, the task both sides claimed, the deterministic winner, and the losing UAV that released it and claimed other work.

5. Show that accepted ISR work survives its source:

   ```powershell
   .\.venv\Scripts\python.exe -m eudis_swarm.observation_demo examples\isr_recovery_publications.jsonl --scenario relay-recovery --output shared.json
   .\.venv\Scripts\python.exe -m eudis_swarm.observation_demo examples\isr_recovery_publications.jsonl --scenario relay-recovery --no-task-sharing --output unshared.json
   ```

   With description sharing, 3/3 objectives complete by 20.0 s although the observing UAV fails at 1.0 s; without it, 2/3 by the 30.0 s deadline (`completed_tasks`, `total_tasks` and `duration_s` in each JSON file). The observations are synthetic; see [`observation_integration.md`](observation_integration.md).

6. State the result and limit together: the distributed mode recovers through claim-lease expiry without a failure declaration in these deterministic scenarios. They do not measure flight time, realistic RF, detection accuracy, or physical UAV safety — the lab evidence in [`trl4_validation.md`](trl4_validation.md) addresses what has been run against a real autopilot and a real network, and says what has not.

For a console-only fallback on Windows, run `.\scripts\pitch-demo.ps1`. If live generation fails, show the rehearsal output and disclose that it is prerecorded. Do not invent a fresh run result.

## Before presenting

- Verify the installed commands on the presentation machine and run the full sequence once.
- Keep the raw result text and a short screen recording as a fallback.
- A stopped aircraft may retain a self-claim, so claim overlap does not automatically mean two aircraft flew the same task.
- Keep the [measured-results limits](measured_results.md), [fleet comparison](generalization_study.md), and [layout/fault-time sensitivity](robustness_study.md) accessible. The latter records cases where the mission finished before a scheduled fault was injected.

## Implemented and planned

Implemented: deterministic 2-D point-mass simulation, receiver-local task claims, modeled communication faults and failures, central comparison, recorded traces, synthetic ISR publications that create bounded inspection tasks with description sharing, one standalone ArduPilot SITL vehicle experiment, and a `MavlinkBackend` adapter that puts SITL vehicles inside a coordination mission.

Status of the laboratory evidence (live SITL coordination missions, the protocol over real UDP between processes and hosts, link measurements) is kept in one place: [`trl4_validation.md`](trl4_validation.md).

Planned: live perception input, exploration mapping, relay movement, physical flight, and calibrated radio or vehicle models.
