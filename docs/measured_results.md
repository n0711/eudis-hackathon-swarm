# Measured results

Every number here comes from `eudis-swarm-sweep` and is reproducible from the
command printed above it. Nothing in this file is an estimate or an argument;
where a result is a negative one, or contradicts what the design was expected
to show, it is reported as such.

All coordination measurements are **evaluator-computed ground truth**: they are
obtained by reading every replica's private store at one instant, which is a
comparison no UAV can make. They are never available to the swarm.

Units are task-seconds — the number of tasks in a given condition, integrated
over logical time.

## 1. Failure detection has a connectivity floor

Declaring a UAV failed needs a quorum of peers that can still hear each other,
and nothing guarantees one exists.

```bash
eudis-swarm-sweep --param communication-range --from 50 --to 130 --steps 9 --seeds 20
```

| `communication_range` | physically lost | declared | never detected | tasks |
| ---: | ---: | ---: | ---: | ---: |
| 130 | 1 | 1 | 0 | 20/20 |
| 90 | 1 | 1 | 0 | 20/20 |
| 78 | 1 | 1 | 0 | 20/20 |
| 75 | 1 | 0 | **1** | 20/20 |
| 50 | 1 | 0 | **1** | 20/20 |

Detection fails as a cliff between 78 and 75, not as a gradient.

**Why this matters.** At 75 and below the aircraft stops at `t=4.00 s` and is
never declared. Its coordinator status stays `ACTIVE` for the whole mission, no
task is released, and all twenty tasks still complete because ownership leases
expire on their own. The ownership layer is resilient to the loss; the health
layer never notices it. **No mission-level number reveals that an aircraft is
gone** — which is precisely what makes it dangerous.

## 2. The two control modes fail in opposite ways

480 runs: twelve lease values, twenty seeds each, under both control modes,
with a balanced partition that heals.

```bash
eudis-swarm-sweep --param lease-timeout --from 0.5 --to 6.0 --steps 12 --seeds 20 \
  --communication-range 50 --failure-time 1000 --jammer 40,50,15,3,8 \
  --task-control distributed --output distributed.csv
eudis-swarm-sweep --param lease-timeout --from 0.5 --to 6.0 --steps 12 --seeds 20 \
  --communication-range 50 --failure-time 1000 --jammer 40,50,15,3,8 \
  --task-control centralized --output centralized.csv
```

| lease | centralized: stranded | centralized: double-booked | distributed: stranded | distributed: double-booked |
| ---: | ---: | ---: | ---: | ---: |
| 0.50 | 21.600 | 0.000 | 0.000 | 0.512 |
| 1.50 | 9.000 | 0.000 | 0.000 | 0.312 |
| 2.50 | 3.263 | 0.000 | 0.000 | 0.237 |
| 4.00 | 0.500 | 0.000 | 0.000 | 0.250 |
| 6.00 | 0.025 | 0.000 | 0.000 | 0.250 |
| **mean** | **4.786** | **0.000** | **0.000** | **0.286** |

All 480 runs completed 20/20. Mission duration was insensitive to the lease in
both modes: 10.74 s centralized, 10.80 s distributed.

**The signatures are cleanly disjoint.** A central coordinator serialises every
assignment, so two UAVs never both believe they own a task — concurrent
ownership is structurally zero, in every one of 240 runs. What it does instead
is hand work to aircraft it cannot reach, and that work is stranded.
Replicated ownership strands nothing and pays with occasional double-booking
during a partition. Neither approach is better; they are wrong in different
directions.

**Lease timeout is a strong dial for one mode and nearly irrelevant to the
other.** Centralized stranded work falls by a factor of 864 across the range.
Distributed double-booking falls from 0.512 to 0.250 and then plateaus, because
a replica that cannot hear the owner re-claims locally regardless of how long
the lease had left.

## 3. The operator's degradation policy is a dial on the same trade-off

420 runs: seven communication ranges, twenty seeds each, three policies.

```bash
eudis-swarm-sweep --param communication-range --from 30 --to 90 --steps 7 --seeds 20 \
  --failure-time 1000 --jammer 40,50,15,3,8 --degradation-policy HOLD --output hold.csv
```

| policy | double-booked | mission duration | tasks |
| --- | ---: | ---: | ---: |
| none (uncontracted) | 0.323 | 10.71 s | 20/20 |
| `CONTINUE` | 0.323 | 10.71 s | 20/20 |
| `HOLD` | **0.116** | **17.06 s** | 20/20 |

`HOLD` removes 64% of the double-booking and pays 59% more mission time. By
communication range:

| range | `CONTINUE` double-booked | `HOLD` double-booked | `HOLD` duration |
| ---: | ---: | ---: | ---: |
| 30 | 0.838 | 0.475 | 13.34 s |
| 40 | 0.438 | 0.263 | 17.21 s |
| 50 | 0.237 | 0.037 | 18.09 s |
| 70 | 0.188 | **0.000** | 17.26 s |
| 90 | 0.188 | **0.000** | 14.93 s |

**A contract only ever adds restraint.** `none` and `CONTINUE` agree to three
decimal places on every metric across 280 runs. That is the design invariant —
a UAV holding no contract is unconstrained rather than forbidden — confirmed by
measurement rather than asserted.

That the right policy depends on the mission is exactly why the choice belongs
to the operator rather than to the code.

## 4. Recovering from a loss the swarm cannot declare

The quorum threshold is `max(2, floor((N - 1) / 2) + 1)`, so one observer can
never declare a peer failed. With two aircraft that makes failure permanently
undeclarable: the survivor is the only witness.

Reproduce both halves directly:

```bash
eudis-swarm --scenario demo-quorum-floor      # distributed: 3/3
eudis-swarm --scenario baseline-quorum-floor  # centralized: 2/3, never finishes
```

Found by a robustness sweep over 432 configurations, which produced no crashes
but 108 incomplete missions — every one of them centralized, none distributed.

| aircraft | mode | declared | never detected | tasks | mission |
| ---: | --- | ---: | ---: | ---: | --- |
| 2 | centralized | 0 | 1 | **2/3** | **failed** |
| 2 | distributed | 0 | 1 | 3/3 | completed |
| 3 | centralized | 1 | 0 | 3/3 | completed |
| 3 | distributed | 1 | 0 | 3/3 | completed |

Both modes are equally blind: neither declares the loss. The difference is
what recovery depends on. Central reassignment is gated on a declaration that
can never arrive, so the coordinator holds a task assigned to an aircraft that
has stopped and the mission cannot finish. Distributed ownership recovers
through lease expiry, which requires no agreement at all, so the survivor
simply re-claims the work.

A third aircraft restores the quorum and both modes recover, which confirms the
floor is about witness count rather than about the control model.

This is the sharpest argument in the project: **consensus-free recovery keeps
working in exactly the conditions that make consensus impossible.**

## Reproducibility

Sweeps are deterministic: the same seeds produce byte-identical CSV, asserted
by `tests/test_sweep.py`. Held-constant dimensions (`task_control`,
`degradation_policy`) are recorded on every row, so two sweeps can be
concatenated and compared without tracking which file came from where.

## What is not measured

- **RF validity.** The radio model is an analytical free-space abstraction. It
  has not been compared against measurements and must not be described as
  calibrated RF.
- **Absolute timings.** Durations are logical simulation seconds under a
  point-mass motion model, not flight times.
- **Generality.** Every result above uses one scenario family — four UAVs, a
  100×100 area, one circular jammer. Nothing here establishes how these
  trade-offs scale with fleet size or mission geometry.
