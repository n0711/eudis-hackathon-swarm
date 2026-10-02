# Field node: the protocol as real processes over a real network

Until now the coordination protocol has only run inside the deterministic
single-process `Simulation`. The field node runs the same receiver-local
stores as separate OS processes, one per UAV, exchanging real UDP datagrams.
It runs on one laptop over loopback or on several Raspberry Pis and laptops on
a LAN. Packets are really lost, nodes really fail, and partitions are either
real (WiFi off, out of range, a firewall rule) or imposed below the protocol
by a drop filter.

This is the lab evidence intended to support a TRL4 claim ("technology
validated in lab") for EUDIS Defence Hackathon 2026, Challenge 2. What it
does and does not show is spelled out in
[What this validates](#what-this-validates--and-what-it-does-not). The
[results](#results-localhost-only) below come from **localhost** runs only.
No hardware run has been recorded yet; the [lab test cards](#lab-test-cards)
(LT4, LT5, LT6 in [`trl4_validation.md`](trl4_validation.md)) explain how to
make one.

## Architecture

```
            shared mission file (agents, tasks, timing, addresses)
                                  |
      +---------------------------+---------------------------+
      v                           v                           v
+--------------+          +--------------+          +--------------+
| field_node 1 |   UDP    | field_node 2 |   UDP    | field_node N |
|  (process)   |<-------->|  (process)   |<-------->|  (process)   |
+--------------+          +--------------+          +--------------+
 each process owns, and nothing else:
   PeerStateStore      (peer_state.py, unchanged)
   TaskClaimStore      (task_claims.py, unchanged)
   LocalFailureDetector(field_detector.py: FailureManager's rules, one replica)
   TaskObjective catalogue + ReceiverLocalTaskUtility (task_utility.py, unchanged)
   own position: PointMassVehicle (real elapsed time) | MavlinkFieldVehicle
   EvidenceRelay + UdpTransport (field_transport.py) behind FaultInjector
   JSONL log  ----------------------------------------------+
                                                            v
             field_launch (localhost harness)        field_report (observer)
             writes control.json, launcher.jsonl     merges logs, judges G1-G3/S1
```

| Module | Layer | Role |
| --- | --- | --- |
| [`field_codec.py`](../src/eudis_swarm/field_codec.py) | wire | Versioned JSON envelope for `Heartbeat`, `TaskClaim`, `TaskClaimRelease`, `TaskCompletionEvidence`, `FailureVote`, `FailureDeclaration`. Strict validation of untrusted input. |
| [`field_transport.py`](../src/eudis_swarm/field_transport.py) | wire | `UdpTransport` (stdlib `socket`, unicast peer list or broadcast) and `EvidenceRelay` (duplicate suppression by structural ID, forward on first delivery, persistent re-offer). |
| [`field_detector.py`](../src/eudis_swarm/field_detector.py) | receiver-local control | One failure-detector replica: votes from local silence, quorum declaration, certificate application, retraction on contact. |
| [`field_mission.py`](../src/eudis_swarm/field_mission.py) | configuration | Loads and validates the shared mission file. |
| [`field_node.py`](../src/eudis_swarm/field_node.py) | receiver-local control | One UAV per process: the loop, the vehicle adapters, the JSONL log. `FieldNode.deterministic = False`. |
| [`field_faults.py`](../src/eudis_swarm/field_faults.py) | harness | Drop windows, control-file partitions, seeded random loss, fail-stop. |
| [`field_launch.py`](../src/eudis_swarm/field_launch.py) | harness | Starts N nodes on localhost with a fault schedule and collects the logs. |
| [`field_report.py`](../src/eudis_swarm/field_report.py) | observer | Merges logs and evaluates the guarantee. Exits non-zero on violation. |
| [`field_probe.py`](../src/eudis_swarm/field_probe.py) | measurement | Link probe: delivery ratio and latency, CSV, summary in `RadioModel` terms. |

None of the coordination-core modules changed. The field node composes
`PeerStateStore`, `TaskClaimStore`, `ReceiverLocalTaskUtility`, `Heartbeat`,
`FailureVote` and `FailureDeclaration` exactly as the simulator does. The only
new protocol code is `LocalFailureDetector`. `FailureManager` binds every
replica's store in one process, so a node holding one store cannot use it.
The new class reimplements the same rules for a single receiver, and
`tests/test_field_detector.py` checks those rules separately.

### One loop iteration

`FieldNode.step()` runs every `round_interval` (50 ms by default) on the
node's own monotonic clock. That clock counts seconds since the node started
and is also the source clock stamped on its own evidence.

1. Fail-stop if the injector says so. Refresh the drop filter.
2. Drain the socket. Each datagram gets the receiver's clock time **when it is
   read**. A heartbeat goes to the `PeerStateStore` only if its hop sender is
   its own author: heartbeats are one-hop and are never forwarded, and a
   relayed one is rejected. Protocol evidence is admitted once per
   structural ID, applied to the store, and forwarded on its first delivery.
3. Age peer freshness. On the heartbeat cadence, send a heartbeat, vote
   against peers that are silent *here*, and declare only on a quorum.
4. Run one task round. This is the single-UAV version of
   `Simulation._exchange_claim_evidence`:
   - advance the lease clock;
   - `reconcile_all()`, where the local loser emits an exact-generation
     release;
   - stand down from a task that no longer passes `owns_task()`;
   - renew the held claim once it is `freshness_timeout` old;
   - when idle, rank the locally claimable objectives and **create a claim
     before binding**;
   - bind only after `claim_settle` (150 ms), and only if the claim is still
     owned.
5. Move only while `owns_task()` holds. On arrival, create completion
   evidence.
6. On the rebroadcast cadence (250 ms), re-offer own live evidence and the
   lease-valid evidence held for others.
7. Write one JSONL record for every belief change.

### Wire format

One datagram carries one envelope:

```json
{"body":{"created_at":3.757,"epoch":1,"freshness_timeout":0.75,"lease_timeout":1.5,
 "owner_agent_id":1,"task_id":4},"kind":"claim","mission":"lab-4uav",
 "proto":"eudis-field","sender":2,"v":1}
```

`sender` is the hop that transmitted the datagram. It is not the origin of the
evidence: here node 2 is forwarding node 1's claim. Decoding rejects all of
the following with `CodecError`, and a node only counts and logs a rejection,
so none of them can crash it:

- datagrams over 4096 bytes, checked before parsing;
- non-UTF-8 or non-JSON input, and nesting deep enough to overflow the parser;
- `NaN`, `Infinity`, and numbers that overflow to infinity;
- any version other than 1, and unknown kinds;
- missing or extra keys;
- booleans or floats where an integer is required;
- values that break a domain invariant, such as a release whose winner has
  the higher ID, or a completion dated before its claim.

A fuzz test sends 3000 corrupted datagrams and checks that `CodecError` is the
only exception raised.

## How each invariant is preserved

| AGENTS.md invariant | How the field node keeps it | Test |
| --- | --- | --- |
| Receiver-local knowledge boundary | A node's only inputs are the mission file, its own vehicle, and datagrams that arrived. `field_node`, `field_transport`, `field_detector` and `field_codec` import none of `mission`, `simulation`, `communication`, `messaging`, `metrics`, `trace`, `evaluation` or `task_allocator`. The report and launcher are never imported by a node. | `test_field_node_cannot_reach_world_truth` |
| Determinism of the core simulator | No field module is reachable from `eudis_swarm.simulation` or `eudis_swarm.sweep`. This is checked in a fresh interpreter, like the SITL check. Every field class that moves or times anything declares `deterministic = False`. | `test_field_modules_stay_out_of_the_simulator_import_graph` |
| No new runtime dependency | Stdlib only: `socket`, `select`, `json`. `pymavlink` is imported only inside `open_mavlink_vehicle`, through `sitl.py`, when `--mavlink` is given. | CI installs without the `sitl` extra |
| Silence is not failure | `LocalFailureDetector` votes from local silence and declares only with its own vote plus `max(2, floor((N-1)/2)+1)` matching votes received within `heartbeat_timeout`. A declaration changes belief only. A heartbeat received first-hand retracts it. | `test_silence_alone_never_declares`, report check S1 |
| No topology leakage | Partitions are imposed below the codec: a filtered datagram is never decoded, and the node is never told why it is missing. | `test_chain_topology_floods_evidence_but_never_heartbeats` |
| Source clock vs receiver clock | `received_at` is the receiver's monotonic clock at `recvfrom`. Freshness, vote windows and lease ages use only `local_now - received_at`. Source timestamps are carried but never compared across nodes. The wall-clock field in the logs is used only by the observer report. | `test_receipt_is_stamped_with_the_receiver_clock_on_arrival` |
| Lease semantics (`STALE != UNCLAIMED`) | Unchanged `TaskClaimStore`. A node creates a claim only when `can_create_claim()` holds, that is, when its copy has `EXPIRED`. | node tests; the chain test checks that 3 never claims under an owner it cannot hear |
| Heartbeats one-hop | The relay rejects heartbeats outright. A received heartbeat whose `agent_id` differs from its hop `sender` is rejected as `forwarded_heartbeat`. | `test_a_forwarded_heartbeat_is_rejected_not_believed` |
| Evidence floods with store-and-forward and duplicate suppression | `EvidenceRelay.admit()` accepts a structural ID (`(kind, claim_id)`, or the vote and declaration generation fields) once per receiver and forwards it once. UDP gives no delivery signal, so the simulator's "route pending until delivered" becomes persistent re-offer instead. Origins re-offer their own live evidence, and relays re-offer lease-valid claims and releases plus every completion. Duplicates are suppressed and never refresh a lease. There is no TTL and no random backoff. | `test_relay_*`, chain test |
| `owns_task()` authorizes every action | Movement, renewal and completion each call `owns_task()` at the moment they act. `current_task` is an intent cache. | node tests |
| Lowest-ID reconciliation, loser stands down | Unchanged `reconcile_all()`. On a local loss the node emits the release and stands down in the same round. | report check G3, end-to-end test |

## Running it

Set up once:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,sitl]"   # sitl only if a vehicle will be attached
```

### (a) One laptop: localhost with a 2+2 partition

```bash
.venv/bin/python -m eudis_swarm.field_launch \
    --mission examples/field/mission_4uav.json \
    --faults  examples/field/partition_2x2.json \
    --duration 20 --logs logs/partition --report
```

The launcher starts four `field_node` processes on `127.0.0.1:47001-47004`
with a common start time. At t=3 s it partitions `{1,2} | {3,4}` and at
t=8 s it heals, by rewriting `logs/partition/control.json`. It then waits and
runs the report. The exit status is the report's: 0 pass, 1 violated,
2 inconclusive. Other schedules:

- `examples/field/fail_stop_4.json` stops node 4 at t=4 s. Use `--duration 40`
  so that its task can be re-claimed and flown before the run ends;
- `--loss 0.2` adds 20% random loss to each one-hop delivery, applied at the
  receiver.

You can also start nodes by hand, which is the same thing the launcher does:

```bash
.venv/bin/python -m eudis_swarm.field_node --mission examples/field/mission_4uav.json \
    --agent 1 --duration 30 --log logs/n1.jsonl --control logs/control.json
# ... --agent 2, 3, 4 in other terminals; then edit logs/control.json:
#   {"partition": [[1, 2], [3, 4]]}   ...later...   {}
.venv/bin/python -m eudis_swarm.field_report logs/
```

### (b) Several hosts: Raspberry Pis and laptops on a LAN

1. Give every host a static IP on the same subnet. Copy the repository and
   install it on each host as above. Python 3.11 or later is required.
2. Write one mission file with each agent's real address, for example
   `"address": "192.168.50.11:47000"`, and copy the identical file to every
   host. Timing must match exactly, because claim lease policy is validated on
   receipt. To use broadcast instead of a unicast list, set
   `"network": {"mode": "broadcast", "broadcast_address": "192.168.50.255",
   "port": 47000}`. Broadcast has been checked on loopback
   (`127.255.255.255`), not yet on a LAN.
3. Synchronise clocks well enough for the observer report, for example with
   `chrony` against one laptop. Nodes never use wall time for decisions; the
   report uses it to merge logs, so skew between hosts appears directly as
   error in the convergence time. Record `chronyc tracking` offsets.
4. On each host, at the same time:

   ```bash
   START=$(( $(date +%s) + 20 ))   # agree one start time, e.g. read it out
   .venv/bin/python -m eudis_swarm.field_node --mission mission_lan.json \
       --agent 1 --bind 0.0.0.0:47000 --duration 120 --start-at $START \
       --log logs/node-1.jsonl
   ```

   Bind `0.0.0.0` so that the socket survives the interface going down and up.
5. Cause a **real** partition in one of these ways, and write down the
   wall-clock time of each change:
   - **WiFi off.** Run `sudo nmcli radio wifi off` (or `rfkill block wifi`) on
     the hosts of one side, then `on` again. This isolates each host it is
     run on.
   - **Range.** Walk one pair out of range of the access point and back.
   - **Firewall.** For a reproducible 2+2 split on hardware, run this on each
     host of side A, once for each host of side B:
     `sudo iptables -I INPUT -s <B-host-ip> -j DROP` and
     `sudo iptables -I OUTPUT -d <B-host-ip> -j DROP`. Undo with `-D` instead
     of `-I`. This is a real network-layer partition, outside the program.
   - Alternatively, the in-program filter works on hardware too, with
     `--drop 3,4@30-60` on hosts 1 and 2 and `--drop 1,2@30-60` on hosts 3
     and 4. Times are node seconds after `--start-at`.
6. Copy every `node-*.jsonl` to one machine and run
   `python -m eudis_swarm.field_report logs/`. Without in-program filter
   events, the report takes reconnection to be the first instant after which
   every live pair stays mutually `HEARD`. Pass
   `--reconnect-at <wall time>` to use the time you wrote down instead.

A live vehicle can stand in for the point mass on any node with
`--mavlink udp:127.0.0.1:14550 --takeoff-alt 10`. That needs the `sitl` extra.
The vehicle's frame is anchored at the agent's mission start position. This
path is unit-tested against a fake telemetry source and has **not** been
flown.

### (c) Link probe

```bash
# host B
python -m eudis_swarm.field_probe echo --bind 0.0.0.0:47100 --duration 300
# host A
python -m eudis_swarm.field_probe send --target <B-ip>:47100 \
    --count 3000 --rate 20 --size 300 --output probe-A-B.csv
python -m eudis_swarm.field_probe summarize probe-A-B.csv --json probe-A-B.json
```

Each CSV row is one sequence number with the sender's send time, the echo's
arrival time (empty when lost) and the RTT. All times come from the sender's
clock, so no clock synchronisation is needed. The summary reports:

- round-trip delivery ratio;
- one-way delivery estimate `sqrt(ratio)`, which assumes a symmetric link with
  independent loss (stated next to the number);
- loss-burst count, mean and maximum;
- RTT p50, p95, p99, max and jitter;
- one-way latency estimate RTT/2;
- the same link in simulator terms: `RadioModel.link_quality` (a per-frame
  delivery probability), the equivalent BER for `frame_bits`, and the
  probability that independent loss alone silences a peer for one
  `heartbeat_timeout`.

The simulator delivers within one tick, so measured latency is reported as
latency the simulator does not model.

## Lab test cards

These are the procedures that [`trl4_validation.md`](trl4_validation.md)
refers to as LT4, LT5 and LT6. Record each run as one row in that document's
evidence log.

The report prints two separate verdicts. `field report: PASS|VIOLATED|INCONCLUSIVE`
is the **coordination guarantee**, and it alone sets the exit code.
`mission complete YES|NO` says whether every task was completed. A run that is
too short can satisfy the guarantee without finishing the mission, so every
card below requires **both**.

Record for every run:

- the commit (`git rev-parse HEAD`);
- the mission file hash (`sha256sum <mission>`);
- the hardware and OS;
- the full report text and `report.json`;
- the node logs.

### LT4 — separate processes on one host (localhost)

**Equipment:** any laptop with Python 3.11 or later and this repository
installed (`pip install -e ".[dev]"`).

| # | Command | Expect |
| --- | --- | --- |
| 1 | `python -m pytest tests/test_field_launch.py -q` | `3 passed` |
| 2 | `python -m eudis_swarm.field_launch --mission examples/field/mission_4uav.json --faults examples/field/partition_2x2.json --duration 20 --logs logs/lt4-partition --report` | exit 0 |
| 3 | `python -m eudis_swarm.field_launch --mission examples/field/mission_4uav.json --faults examples/field/partition_2x2.json --duration 20 --loss 0.2 --logs logs/lt4-lossy --report` | exit 0 |
| 4 | `python -m eudis_swarm.field_launch --mission examples/field/mission_4uav.json --faults examples/field/fail_stop_4.json --duration 40 --logs logs/lt4-failstop --report` | exit 0 |

**Pass** only if every condition holds:

- Steps 2–4 all exit 0, and each report shows `field report: PASS` and
  `mission complete YES (4/4 tasks ...)`.
- Steps 2 and 3 show `divergent at reconnect` = `[3, 4]` or another non-empty
  list. That is the evidence the partition really split ownership.
- Steps 2 and 3 show convergence after reconnect ≤ the printed bound
  (1.75 s), and every `contest resolutions` entry has the lowest ID as
  winner.
- Step 4 shows a `fail-stop detection` latency for each of nodes 1, 2 and 3
  (about 0.5–0.65 s with this timing) and 0 duplicate completions. Node 4's
  task is completed by a survivor.

The fail-stop run needs `--duration 40`. Node 4's task becomes claimable
only after its lease expires, and the survivor then still has to fly the
leg. With 15 s the guarantee passes but the mission does not finish.

**Fail** on any exit 1, `mission complete NO`, or an empty
`divergent at reconnect` in steps 2 and 3. An empty list means no split
brain was created, so the run proves nothing; repeat it.

### LT5 — networked hosts with a real partition

**Equipment:** at least 3 hosts (4 to reproduce the 2+2 split), such as
Raspberry Pi 4/5 boards or laptops. They need Python 3.11 or later, the same
commit, one Wi-Fi access point or Ethernet switch, static IPs, and `chrony`
syncing against one host. You also need a second person with a stopwatch, or
a phone showing wall-clock time.

**Setup**

1. Copy `examples/field/mission_4uav.json` to `mission_lan.json`. Set each
   agent's `"address"` to `"<host-ip>:47000"`, and delete agent and task
   entries beyond the number of hosts. Copy the identical file to every host
   and compare `sha256sum mission_lan.json` on each.
2. Run `chronyc tracking` on every host and record the offsets.
3. Run LT6 between at least one pair of hosts first.

**Run.** Every host uses the same `START`, for example read aloud as
`date +%s` plus 30.

```bash
python -m eudis_swarm.field_node --mission mission_lan.json --agent <N> \
    --bind 0.0.0.0:47000 --start-at <START> --duration 180 \
    --log logs/lt5/node-<N>.jsonl
```

| t after START | Action (record wall-clock time) |
| --- | --- |
| 0–30 s | connected baseline: do nothing |
| 30 s | **cut:** switch Wi-Fi off on the hosts of one side (`sudo nmcli radio wifi off`), unplug their switch port, or apply the firewall rules from (b) |
| 60 s | **reconnect:** reverse the cut |
| 60–180 s | do nothing; the nodes stop by themselves |
| optional, second run | at 60 s, power off one Pi instead of reconnecting (physical fail-stop) |

Collect all `node-*.jsonl` on one machine, then run:

```bash
python -m eudis_swarm.field_report logs/lt5 --json logs/lt5/report.json \
    --reconnect-at <reconnect wall-clock time, UNIX seconds>
```

**Pass** only if every condition holds:

- Exit 0 with `field report: PASS`, and `mission complete YES`.
- `divergent at reconnect` is non-empty.
- Convergence after reconnect ≤ the bound (1.75 s). If the chrony offsets
  exceed 50 ms, write down the convergence time with that uncertainty.
- Every contest was won by the lowest ID, and `contest resolutions` lists no
  other winner.
- Every partition-era declaration was retracted after reconnect. Check that
  `retractions` ≥ the number of distinct (declarer, peer) pairs.
- In the fail-stop variant, every surviving host declared the powered-off
  host, and the report shows no retraction for it.

**Fail** on exit 1, `mission complete NO`, or a convergence time over the
bound. **Inconclusive** (exit 2): repeat with a longer `--duration`, or fix
the link first; LT6 shows how bad it is.

### LT6 — measured link delivery

**Equipment:** two hosts on the network that LT5 will use.

```bash
# host B (echo)
python -m eudis_swarm.field_probe echo --bind 0.0.0.0:47100 --duration 600
# host A (sender), once per position or separation
python -m eudis_swarm.field_probe send --target <B-ip>:47100 \
    --count 3000 --rate 20 --size 300 --output lt6-<position>.csv
python -m eudis_swarm.field_probe summarize lt6-<position>.csv --json lt6-<position>.json
```

**Pass** (the measurement is valid) only if:

- 3000 probes were sent;
- the summary was produced;
- the CSV and JSON are kept, with the position, distance and obstructions
  written down for each.

LT6 measures; it has no performance threshold. A round-trip delivery ratio
below 0.9, or a p99 RTT above the 0.1 s heartbeat interval, predicts
trouble for LT5's `peer_stale_after` and `heartbeat_timeout`. Record that
next to the result rather than tuning the timing silently.

**Record:** the CSV per position and the summary line for each. Claim only the
measured environment.

## What this validates — and what it does not

**Validates, on whichever network it is run on:**

- The unchanged receiver-local stores keep their guarantee when replicas are
  separate processes with independent clocks and the only channel is lossy,
  reordering UDP. Within the bound after a heal, every live replica agrees on
  every task, the lowest ID wins every contest, and every losing claimant
  that was executing stands down.
- Silence is not failure on a real network. Partition-era declarations need a
  real quorum and are retracted by first-hand contact after the heal. A
  fail-stopped node is declared by every survivor and its task is re-claimed
  after lease expiry.
- Strict handling of untrusted input: malformed, oversized and foreign
  datagrams are counted and dropped without crashing a node.
- Store-and-forward without topology knowledge: on a chain where 1 and 3
  cannot hear each other, claims still reach 3 through 2, and heartbeats
  never do (unit test).

**Does not validate:**

- **Radio behaviour.** Localhost loopback is effectively lossless with
  sub-millisecond latency. Its numbers say nothing about a real link beyond
  "the software is not the bottleneck". Hardware runs and probe CSVs are
  needed for that.
- **Scale.** Only 2 to 4 nodes have been run.
- **Flight.** The MAVLink adapter has not flown and has not been run against
  SITL in this setup. Motion is a point mass on wall-clock time.
- **Restart.** A restarted node begins again at epoch 1 with a fresh clock.
  Peers that still hold its old claims would see a conflicting equal-epoch
  claim and reject it until the old one expires. Only fail-stop is supported.
- **Security.** There is no authentication or encryption. Strict decoding
  stops malformed input, not a well-formed lie. Anyone on the subnet who
  knows the mission ID can inject evidence.
- **Throughput.** Traffic is not optimised: 73-87 datagrams/s per node for
  4 nodes and 4 tasks, and 90-97% of received protocol evidence is suppressed
  duplicates from the persistent re-offer. That is the price of reliable
  flooding without acknowledgements. It has not been measured at larger swarm
  sizes.
- **Multi-host timing.** Convergence times from separate hosts are only as
  accurate as their clock synchronisation.
- **Determinism.** None of this is a deterministic result, and none of it
  replaces a number in `measured_results.md`.

## Results (localhost only)

### Cloud VM, localhost UDP, 2026-10-02

> **All numbers below come from loopback on one machine:** a 4-vCPU Linux VM
> (kernel 6.18), Python 3.11.15, 2026-10-02. They show the software meets
> its guarantee over a real UDP stack between real processes. They are not
> hardware or radio measurements.

Every run used `examples/field/mission_4uav.json`: 4 UAVs, 4 tasks,
heartbeat 0.1 s, peer stale 0.5 s, `heartbeat_timeout` 0.6 s, lease 1.5 s,
freshness 0.75 s, rebroadcast 0.25 s, settle 0.15 s, speed 4 m/s, and
`--duration 20`. The report's bound is 1.75 s.

| Scenario (runs) | Report | Convergence after heal | Max disagreement after convergence | Split brain at heal | Concurrent ownership (total) | Contests won by lowest ID / losers stood down | Tasks done / duplicates | Last completion |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2+2 partition 3-8 s (5) | 5/5 PASS | 9.3-12.8 ms | ≤ 2.9 ms | tasks 3, 4 in every run | 8.51-8.52 s, all inside the partition window | 10/10, 10/10 | 4/4, 0 | 12.61-12.62 s |
| 2+2 partition + `--loss 0.2` (3) | 3/3 PASS | 11.3-12.5 ms | ≤ 4.7 ms | tasks 3, 4 in every run | 8.51-8.52 s, all inside the partition window | 6/6, 6/6 | 4/4, 0 | 12.61-12.63 s |
| no partition, `--loss 0.3` (3) | 3/3 PASS | no partition | 0 ms | none | 0 s | none needed | 4/4, 0 | 14.98-15.01 s |
| fail-stop node 4 at 4 s (3) | 3/3 PASS | no partition | ≤ 4.0 ms | none | ≤ 2.5 ms (simultaneous re-claim, resolved) | 2/2, loser had not bound yet | 4/4, 0 | 15.42-15.43 s |

How to read the table:

- **Split brain is real.** In every partition run, side {1,2} saw the
  {3,4} leases expire about 0.75 s into the partition and re-claimed tasks 3
  and 4 while 3 and 4 kept flying them. That is two tasks with two live
  owners for about 4.26 s each.
- **Reconciliation.** About 10 ms after the heal, the higher IDs (3, 4)
  released and stood down, and every replica agreed.
- **Completion time.** Partition runs finish earlier (12.6 s) than
  unpartitioned ones (15.0 s). The re-claimants 1 and 2 were idle and closer.
  This is an incidental effect of lowest-ID-wins in this layout, not a
  property of the protocol.
- **Failure detection during partitions.** Each run had 1 to 4 local
  `FAILURE_DECLARED` events. The other replicas on that side mostly learned
  by applying the flooded certificate rather than declaring on their own.
  Every declaration had a 2-vote quorum, and every one was wrong in the sense
  that its target was alive across the partition. All were retracted by
  first-hand contact after the heal: 8 retractions per lossless partition run,
  2 to 6 per lossy one.
- **No false declarations on a connected network, even at 30% loss.** The
  report's `false_declarations_connected` was 0 in all 14 runs.
- **Fail-stop.** All three survivors declared node 4 after 0.512-0.516 s,
  measured from the fail-stop instant. That is less than `heartbeat_timeout`
  because the last heartbeat preceded the stop by up to one interval. Node 4
  was never retracted. Its task was re-claimed after its lease expired and
  completed by a survivor.
- **Traffic.** 5,860-6,980 datagrams in total per 20 s run for 4 nodes,
  73-87/s per node. The highest figures are from the 30% loss runs. 90-97% of
  received protocol evidence was suppressed as duplicates.

### Owner's laptop, localhost UDP, 2026-10-02

These runs were made and reported by the session working on the owner's
laptop (WSL2 Ubuntu, Python 3.11), on commit `c7b67cd`. The figures come from
that session's report; I did not reproduce them. It also reported all 151
field tests passing there.

| Scenario (runs) | Report | Tasks | Convergence after heal | Other |
| --- | --- | --- | --- | --- |
| 2+2 partition, `--duration 15` (10) | 10/10 PASS | 4/4 every run | 7.1-15.9 ms | divergent at reconnect [3, 4] every run; 6-8 false declarations per run, all retracted on heal; 0 duplicates |
| 2+2 partition + `--loss 0.2` (5) | 5/5 PASS | 4/4 | 8.3-9.1 ms | 0-4 false declarations; 0 duplicates |
| fail-stop node 4, `--duration 15` (5) | PASS, but mission incomplete | 2/4 or 3/4 | — | the run is too short for lease expiry plus the 60 m recovery leg; this is why the report now prints `mission complete` and LT4 uses 40 s |
| fail-stop node 4, `--duration 40` (5) | 5/5 PASS | 4/4 | — | fail-stop detected by all three survivors in 0.611-0.614 s; 0 duplicates |

The laptop's fail-stop detection (≈0.61 s) is about 0.1 s slower than in the
cloud VM runs (≈0.51 s). Both are within one heartbeat interval of
`heartbeat_timeout` (0.6 s). Detection is measured from the logged fail-stop
instant, and when that falls relative to the victim's last heartbeat differs
between the hosts.

### Other localhost measurements

**Localhost link probe:** 2000 probes at 50 Hz, 300 B. Round-trip delivery was
1.0. RTT p50 0.319 ms, p95 0.449 ms, p99 0.779 ms, max 7.995 ms. In
`RadioModel` terms that is `link_quality` 1.0 with BER 0. This describes the
loopback stack only.

**Broadcast mode:** four nodes on `127.255.255.255:47555` heard each other and
converged within 0.145 s of first contact. Only a smoke run was done.

**End-to-end test:** `tests/test_field_launch.py` runs four processes for
8 s with a partition from 1.0 s to 3.5 s. It asserts a split brain at the
heal, a contested stand-down, convergence within the bound and 4/4 tasks.
It takes about 10 s, and passed 6 of 6 consecutive runs on this machine.

To reproduce any row, use the commands in (a) with the same flags. Every run
writes `report.json` next to its logs.

## Decisions taken without the owner

The owner was unavailable, so these were decided here and recorded:

1. **A new single-replica detector instead of changing `FailureManager`.**
   Adapting `FailureManager` to a one-store mode would have touched a
   coordination-core module. `LocalFailureDetector` copies its rules
   instead. The cost is two implementations of one protocol, which an
   integration step could merge behind a shared core.
2. **Persistent re-offer stands in for "pending until delivered".** UDP has
   no delivery signal, and acknowledgements would add protocol state the
   simulator does not have. The re-offer is bounded: own live evidence,
   claims and releases inside one lease (newest epoch per owner only), and
   completions.
3. **`claim_settle` (150 ms) before binding.** This matches the simulator's
   same-instant claim, gossip and reconcile fixpoint as closely as a real
   network allows. It avoids moving toward a task when a lower-ID claim is
   already in flight. It must be less than `freshness_timeout`, and the
   loader enforces that.
4. **`LocalAutonomyKernel` is not wired in.** The task asked for it only as
   an option. The node runs the claim protocol directly, as the simulator's
   distributed path does. Adding the kernel would give HOLDING/ABORTED
   coordination modes and is left for integration.
5. **One envelope per datagram, JSON.** This is easy to inspect with
   `tcpdump -A` in the lab. Size and rate are well within LAN and WiFi
   budgets at this swarm size.
6. **Random loss (`--loss`) applies on receipt only,** so the flag is the
   one-hop loss probability.
7. **The report's bound is `lease_timeout + rebroadcast_interval`.** Within
   one rebroadcast interval every live claim is offered again, and any claim
   not offered again expires within one lease.

## Integration notes

Changes wanted in files owned by another workstream. None of them were made
here.

- **README.md:** add a "Lab validation (field node)" section linking this
  document, with the two commands from (a) and the localhost table clearly
  labelled. Mention the TRL4 evidence path only once hardware results exist.
- **CHANGELOG.md:** under Unreleased, "Added: lab field node — one process per
  UAV over UDP (`field_codec`, `field_transport`, `field_detector`,
  `field_mission`, `field_node`, `field_faults`, `field_launch`,
  `field_report`, `field_probe`), example scenarios in `examples/field/`,
  console scripts `eudis-field-*`."
- **docs/milestones.md:** add a milestone "Protocol validated as separate
  processes over real UDP on localhost". Add a later one for the hardware
  test card result.
- **docs/sitl.md:** note that `field_node --mavlink` drives one live vehicle
  per node through `SitlVehicle`, translating between the mission frame and
  the vehicle frame. Unlike `MavlinkBackend`, it does not step lock-step
  against vehicle time: a node runs on wall time and polls telemetry each
  round. The two paths should share one adapter once the SITL workstream
  settles its interface.
- **src/eudis_swarm/mavlink_backend.py:** `MavlinkFieldVehicle` uses only its
  `VehicleTelemetry` protocol. If that protocol changes, the field adapter
  and `tests/test_field_node.py::test_point_mass_and_mavlink_vehicles` must
  follow.
- **AGENTS.md / CONTRIBUTING.md:** worth one line saying `field_*` modules are
  non-deterministic harness code outside the simulator's import graph,
  guarded by `test_field_modules_stay_out_of_the_simulator_import_graph`.
