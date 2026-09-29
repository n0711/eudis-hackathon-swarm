# External ISR observation replay boundary

This document specifies the boundary between an independent aerial-video perception application and EUDIS. The repository now contains a standalone, deterministic receiver-local JSONL replay adapter. It does not connect to `Simulation` task discovery or import VERA-MOT. No perception result, Raspberry Pi throughput, cross-UAV identity association, or live swarm integration is claimed here.

## Producer and consumer

Christoforos's VERA-MOT work is an external perception project. A separate EUDIS application may read video frames and emit local tracks. `observation_replay.py` validates externally supplied records and stores each delivered record in one receiver's private view. It does not write to peer or claim stores, mark a task complete from a bounding box, or make a local track ID into a global identity. Streaming transport and actual simulator integration remain later choices.

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

## Remaining integration work

1. Reproduce the existing VERA-MOT video baseline and record its dependencies and checkpoint provenance on a development machine.
2. Export an actual timestamped JSONL log from the perception application, with explicit empty-scene and source-status records. Validate its schema against the standalone replay.
3. Specify how simulated link delivery reaches each receiver and test interruption, reconnection, and stale evidence without giving a UAV another receiver's private state.
4. Define a separate task-discovery rule, if observations should create mission objectives; test it before connecting observations to task claims or completion.
5. Report desktop and physical Pi measurements separately, if the latter become available. Do not send raw video on the simulated swarm link by default.

The adapter must preserve the repository's world/receiver-local/observer separation. An observation can inform a future mission task only through a separately specified, tested task-discovery rule. Neither this schema nor the existing simulator supplies geolocation from a 2-D bounding box.

## Reference provenance

The source is `VERA_MOT_EUDIS_Christoforos_28_9_26.docx`, supplied for this project on 29 September 2026. It is a work plan, not measured evidence that the proposed application or edge deployment exists.
