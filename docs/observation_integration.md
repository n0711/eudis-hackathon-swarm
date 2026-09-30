# External ISR observation replay boundary

This document specifies the boundary between an independent aerial-video perception application and EUDIS. The repository contains a standalone receiver-local replay adapter and an opt-in scheduled publication path into `Simulation`. Explicit georeferenced observations can make stationary inspection tasks eligible for individual receivers. It does not import VERA-MOT. No perception result, Raspberry Pi throughput, cross-UAV identity association, or live swarm integration is claimed here.

## Producer and consumer

Christoforos's VERA-MOT work is an external perception project. A separate EUDIS application may read video frames and emit local tracks. `observation_replay.py` validates externally supplied records and stores each delivered record in one receiver's private view. The replay store does not write to peer or claim stores, mark a task complete from a bounding box, or make a local track ID into a global identity. The simulator applies a separate task-discovery rule before its existing claim/arrival/completion protocol. Live streaming transport remains future work.

The supplied VERA-MOT task note calls for desktop baseline reproduction, a separated EUDIS application, measured Raspberry Pi feasibility, versioned observations, and disconnect/reconnect testing. Its Pi references alternate between Model B and Model B+; verify the physical board before describing deployment or publishing a benchmark. The current EUDIS repository contains no VERA-MOT code or checkpoint.

## Version 1 replay record

One JSON Lines record per locally tracked object:

```json
{"schema_version":1,"uav_id":"isr_01","track_id":17,"timestamp_ms":1790590000000,"class_label":"vehicle","confidence":0.87,"bbox_xyxy":[120,85,220,160],"image_size":[640,480],"track_status":"observed"}
```

`uav_id` identifies the producer; `track_id` is unique only within that producer's track lifecycle. `timestamp_ms` is the frame or observation time in Unix milliseconds, not receipt time. `confidence` is detector confidence from 0 to 1, not identity reliability. Bounding-box coordinates are pixel `[x_min,y_min,x_max,y_max]`, constrained to the stated image dimensions. `track_status` is `observed`, `temporarily_missing`, or `expired`; consumers must distinguish a current detection from a tracker prediction. A validated identity-reliability field can be added later with a separate definition.

A separate `source_status` record carries producer ID, schema version, timestamp, and status (`running`, `stalled`, or `stopped`). `empty_scene:true` explicitly clears earlier tracks for that producer; silence or a heartbeat with no tracks does not establish an empty scene. The receiver stores both source time and local receipt time, uses local receipt age for track and source timeouts, and rejects repeated or older source records without refreshing freshness. Source time is metadata and is never compared directly with simulator time.

## Run the standalone replay

The example uses two producer IDs with the same local `track_id`. Each JSONL line is a delivery envelope with `receiver_id`, receiver-local `received_at`, and `record`; `delivered:false` denotes an event that did not reach a receiver. It includes an empty scene, a source timeout, and a later source update. It is synthetic test data, not output from VERA-MOT.

```bash
eudis-swarm-observation-replay examples/isr_replay.jsonl --at 0.5
eudis-swarm-observation-replay examples/isr_replay.jsonl --at 3.5
eudis-swarm-observation-replay examples/isr_replay.jsonl --at 5.1
```

At 0.5, `uav_1` has two distinct tracks. At 3.5 both sources time out. At 5.1 the first source has explicitly reported an empty scene and the second has reconnected with a newer observation. The output is receiver-local JSON evidence; it is not a swarm task or flight instruction.

## Scheduled simulator publications and inspection tasks

Run the supplied synthetic example after installing the package:

```bash
python -m eudis_swarm.observation_demo examples/isr_publications.jsonl --output demo-output/observations.json
```

This publication log is different from the standalone replay envelopes: each line has `source_agent_id`, `published_at` (simulation seconds), and `record`. Publication times are nonnegative and rounded to the simulator clock's 12 decimal places. The simulator applies its current one-hop link graph; callers cannot set `delivered:true`. Each producer ID is bound to exactly one simulated UAV. A responsive source ingests its own sensor record even while its radio is isolated. Failed endpoints cannot publish. Drops never update a receiver store. There is no forwarding, retained backlog, or implicit retry; a producer must publish fresh status/track records after reconnection.

Optional version-1 track fields are:

```json
{"coordinate_frame":"mission_xy_m","mission_position_m":[40.0,40.0]}
```

These coordinates must be supplied independently by the producer or test fixture. They are finite local mission-plane metres, in the same frame as simulator tasks and inside the configured mission area. No pixel-to-world projection, GNSS conversion, altitude, or camera calibration is implemented. Pixels alone cannot create a task.

A receiver discovers inspection work only when its own delivered track is fresh, its delivered source status is fresh and `running`, the track status is `observed`, confidence is at least 0.6, and explicit mission coordinates are present. Confidence is a configurable heuristic, not validated detection performance. Identity is `(producer_id, track_id)`; two producers with the same track number create separate objectives. Producers must not reuse a track ID for a new lifecycle within a log.

The objective is a stationary point inspection. Coordinates for a producer/track pair must agree throughout a publication log; moving-target tracking is rejected. Discovery is idempotent. Once accepted, work persists through source timeout/empty-scene evidence and is completed only through the existing claim-authorized arrival rule. Existing work is not preempted. A timeout does not prove that a source or tracked object physically failed.

### Offline catalog boundary

This is a bounded replay prototype, not an open-ended live mission. The simulator reserves observation-derived task IDs and world positions from the supplied log at construction because the existing claim protocol requires a fixed task catalog. Receivers get the ID mapping only; the discovery module obtains coordinates from delivered records. The task-selection gate excludes every reserved objective that this receiver has not discovered. The operator/evaluator trace sees reserved world tasks before discovery; it must not be described as UAV knowledge.

`config.task_count` describes the original tasks; result `total_task_count` includes reserved observation-derived tasks. Undelivered, low-confidence, or never-eligible reserved tasks remain incomplete and can cause a mission timeout. Logs may therefore specify impossible objectives; this is reported rather than counted as success. Publications beyond the mission time limit are never delivered. No autonomous forwarding of discovered coordinates is implemented, so a peer that never received valid observation evidence cannot take over that inspection objective solely from task-claim messages.

### Synthetic example evidence

With seed 2026, the example has three UAVs, six initial tasks, and one reserved inspection task. UAV 1's radio is isolated until 2.0 seconds. It discovers task 7 locally at 0.1 seconds; UAVs 2 and 3 discover it from new publications at 2.0 seconds. All seven tasks finish at 9.25 simulated seconds. The exported JSON includes configuration, receiver discoveries, and attempted delivery receipts. These numbers are simulation results, not measured perception or flight performance. The demo uses the point-mass backend and has no flight-command path.

## Remaining integration work

1. Reproduce the VERA-MOT desktop baseline and record dependencies/checkpoint provenance.
2. Export actual timestamped logs with explicit source status and empty-scene records; validate the records against the adapter.
3. Define and validate an actual georeferencing contract before publishing mission coordinates.
4. Add distributed objective-description delivery and runtime catalog registration if open-ended live discovery is required. Preserve private receiver knowledge.
5. Measure physical Pi throughput and hardware/SITL integration separately. Do not send raw video on the simulated swarm link by default.

The supplied IN424 material is prior art for separating sensing, objective selection, planning, and control. Its merged ground-robot map architecture is not an inherited EUDIS requirement. The comparison reference's ADDITESS label does not make ADDITESS a partner in this independent project.

## Reference provenance

The source is `VERA_MOT_EUDIS_Christoforos_28_9_26.docx`, supplied for this project on 29 September 2026. It is a work plan, not measured evidence that the proposed application or edge deployment exists.
