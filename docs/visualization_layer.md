# Trace-driven visualization layer

The preferred visualization is a post-run Streamlit/Plotly playback dashboard.
The simulator records structured state; the viewer never scrapes console logs and
never reruns the simulation when the selected frame changes.

## Architecture

```text
deterministic Simulation
        |
        v
optional TraceRecorder snapshots after existing scheduler boundaries
        |
        v
versioned JSON trace
        |
        v
local Streamlit dashboard with coordinated Plotly panels
```

Trace capture is opt-in, so ordinary headless runs retain their previous runtime
and memory behavior. Each immutable frame contains:

- simulation time;
- physical/observer UAV state, positions, cached task intents, and direct
  neighbors;
- mutable task lifecycle projections alongside every receiver's ownership view;
- active canonical communication links and connected components;
- receiver-local `UNKNOWN` / `FRESH` / `STALE` peer knowledge, including clearly
  labelled last-known snapshots;
- cumulative mission, topology, freshness, one-hop heartbeat delivery, and
  classified protocol-gossip first-delivery, forwarding, duplicate, and
  inactive-endpoint-deferral metrics;
- structured mission, allocation, recovery, communication, and peer events.

Allocation events reuse the recorded local-utility decision metadata.
Connectivity decisions therefore expose travel distance, predicted peer degree,
and predicted isolation without recomputing a decision in the UI. The displayed
`current_task` is an execution-intent cache and `Task.assigned_agent` is an
observer projection; neither field is task authority.

## Install and run

From the repository root:

```powershell
python -m pip install -e ".[dev,visualization,dashboard]"
eudis-swarm --record-trace trace.json
eudis-swarm-dashboard trace.json
```

The dashboard also accepts a path or uploaded JSON file from its sidebar. It
starts paused at the first frame with UAV 1 selected.

Generate the communication-outage acceptance trace:

```powershell
eudis-swarm --failure-time 100 --communication-range 130 --comm-fault-agent 2 --comm-fault-start 4 --comm-fault-end 8 --peer-state-stale-after 2.5 --record-trace outage.trace.json
eudis-swarm-dashboard outage.trace.json
```

Generate the Prototype 0.3A connectivity trace:

```powershell
eudis-swarm --seed 1 --failure-time 100 --communication-range 35 --allocation-policy connectivity --record-trace connectivity.trace.json
eudis-swarm-dashboard connectivity.trace.json
```

At `t=4.50 s`, the decision panel shows UAV 2 → Task 3, distance 24.20,
predicted peer degree 1, and predicted isolation `NO`.

## Panels and controls

- The metrics bar summarizes mission, topology, failure, stale-view, delivery,
  and allocation state.
- The mission map distinguishes trajectories, dashed active links, task states,
  failed UAVs, healthy isolated UAVs, and network components.
- The UAV table separates physical state from peer-knowledge counts.
- The topology graph ignores mission geometry so partitions remain obvious.
- The decision panel explains the most relevant event at the current time.
- The selected-UAV table displays only receiver-local last-known peer snapshots.
- Playback supports play, pause, speed, frame steps, event jumps, and a slider.
- The timeline and filterable event log use structured trace events.

The legacy `--visualize` matplotlib final-frame view remains available as a
static debugging aid; it is not the preferred mission playback interface.

## Current limitations

- Playback is single-trace; side-by-side policy comparison is intentionally
  deferred, while the versioned metadata and frame model support adding it.
- UAV selection uses a reliable select box rather than custom bidirectional
  Plotly click callbacks.
- Event markers use frame/event times rather than a custom continuous animation
  engine.
- Trace size scales with frames × mission entities. This is appropriate for the
  current prototype sizes and avoids a database or streaming backend.
- The dashboard is desktop-oriented and local; mobile, live sockets, 3D, GIS,
  route planning, relay movement, and new swarm behaviours are outside scope.
  Deterministic immutable-evidence flooding exists below the UI; its transport
  receipts and counters remain observer data and are not fed back into decisions.
