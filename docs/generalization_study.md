# Paired fleet and network comparison (29 September 2026)

This is a limited generalization check of the deterministic simulator, not a
field validation or a claim about real UAVs. The experiment was run against
`main` after VehicleBackend PR #2 (`be6e8ab`), with only evaluator-side code
added to produce the rows. The [raw CSV](data/generalization_2026-09-29.csv)
contains every run and the paired configuration dimensions.

## Reproduce

```bash
python -m pip install -e .
eudis-swarm-generalization --seeds 20 --seed-base 1 \
  --output docs/data/generalization_2026-09-29.csv
```

Run the same command twice and compare the output bytes. The CLI emits rows in
fixed order with six-decimal float formatting and Unix line endings. Tests pin
pairing and a known non-completion. Timeouts in the log are expected simulation
outcomes.

The committed CSV has SHA-256
`f23f50f8157e23ee67f9946568781ebac62d85b7313e52946715509cf586e1fe`.

## Design

- Fleet sizes: 4 and 6. Tasks: two per UAV (8 and 12).
- Area side: `100 × sqrt(agents / 4)`; this keeps area and task count roughly
  proportional to fleet size. Speed stays 10 distance units per logical second.
- Network regimes: `connected` uses range `130 × scale` with no jammer;
  `sparse` uses range `50 × scale`; `sparse_jammed` uses the same sparse range
  and a midpoint-link jammer at `(40 × scale, 50 × scale)`, radius `15 × scale`,
  active from logical time 3 to 8. Here `scale = sqrt(agents / 4)`.
- For every fleet, regime, and seed 1–20, run distributed and centralized
  control with all other settings identical. Inject a physical failure of
  UAV 2 at time 4.0, use a 0.5 motion step, and cap each mission at time 30.0.
  All other settings use `SimulationConfig` defaults, including the 2.5 lease.

The `connected` label names the high-range setting, not a guarantee that every
link remains up at every instant. The script does not change decision logic or
use the optional analytical radio model.

## Results

Each cell below summarizes **20 paired seeds**. `complete` counts missions
finished by 30 logical seconds. `dropped` is mean task-seconds during which a
previously owned incomplete task has no self-claimant. `overlap` is mean
task-seconds with multiple local self-claimants; it does **not** establish that
two aircraft were physically flying the task. Means are rounded to two decimals.

| UAVs | Network | Distributed complete | Central complete | Distributed dropped | Central dropped | Distributed overlap | Central overlap |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | connected | 20/20 | 20/20 | 0.03 | 6.65 | 0.00 | 0.07 |
| 4 | sparse | 20/20 | 2/20 | 0.28 | 27.38 | 0.72 | 0.00 |
| 4 | sparse + jammer | 20/20 | 2/20 | 0.60 | 27.40 | 1.73 | 0.00 |
| 6 | connected | 20/20 | 20/20 | 0.03 | 11.25 | 0.00 | 0.03 |
| 6 | sparse | 20/20 | 1/20 | 0.07 | 32.73 | 0.12 | 0.00 |
| 6 | sparse + jammer | 20/20 | 1/20 | 0.17 | 32.73 | 0.78 | 0.00 |

In the sparse settings, the distributed run completed when the paired central
run timed out in 18/20 four-UAV seeds and 19/20 six-UAV seeds. Neither case
occurred in reverse. Detection outcomes were identical across control modes:
undetected physical losses were 0/20 in each high-range condition, 18/20 in
each sparse four-UAV condition, and 20/20 in each sparse six-UAV condition.

The timed jammer did not change completion counts relative to the sparse
setting in these seeds, but it increased measured overlap in distributed
control. The central overlap values in the high-range rows are small but
nonzero: four-UAV seeds 7, 9, and 12, and six-UAV seed 5 each had 0.5
task-seconds. Thus the earlier statement that centralized ownership overlap is
*structurally* impossible is too broad. A trace of the four-UAV, high-range,
seed-7 run shows Task 5 locally owned by responsive UAV 1 and physically stopped
UAV 2 at time 6.0. The evaluator counts both self-claims for 0.5 task-seconds;
this is claim overlap after reassignment, not evidence of two aircraft flying.

## Limits and next decision

Seeds vary task locations, not fleet geometry or fault timing. The simulator
starts four UAVs in corners but places extra UAVs on a ring, so the fleet-size
comparison also changes initial topology. A 30-second cap counts unfinished
missions as failures within this window; it does not prove they can never
complete. These runs use logical time, a point-mass vehicle, and an abstract
range/jammer model. The 20 task-placement seeds are paired comparisons, not
independent evidence about RF or flight performance.

The result survives this *small* change in fleet size and network regime.
Next test a second independently chosen initial-layout family or failure time
before claiming broad robustness. When comparing overlap costs, separate stale
claims on stopped UAVs from simultaneous work by responsive UAVs.
