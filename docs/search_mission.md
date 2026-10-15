# Coordinated area search

Challenge 2 names search and rescue and mapping as example missions, and the
Cyprus edition asks for "coordinated search". This page describes how the
existing coordination layer performs one, and what was measured.

## How it works

The mission area is cut into square sectors (20 m by default over the 100 m ×
100 m area, so 25 sectors), numbered in lawnmower order. Each sector becomes an
ordinary task whose objective is the sector centre; a UAV that reaches it is
taken to have searched the sector, on the assumption that its sensor footprint
covers one sector.

No coordination code changes. The same receiver-local utility, claims, leases
and reconciliation that allocate any task decide which UAV searches which
sector. The consequences follow from the existing protocol:

- **No central planner.** Each UAV picks its next sector from its own view and
  claims it before flying.
- **A lost UAV's sectors are not abandoned.** They become claimable to the
  others when each receiver's own copy of the lost UAV's lease expires — no
  failure declaration is needed.
- **During a partition, both sides keep searching.** Both may search the same
  sector; after reconnection the claims reconcile. The cost of availability is
  redundant search, and it is measured below.

Coverage is computed by the observer from the run's mission events: the first
time each sector was searched, by whom, and how many searches were redundant.
Nothing measured feeds back into a UAV's decisions.

## Run it

```bash
eudis-swarm-search                                   # UAV 2 lost at 8 s; both modes
eudis-swarm-search --failure-time -1                 # no loss
eudis-swarm-search --jammer 50,50,25,3,15            # central jammer from 3 s to 15 s
eudis-swarm-search --study --csv search.csv          # the paired study below
```

(`python -m eudis_swarm.search_mission` is equivalent.)

## Measured: 64 paired runs

25 sectors, 4 UAVs. Every combination of the lost UAV (1–4), the loss time (4,
8, 12, 16 s) and jamming (none, or a 25 m-radius jammer at the area centre from
3 s to 15 s) was run under both control modes on identical sectors. Raw rows:
[`data/search_2026-10-02.csv`](data/search_2026-10-02.csv). All times are
logical simulation seconds.

| Links | Mode | Full coverage | Mean time to 100% | Redundant searches |
| --- | --- | ---: | ---: | ---: |
| clean | distributed | 16/16 | 16.69 s | 0 |
| clean | central | 16/16 | **15.89 s** | 0 |
| jammed | distributed | **16/16** | 21.12 s | 67 |
| jammed | central | 14/16 | 21.18 s | 27 |

What this shows:

- **With clean links the central coordinator is faster** by 0.8 s on average:
  it reassigns as soon as it declares the loss, while the distributed mode
  waits for leases to expire.
- **Under jamming, the central coordinator twice leaves a sector unsearched
  for good** (UAV 1 or 2 lost at 12 s while the jammer is active: 24 of 25
  sectors, mission timed out at 300 s). The distributed search completed every
  run.
- **The distributed mode pays for that in duplicate effort**: 67 redundant
  sector searches across the 16 jammed runs (about four per run), against 27
  for the central coordinator.

## Limits

- A sector counts as searched when its centre is reached. There is no sensor
  model, probability of detection, or footprint geometry; with a real sensor a
  sector could need several passes.
- Search and target hand-off are demonstrated separately. Detections do not
  yet create inspection tasks during a search run; the ISR hand-off is shown by
  the scheduled relay-recovery scenario in
  [`observation_integration.md`](observation_integration.md).
- The vehicles are point masses, as in every simulator result; flight times
  are logical seconds, not airframe performance.
- One area size, one sector size, one jammer geometry. Treat the table as a
  controlled comparison, not a general performance claim.
