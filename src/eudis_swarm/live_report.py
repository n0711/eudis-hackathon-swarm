"""Turn evidence logs from ``live_mission`` into a side-by-side comparison.

Every number here is derived from the JSON-lines records alone, so a log
written by a lab run on a laptop can be summarised later, anywhere, with no
vehicle and no ``sitl`` extra installed::

    python scripts/sitl_swarm_report.py evidence.jsonl

Runs are grouped by their ``run`` label. A run whose header says
``deterministic: false`` is labelled as such in every table this prints; its
numbers belong in docs/sitl.md, never in docs/measured_results.md.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .live_mission import LOG_SCHEMA_VERSION, Record, read_jsonl

#: Default 3-D distance below which two live vehicles count as too close.
DEFAULT_SEPARATION_THRESHOLD_M = 5.0

#: Ownership states in which a receiver counts a task as handled.
_HANDLED_STATES = ("OWNED_BY_SELF", "COMPLETE")


@dataclass(frozen=True, slots=True)
class VehicleStats:
    agent_id: int
    steps: int
    max_drift_s: float
    mean_drift_s: float
    max_wait_wall_s: float


@dataclass(frozen=True, slots=True)
class FailStopSummary:
    agent_id: int
    logical_time: float
    mode: str
    confirmed: bool
    #: How far the held vehicle moved after the hold command, by the last
    #: observation in the log. ``None`` when it was never observed again.
    displacement_after_hold_m: float | None
    #: Vehicle seconds between the hold command and that last observation.
    observed_for_s: float | None
    #: Ground speed between the last two held observations: whether the
    #: aircraft had actually stopped by the end of the log.
    residual_speed_mps: float | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    run: str
    backend: str
    deterministic: bool
    agent_count: int
    integrated_agent_ids: tuple[int, ...]
    mission_completed: bool
    completed_task_count: int
    total_task_count: int
    mission_time_s: float
    concurrent_ownership_task_s: float
    failed_agent_id: int | None
    failure_time_s: float | None
    failure_detection_latency_s: float | None
    #: Tasks the failed UAV owned (in its own replica) when it failed.
    orphaned_task_ids: tuple[int, ...]
    #: Failure until every orphaned task was owned-by-self or complete in some
    #: surviving receiver's replica. ``None``: nothing orphaned or never.
    recovery_latency_s: float | None
    recovered: bool
    vehicles: tuple[VehicleStats, ...] = field(default=())
    fail_stops: tuple[FailStopSummary, ...] = field(default=())
    #: Agent id -> "copter" / "rover", from the header (empty for point-mass).
    vehicle_types: tuple[tuple[int, str], ...] = field(default=())
    #: Smallest pairwise 3-D distance between live vehicles over the mission,
    #: from observed east/north/altitude. ``None`` without two such vehicles.
    min_separation_m: float | None = None
    #: Which pair, and when, that minimum occurred.
    min_separation_pair: tuple[int, int] | None = None
    min_separation_t: float | None = None
    #: Steps whose minimum pairwise separation was below the threshold.
    separation_threshold_m: float = DEFAULT_SEPARATION_THRESHOLD_M
    steps_below_separation: int = 0
    separation_steps: int = 0


def _group(records: Sequence[Record]) -> dict[str, list[Record]]:
    runs: dict[str, list[Record]] = {}
    for record in records:
        runs.setdefault(str(record.get("run")), []).append(record)
    return runs


def _single(records: Sequence[Record], kind: str, run: str) -> Record:
    matches = [record for record in records if record["type"] == kind]
    if len(matches) != 1:
        raise ValueError(
            f"run {run!r}: expected one {kind} record, found {len(matches)}"
        )
    return matches[0]


def _failure(steps: Sequence[Record]) -> tuple[int | None, float | None]:
    for step in steps:
        for event in step["events"]:
            if event["kind"] == "FAILURE_INJECTED":
                return event["agent_id"], float(event["t"])
    return None, None


def _ownership_row(step: Record, receiver_id: int) -> dict[int, tuple[str, Any]]:
    rows = step["ownership"].get(str(receiver_id), [])
    return {int(task_id): (state, owner) for task_id, state, owner in rows}


def _recovery(
    steps: Sequence[Record], failed_agent_id: int, failure_time: float
) -> tuple[tuple[int, ...], float | None, bool]:
    at_failure = next(
        (step for step in steps if float(step["t"]) >= failure_time - 1e-9), None
    )
    if at_failure is None:
        return (), None, False
    orphaned = tuple(
        sorted(
            task_id
            for task_id, (state, _owner) in _ownership_row(
                at_failure, failed_agent_id
            ).items()
            if state == "OWNED_BY_SELF"
        )
    )
    if not orphaned:
        return (), None, False
    pending = set(orphaned)
    for step in steps:
        t = float(step["t"])
        if t < failure_time - 1e-9:
            continue
        survivors = [
            agent["id"]
            for agent in step["agents"]
            if agent["responsive"] and agent["id"] != failed_agent_id
        ]
        for receiver_id in survivors:
            row = _ownership_row(step, receiver_id)
            for task_id in tuple(pending):
                state, _owner = row.get(task_id, ("UNCLAIMED", None))
                if state in _HANDLED_STATES:
                    pending.discard(task_id)
        if not pending:
            return orphaned, t - failure_time, True
    return orphaned, None, False


def _vehicle_stats(steps: Sequence[Record]) -> tuple[VehicleStats, ...]:
    by_agent: dict[int, list[Record]] = {}
    for step in steps:
        for vehicle in step["vehicles"]:
            if not vehicle["held"]:
                by_agent.setdefault(int(vehicle["id"]), []).append(vehicle)
    stats = []
    for agent_id in sorted(by_agent):
        samples = by_agent[agent_id]
        drifts = [float(sample["drift_s"]) for sample in samples]
        waits = [float(sample["waited_wall_s"]) for sample in samples]
        stats.append(
            VehicleStats(
                agent_id=agent_id,
                steps=len(samples),
                max_drift_s=max(drifts),
                mean_drift_s=sum(drifts) / len(drifts),
                max_wait_wall_s=max(waits),
            )
        )
    return tuple(stats)


def step_separation(step: Record) -> tuple[float, tuple[int, int]] | None:
    """Minimum pairwise 3-D distance between the vehicles observed in a step.

    A vehicle may appear twice in the step at which it is fail-stopped (its
    last lock-step sample and its first held observation); the later record
    wins. Vehicles without an observed altitude are left out.
    """

    latest: dict[int, tuple[float, float, float]] = {}
    for vehicle in step["vehicles"]:
        altitude = vehicle.get("altitude_m")
        if altitude is None:
            continue
        east, north = vehicle["position"]
        latest[int(vehicle["id"])] = (float(east), float(north), float(altitude))
    ids = sorted(latest)
    best: tuple[float, tuple[int, int]] | None = None
    for index, first in enumerate(ids):
        for second in ids[index + 1 :]:
            distance = math.dist(latest[first], latest[second])
            if best is None or distance < best[0]:
                best = (distance, (first, second))
    return best


def _separation(
    steps: Sequence[Record], threshold: float
) -> tuple[float | None, tuple[int, int] | None, float | None, int, int]:
    minimum: float | None = None
    pair: tuple[int, int] | None = None
    at: float | None = None
    below = 0
    counted = 0
    for step in steps:
        result = step_separation(step)
        if result is None:
            continue
        counted += 1
        distance, which = result
        if distance < threshold:
            below += 1
        if minimum is None or distance < minimum:
            minimum, pair, at = distance, which, float(step["t"])
    return minimum, pair, at, below, counted


def _fail_stops(steps: Sequence[Record]) -> tuple[FailStopSummary, ...]:
    summaries = []
    for step in steps:
        for event in step["events"]:
            if event["kind"] != "FAIL_STOP":
                continue
            agent_id = int(event["agent_id"])
            held_at = event["position"]
            held = [
                vehicle
                for later in steps
                for vehicle in later["vehicles"]
                if vehicle["held"] and int(vehicle["id"]) == agent_id
            ]
            last = held[-1] if held else None
            displacement = None
            observed_for = None
            residual = None
            if len(held) >= 2:
                before, final = held[-2], held[-1]
                interval = float(final["vehicle_time_s"]) - float(
                    before["vehicle_time_s"]
                )
                if interval > 0.0:
                    residual = (
                        math.hypot(
                            final["position"][0] - before["position"][0],
                            final["position"][1] - before["position"][1],
                        )
                        / interval
                    )
            if last is not None:
                displacement = math.hypot(
                    last["position"][0] - held_at[0], last["position"][1] - held_at[1]
                )
                observed_for = float(last["vehicle_time_s"]) - float(
                    event["vehicle_time_s"]
                )
            summaries.append(
                FailStopSummary(
                    agent_id=agent_id,
                    logical_time=float(event["t"]),
                    mode=str(event["mode"]),
                    confirmed=bool(event["confirmed"]),
                    displacement_after_hold_m=displacement,
                    observed_for_s=observed_for,
                    residual_speed_mps=residual,
                )
            )
    return tuple(summaries)


def summarize_run(
    run: str,
    records: Sequence[Record],
    *,
    separation_threshold_m: float = DEFAULT_SEPARATION_THRESHOLD_M,
) -> RunSummary:
    header = _single(records, "header", run)
    if header.get("schema") != LOG_SCHEMA_VERSION:
        raise ValueError(
            f"run {run!r}: log schema {header.get('schema')!r}, "
            f"this report reads {LOG_SCHEMA_VERSION}"
        )
    result = _single(records, "result", run)
    steps = [record for record in records if record["type"] == "step"]
    failed_agent_id, failure_time = _failure(steps)
    orphaned: tuple[int, ...] = ()
    recovery: float | None = None
    recovered = False
    detection: float | None = None
    if failed_agent_id is not None and failure_time is not None:
        orphaned, recovery, recovered = _recovery(steps, failed_agent_id, failure_time)
        detection = result["failure_detection_latency_s"].get(str(failed_agent_id))
    minimum, pair, at, below, counted = _separation(steps, separation_threshold_m)
    return RunSummary(
        run=run,
        backend=str(header["backend"]),
        deterministic=bool(header["deterministic"]),
        agent_count=len(header["agent_ids"]),
        integrated_agent_ids=tuple(header["integrated_agent_ids"]),
        mission_completed=bool(result["mission_completed"]),
        completed_task_count=int(result["completed_task_count"]),
        total_task_count=int(result["total_task_count"]),
        mission_time_s=float(result["mission_time_s"]),
        concurrent_ownership_task_s=float(result["concurrent_ownership_task_s"]),
        failed_agent_id=failed_agent_id,
        failure_time_s=failure_time,
        failure_detection_latency_s=detection,
        orphaned_task_ids=orphaned,
        recovery_latency_s=recovery,
        recovered=recovered,
        vehicles=_vehicle_stats(steps),
        fail_stops=_fail_stops(steps),
        vehicle_types=tuple(
            sorted(
                (int(agent_id), str(kind))
                for agent_id, kind in header.get("vehicle_types", {}).items()
            )
        ),
        min_separation_m=minimum,
        min_separation_pair=pair,
        min_separation_t=at,
        separation_threshold_m=separation_threshold_m,
        steps_below_separation=below,
        separation_steps=counted,
    )


def summarize(
    records: Sequence[Record],
    *,
    separation_threshold_m: float = DEFAULT_SEPARATION_THRESHOLD_M,
) -> list[RunSummary]:
    """One summary per run label, in the order the runs first appear."""

    if not separation_threshold_m > 0.0:
        raise ValueError("separation threshold must be positive")
    return [
        summarize_run(run, items, separation_threshold_m=separation_threshold_m)
        for run, items in _group(records).items()
    ]


def _separation_cell(summary: RunSummary) -> str:
    if summary.min_separation_m is None or summary.min_separation_pair is None:
        return "n/a"
    first, second = summary.min_separation_pair
    return (
        f"{summary.min_separation_m:.1f} m (UAV {first}-{second} at "
        f"{summary.min_separation_t:.2f} s); {summary.steps_below_separation}/"
        f"{summary.separation_steps} steps < {summary.separation_threshold_m:g} m"
    )


def _types_cell(summary: RunSummary) -> str:
    if not summary.vehicle_types:
        return "n/a"
    return ", ".join(f"{agent}:{kind}" for agent, kind in summary.vehicle_types)


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f} s"


def _recovery_cell(summary: RunSummary) -> str:
    if summary.failed_agent_id is None:
        return "no failure"
    if not summary.orphaned_task_ids:
        return "nothing orphaned"
    if not summary.recovered:
        return "NOT recovered"
    return _seconds(summary.recovery_latency_s)


def format_comparison(summaries: Sequence[RunSummary]) -> str:
    """A Markdown table, one column per run, with a non-determinism banner."""

    if not summaries:
        raise ValueError("no runs to compare")
    header = (
        "| | "
        + " | ".join(
            f"{summary.run} ({'deterministic' if summary.deterministic else 'NON-deterministic'})"
            for summary in summaries
        )
        + " |"
    )
    divider = "| --- |" + " ---: |" * len(summaries)
    rows: list[tuple[str, list[str]]] = [
        ("Backend", [summary.backend for summary in summaries]),
        (
            "Live-telemetry agents",
            [
                ", ".join(str(item) for item in summary.integrated_agent_ids) or "none"
                for summary in summaries
            ],
        ),
        ("Vehicle types", [_types_cell(s) for s in summaries]),
        (
            "Tasks completed",
            [
                f"{summary.completed_task_count}/{summary.total_task_count}"
                for summary in summaries
            ],
        ),
        (
            "Mission completed",
            ["yes" if s.mission_completed else "NO" for s in summaries],
        ),
        ("Mission time (logical)", [_seconds(s.mission_time_s) for s in summaries]),
        (
            "Physical failure",
            [
                "none"
                if s.failed_agent_id is None
                else f"UAV {s.failed_agent_id} at {_seconds(s.failure_time_s)}"
                for s in summaries
            ],
        ),
        (
            "Failure detection latency",
            [_seconds(s.failure_detection_latency_s) for s in summaries],
        ),
        (
            "Orphaned tasks",
            [
                ", ".join(str(task) for task in s.orphaned_task_ids) or "none"
                for s in summaries
            ],
        ),
        ("Recovery latency after failure", [_recovery_cell(s) for s in summaries]),
        (
            "Concurrent-ownership time",
            [f"{s.concurrent_ownership_task_s:.2f} task-s" for s in summaries],
        ),
        ("Min 3-D separation (live)", [_separation_cell(s) for s in summaries]),
        (
            "Max lock-step drift (vehicle ahead)",
            [
                "n/a"
                if not s.vehicles
                else f"{max(v.max_drift_s for v in s.vehicles):.3f} s"
                for s in summaries
            ],
        ),
        (
            "Fail-stop",
            [
                "n/a"
                if s.backend == "point-mass"
                else (
                    "; ".join(
                        f"UAV {f.agent_id} {f.mode} at {f.logical_time:.2f} s "
                        f"({'confirmed' if f.confirmed else 'UNCONFIRMED'}"
                        + (
                            ""
                            if f.displacement_after_hold_m is None
                            else f", moved {f.displacement_after_hold_m:.1f} m "
                            f"over {f.observed_for_s:.1f} s after"
                        )
                        + (
                            ""
                            if f.residual_speed_mps is None
                            else f", final speed {f.residual_speed_mps:.2f} m/s"
                        )
                        + ")"
                        for f in s.fail_stops
                    )
                    or "none sent"
                )
                for s in summaries
            ],
        ),
    ]
    lines = [header, divider]
    lines.extend(f"| {label} | " + " | ".join(cells) + " |" for label, cells in rows)
    if any(not summary.deterministic for summary in summaries):
        lines.append("")
        lines.append(
            "NON-deterministic: a live-telemetry run is not reproducible. Record it "
            "in docs/sitl.md, never in docs/measured_results.md."
        )
    return "\n".join(lines)


def format_vehicle_table(summary: RunSummary) -> str:
    if not summary.vehicles:
        return f"{summary.run}: no live vehicles"
    lines = [
        f"{summary.run}: lock-step per vehicle",
        "| UAV | steps | max drift | mean drift | max wait (wall) |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {v.agent_id} | {v.steps} | {v.max_drift_s:.3f} s | "
        f"{v.mean_drift_s:.3f} s | {v.max_wait_wall_s:.3f} s |"
        for v in summary.vehicles
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarise live_mission evidence logs into a comparison table."
    )
    parser.add_argument("logs", nargs="+", help="JSON-lines evidence log(s)")
    parser.add_argument(
        "--separation-threshold",
        type=float,
        default=DEFAULT_SEPARATION_THRESHOLD_M,
        help="3-D metres below which two vehicles count as too close (default 5)",
    )
    arguments = parser.parse_args(argv)
    records: list[Record] = []
    for path in arguments.logs:
        with open(path, encoding="utf-8") as stream:
            records.extend(read_jsonl(stream))
    try:
        summaries = summarize(
            records, separation_threshold_m=arguments.separation_threshold
        )
        print(format_comparison(summaries))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for summary in summaries:
        if summary.vehicles:
            print()
            print(format_vehicle_table(summary))
    return 0
