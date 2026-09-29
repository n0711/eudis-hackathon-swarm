# Layout and fault-time sensitivity (29 September 2026)

This is a follow-up to the [paired fleet/network comparison](generalization_study.md).
It changes only the evaluator's starting positions and physical-failure time;
the simulation's movement, task allocation, and failure logic are unchanged.
The [raw paired CSV](data/robustness_2026-09-29.csv) records every run.

## Reproduce

```bash
python -m pip install -e .
eudis-swarm-robustness --seeds 20 --seed-base 1 \
  --output docs/data/robustness_2026-09-29.csv
```

The script writes stable row order, six-decimal float fields, and Unix line
endings. Each pair uses the same seeded task positions, range, jammer, fleet
size, layout, and failure time. There are 960 runs: 4 or 6 UAVs; standard
corners-plus-ring or a centered, evenly spaced ring; failure at logical time
2 or 6; high-range, sparse, or sparse plus timed jammer; 20 task seeds and two
control modes. Every UAV travels at 10 distance units per logical second in a
square with side `100 × sqrt(UAVs / 4)`, completing two tasks per starting UAV.
The radio range is `130 × scale` or `50 × scale`; the sparse jammer's center,
radius, and 3–8 time window match the earlier study. Missions stop at time 30.

The centered ring uses radius 35% of the area side and angles starting at
45 degrees, then spaced evenly by fleet size. It changes link geometry, not
task locations. This is a deliberately chosen second family rather than a
random sample of possible layouts. The failure is always physical UAV 2.

The committed CSV has SHA-256
`582018f115e9147377ba1648f78857371d9e815b3fef8621c044fde495f75576`.

## Results

Results below count complete missions out of 20 paired task seeds for each
condition. `D` is replicated distributed ownership; `C` is the centralized
comparison. Each cell is `D completed/20 · C completed/20`. A completion by
time 30 is a bounded outcome, not a flight result.

| UAVs | Layout | Fault time | High range D/C | Sparse D/C | Sparse + jammer D/C |
| ---: | --- | ---: | ---: | ---: | ---: |
| 4 | default | 2 | 20/20 · 20/20 | 20/20 · 0/20 | 20/20 · 0/20 |
| 4 | default | 6 | 20/20 · 20/20 | 20/20 · 19/20 | 20/20 · 16/20 |
| 4 | ring | 2 | 20/20 · 20/20 | 20/20 · 10/20 | 20/20 · 10/20 |
| 4 | ring | 6 | 20/20 · 20/20 | 20/20 · 20/20 | 20/20 · 20/20 |
| 6 | default | 2 | 20/20 · 20/20 | 20/20 · 0/20 | 20/20 · 0/20 |
| 6 | default | 6 | 20/20 · 20/20 | 20/20 · 18/20 | 20/20 · 18/20 |
| 6 | ring | 2 | 20/20 · 20/20 | 20/20 · 4/20 | 20/20 · 4/20 |
| 6 | ring | 6 | 20/20 · 20/20 | 20/20 · 20/20 | 20/20 · 18/20 |

Distributed control completed all 480 runs in this matrix; centralized
completed 337/480. Both completed every high-range run. In the sparse and
sparse-plus-jammer runs together, distributed completed 320/320 and central
177/320. The size of that gap depends strongly on geometry and failure time:
the early fault in the standard layout yielded 0/20 central completions in
each sparse condition, while the late fault on the ring often yielded 20/20.
The ring also produces fewer connected components in the sparse distributed
runs (mean maximum 1.43 across both times and fleets, versus 3.21 for the
standard layout); these are different network topologies despite the same
nominal communication range.

**Late-fault qualification.** In 81 of 960 rows, all on the time-6 ring,
the mission completed before the fault was injected. `failure_injected=0`
marks these explicitly. Of the 120 paired ring/time-6 seeds across fleets
and network conditions, the fault was injected in *both* controls for 75
pairs (11 of 20 per network condition for four UAVs; 14 of 20 for six).
Within those fault-exposed pairs, both controls completed 11/11 for every
four-UAV regime and 14/14 for six UAVs in high range or sparse range; under
six-UAV sparse plus jammer, distributed completed 14/14 and central 12/14.
The full table describes a **scheduled** failure at time 6, and its totals
must not be presented as 20 post-failure trials in the ring conditions.

The jammer increased mean claim overlap in distributed control from 0.325
task-seconds in sparse runs to 0.944 with the jammer, pooled over both fleets,
layouts, and fault times. Central control's corresponding means were 0.031
and 0.022. These observer measurements include possible stale claims on a
stopped UAV; they are not a count of duplicate flights.

## Limits

All runs use the same point-mass vehicle and abstract deterministic range graph.
Varying layout and failure time exposes sensitivity inside that model; it does
not establish radio performance, safe flight, or broad statistical robustness.
The time-6 failure follows jammer onset and precedes its end; the time-2 failure
precedes jammer onset. Neither condition isolates fault timing from the task
progress already made when the failure happens. Claim overlap is an observer
metric and can include a stopped aircraft retaining a local claim; it should
not be interpreted as two responsive aircraft flying to the same task.
