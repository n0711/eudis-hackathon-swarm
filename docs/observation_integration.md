# Proposed external ISR observation boundary

This document specifies an integration target between an independent aerial-video perception application and the EUDIS coordination simulator. It is a proposal for recorded-data replay and interface review, not an implemented repository feature. No perception result, Raspberry Pi throughput, cross-UAV identity association, or live swarm integration is claimed here.

## Producer and consumer

Christoforos's VERA-MOT work is an external perception project. A separate EUDIS application may read video frames and emit local tracks. The EUDIS adapter may consume those observations as evidence after an explicit delivery/timeout boundary. It must not write directly to another UAV's private peer or claim store, mark a task complete from a bounding box alone, or make a track ID into a global identity. A two-source recorded replay is the first test; streaming transport is a later choice.

The supplied VERA-MOT task note calls for desktop baseline reproduction, a separated EUDIS application, measured Raspberry Pi feasibility, versioned observations, and disconnect/reconnect testing. Its Pi references alternate between Model B and Model B+; verify the physical board before describing deployment or publishing a benchmark. The current EUDIS repository contains no VERA-MOT code or checkpoint.

## Candidate version 1 record

One JSON Lines record per locally tracked object:

```json
{"schema_version":1,"uav_id":"isr_01","track_id":17,"timestamp_ms":1790590000000,"class_label":"vehicle","confidence":0.87,"bbox_xyxy":[120,85,220,160],"image_size":[640,480],"track_status":"observed"}
```

`uav_id` identifies the producer; `track_id` is unique only within that producer's track lifecycle. `timestamp_ms` is the frame or observation time in Unix milliseconds, not receipt time. `confidence` is detector confidence from 0 to 1, not identity reliability. Bounding-box coordinates are pixel `[x_min,y_min,x_max,y_max]`, constrained to the stated image dimensions. `track_status` is `observed`, `temporarily_missing`, or `expired`; consumers must distinguish a current detection from a tracker prediction. A validated identity-reliability field can be added later with a separate definition.

A separate source-status or heartbeat record should carry producer ID, schema version, timestamp, and status (`running`, `stalled`, or `stopped`). The consumer records both source and local receipt times, defines an observation freshness window and source timeout in configuration, and expires stale evidence without silently presenting it as new. A running source with no object records is an empty scene; missing status is not evidence of an empty scene.

## First integration test

1. Reproduce the existing VERA-MOT video baseline and record its dependencies and checkpoint provenance on a development machine.
2. Export a small timestamped JSONL log, plus explicit empty-scene and source-status records. Replay two logical producer IDs with the same numeric `track_id`.
3. Validate schema, bounds, order, duplicate records, and per-producer identity. Inject delayed frames, dropped messages, source interruption, and reconnect.
4. Show that an expired observation is excluded from current evidence, while the existing coordination demonstration still runs when one ISR source disappears.
5. Report desktop and physical Pi measurements separately, if the latter become available. Do not send raw video on the simulated swarm link by default.

The adapter must preserve the repository's world/receiver-local/observer separation. An observation can inform a future mission task only through a separately specified, tested task-discovery rule. Neither this schema nor the existing simulator supplies geolocation from a 2-D bounding box.

## Reference provenance

The source is `VERA_MOT_EUDIS_Christoforos_28_9_26.docx`, supplied for this project on 29 September 2026. It is a work plan, not measured evidence that the proposed application or edge deployment exists.
