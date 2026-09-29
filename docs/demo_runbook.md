# Hackathon demonstration runbook

This is the repeatable presentation path for the current EUDIS simulator. It demonstrates how locally owned task claims recover work after a physical UAV failure that the swarm cannot declare. The centralized comparison and distributed run use the same named scenario except for task-control mode. All times are logical simulation seconds.

## Prepare

Use Python 3.11 or newer. On Windows PowerShell from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,dashboard]"
.\.venv\Scripts\python.exe -m pytest --cov=eudis_swarm --cov-report=term-missing
```

On Linux or macOS, create the virtual environment with `python3.11 -m venv .venv`, then use `.venv/bin/python` and `.venv/bin/eudis-swarm`. Install and test before the event; keep a copy of the resulting traces for offline replay.

## Two-minute live sequence

1. State the problem: a quiet UAV may be physically stopped or simply unreachable. A quorum prevents one witness from declaring it failed.
2. Run the distributed case:

   ```powershell
   .\.venv\Scripts\eudis-swarm.exe --scenario demo-quorum-floor --record-trace distributed.trace.json
   ```

   Check for 3/3 completed tasks, no declaration, one undetected physical loss, and completion at logical time 10.75 s in the repository's pinned scenario.

3. Run the central comparison:

   ```powershell
   .\.venv\Scripts\eudis-swarm.exe --scenario baseline-quorum-floor --record-trace centralized.trace.json
   ```

   Exit code 1 is expected for this deliberately incomplete scenario. Check for 2/3 tasks and no declaration; keep the trace it writes.

4. Open the recorded dashboard:

   ```powershell
   .\.venv\Scripts\eudis-swarm-dashboard.exe distributed.trace.json
   ```

   Choose **Belief comparison** and load `centralized.trace.json`. Point to the local ownership view and evidence-age labels. The display is post-run observer playback, not a live GCS.

5. State the result and limit together: the distributed mode recovers through claim-lease expiry without a failure declaration in this deterministic scenario. This does not measure flight time, realistic RF, detection accuracy, or physical UAV safety.

For a console-only fallback on Windows, run `.\scripts\pitch-demo.ps1`. If live generation fails, show the two traces created during rehearsal and disclose that playback is prerecorded. Do not invent a fresh run result.

## Before presenting

- Verify the installed command and the two trace files on the presentation machine.
- Keep the raw result text and a short screen recording as a fallback.
- Check the dashboard comparison labels: a stopped aircraft may retain a self-claim, so claim overlap does not automatically mean two aircraft flew the same task.
- Keep the [measured-results limits](measured_results.md), [fleet comparison](generalization_study.md), and [layout/fault-time sensitivity](robustness_study.md) accessible. The latter records cases where the mission finished before a scheduled fault was injected.

## Implemented and planned

Implemented: deterministic 2-D point-mass simulation, receiver-local task claims, modeled communication faults and failures, central comparison, recorded traces, and one standalone ArduPilot SITL vehicle experiment.

Implemented separately: synthetic external ISR observation JSONL replay with receiver-local freshness checks. It is not connected to simulator task discovery or this coordination demonstration.

Planned: live perception input, exploration mapping, relay movement, live swarm SITL integration, physical flight, and calibrated radio or vehicle models. The standalone SITL experiment is not the coordination demonstration.
