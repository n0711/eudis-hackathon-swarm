# Code quality and performance review

This document records the focused cleanup and performance work measured at its
named baseline commit. Later milestones preserve the conclusions while
tightening peer eligibility from raw freshness to complete `HEARD` status and
adding deterministic flooding and receiver-local claim-authorized task control.
The allocator and direct-delivery measurements below remain historical baseline
benchmarks, not measurements of the current operational claim loop.

## Scope and method

This cleanup reviewed every source module, test, prototype document, package
setting, and CI step at commit `97b508b`. It introduced no swarm capability and
did not change the scheduler, allocation scores, topology rules, peer-knowledge
boundary, or physical recovery behavior.

Runtime measurements used Python 3.13 on Windows, `time.perf_counter`, disabled
logging, a warm-up run, and garbage collection before each measured run. The
4x20 and 6x30 medians use seven repetitions; 20x200 and 50x500 use three. These
are local microbenchmarks, not cross-machine performance guarantees.

## Bottlenecks found

`cProfile` on 20 UAVs / 200 tasks showed the connectivity allocator consuming
3.54 of 3.63 profiled seconds. Its repeated-minimum loop reevaluated 132,293
candidate pairs and performed 4.26 million validated peer-store lookups. The
distance allocator had the same repeated candidate-scan shape at lower cost.

Graph recomputation and peer delivery were secondary. The graph rebuilt its
stable canonical pair iterator and repeatedly materialized active-link tuples.
Transport performed a validated canonical link lookup for every directed
delivery attempt. Repeated sorting of fixed agent membership was measurable but
not dominant.

## Changes retained

- Both allocators compute static pair scores once, sort once, and greedily skip
  pairs whose UAV or task was already selected. This is equivalent to repeated
  minimum scans because allocation proposals do not mutate state within a batch.
- Connectivity allocation collects each candidate UAV's `HEARD` delivered peer
  positions once per batch. It still uses no authoritative remote position.
- The graph caches its immutable canonical UAV pairs and current active-link
  tuple/set. Link semantics and inclusive Euclidean range are unchanged.
- Transport gets the source's active neighbor set once, then uses membership
  checks for directed deliveries.
- `Mission` caches fixed deterministic agent order, and position history tracks
  its last timestamp directly instead of rescanning every history.
- Shared positive/non-negative real validators now enforce consistent finite
  numeric and boolean-rejection rules.
- `legacy_boundary` was renamed `mission_boundary`; concise comments now explain
  scheduler ordering and why batch sorting is safe.

## Runtime results

| Scenario | Policy | Before median | After median | Absolute change | Change |
| --- | --- | ---: | ---: | ---: | ---: |
| 4x20 | distance | 4.537 ms | 4.094 ms | -0.443 ms | -9.8% |
| 4x20 | connectivity | 6.516 ms | 4.100 ms | -2.416 ms | -37.1% |
| 6x30 | distance | 4.691 ms | 4.602 ms | -0.089 ms | -1.9% |
| 6x30 | connectivity | 7.947 ms | 4.379 ms | -3.568 ms | -44.9% |
| 20x200 | distance | 54.707 ms | 33.565 ms | -21.142 ms | -38.6% |
| 20x200 | connectivity | 917.881 ms | 62.940 ms | -854.941 ms | -93.1% |
| 50x500 | distance | 534.523 ms | 217.769 ms | -316.754 ms | -59.3% |
| 50x500 | connectivity | 43.695 s | 0.779 s | -42.916 s | -98.2% |

Sub-10 ms results are sensitive to ordinary local noise; the large-scenario
improvements are the meaningful result.

After cleanup, the same 20x200 `cProfile` run reported 0.087 s and 293,892 calls
for distance, and 0.166 s and 653,687 calls for connectivity. Connectivity's
largest remaining allocator cost is the 21,006 endpoint-degree calculations and
352,219 required Euclidean distance checks.

## Protocol-counter correction

An earlier default run reported 15,075 logical protocol attempts and only 474
successes. That was not evidence of a lossy network: the communication graph
remained a clique. The entire 14,601 difference was repeated evaluation toward
failed, inactive UAV 2, and instrumentation classified 97.1% of those
evaluations as repeats. Collapsing the pending work by `(message ID, inactive
receiver)` exposed 141 unique obligations.

The transport now excludes inactive endpoints from the link-attempt denominator
and reports newly observed obligations as `Inactive-endpoint deferrals`.
`Logical forwarding attempts` therefore means eligible delivery evaluations;
its outcomes are `Successful first deliveries` or `Unavailable-link attempts`.
`Useful first deliveries` separately counts domain mutations, while duplicate
source publications and duplicate-route suppressions explain idempotent work.
None of these values represents RF packets, bytes, bandwidth, or airtime.

With freshness-threshold renewal pacing and no redundant second claim round when
nothing completed, the current default reports 178 attempts, all 178 successful
and useful, zero unavailable or forwarded first deliveries, 1,552 duplicate
source publications, 428 duplicate-route suppressions, and 53 inactive-endpoint
deferrals. Zero forwarding is expected in the default clique; chain tests cover
relay behavior.

## Memory results

`tracemalloc` recorded peak Python allocations for one run:

| Scenario | Policy | Before peak | After peak | Change |
| --- | --- | ---: | ---: | ---: |
| 20x200 | distance | 0.507 MiB | 0.674 MiB | +0.167 MiB |
| 20x200 | connectivity | 0.505 MiB | 0.826 MiB | +0.321 MiB |
| 30x300 | distance | 0.845 MiB | 1.109 MiB | +0.264 MiB |
| 30x300 | connectivity | 0.846 MiB | 1.387 MiB | +0.541 MiB |

The ranked pair table intentionally trades modest temporary memory for much
less repeated computation. Retained post-run data structures were not removed;
they are required by tests, metrics, documentation, and visualization.

## Complexity

Let `N` be available UAVs, `M` unassigned tasks, `K=min(N,M)`, and `P` heard
peer observations per candidate UAV.

| Operation | Before | After |
| --- | --- | --- |
| Distance allocation batch | `O(K*N*M)` pair evaluations | `O(N*M log(N*M))` time, `O(N*M)` temporary space |
| Connectivity allocation batch | `O(K*N*M*P)` | `O(N*M*P + N*M log(N*M))` time, `O(N*M)` temporary space |
| Graph update | `O(N^2 + N + E)` | Same asymptotic cost; fixed pairs and active views cached |
| Peer delivery batch | `O(S*N)` attempts | Same; lower constant-cost neighbor membership |
| Agent traversal/completion | sorting plus `O(N)` | cached order and `O(N)` scan |
| Receiver-local objective ranking | not operational | per idle UAV: `O(M*P + M log M)` for connectivity cost and stable ranking |

## Rejected changes

- Squared-distance range checks were rejected to preserve the exact established
  `hypot(...) <= range` boundary behavior.
- Spatial indexes are unjustified for the current all-pairs graph and task sizes.
- Event/history removal and memory pooling would harm observability for small
  memory savings.
- A scheduler framework, generic event bus, concurrency, and third-party
  benchmark dependency would add complexity without addressing measured costs.
- `Simulation.run()` was not fragmented into many phase methods; its explicit
  event order is more important than reducing its line count.

## Preserved invariants and remaining limits

Golden tests preserve Prototype 0.1 recovery, 0.2A topology, 0.2B strict
freshness/delivery, and the 0.3A UAV 2 -> Task 2 versus UAV 2 -> Task 3 decision
using only status-qualified `HEARD` observations. Those allocators are now
comparison baselines; the normal mission obtains the same policy choice through
receiver-local utility, claims before binding, and gates every physical action on
`owns_task()`. Distance remains the default. Communication loss still does not
imply physical failure, and stale-but-lease-valid ownership still cannot release
work.

At larger scales, connectivity endpoint-degree calculation is the likely next
bottleneck, followed by `O(N^2)` graph/link creation. One-hop heartbeat batches
retain the documented `O(S*N)` shape. For `M` pending immutable messages, the
current small-swarm flooding implementation can inspect `O(M*N^2)` directed
node/message routes in a dissemination round; per-receiver duplicate suppression
prevents loop-driven rebroadcast and makes that work finite. Any future spatial
indexing, alternative matching algorithm, or gossip optimization must be
benchmarked and must preserve deterministic tie-breaking, origin identity,
persistent retry, and local knowledge.
