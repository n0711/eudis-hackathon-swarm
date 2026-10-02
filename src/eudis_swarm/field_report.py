"""Merge field-node logs and judge the swarm's coordination guarantee.

This is the observer layer for lab runs. It reads finished JSONL logs (one per
node, plus the launcher's fault record if there is one), lines them up on the
logs' wall-clock field, and evaluates what the project claims:

    Replicas may disagree while the network is partitioned. Once connectivity
    is stably restored, every replica converges on the same owner for every
    task within a bounded time, and the losing claimant stands down.

Checks (any **violation** makes the exit status 1; a run that cannot answer
the question, e.g. it ended before the bound elapsed, exits 2):

* **G1 convergence** -- within ``bound`` seconds of reconnection there is an
  instant at which every live replica holds the same view of every task (same
  owner, or all ``COMPLETE``) and none is ``CONTESTED``.
* **G2 stays converged** -- after that instant no task stays in disagreement,
  and no two live replicas both hold ``OWNED_BY_SELF``, for longer than
  ``bound`` (short windows are ordinary propagation delay of a new claim).
* **G3 stand-down** -- every concurrent-ownership episode settled by
  reconciliation was won by the lowest owner ID, and every other claimant
  released its claim and, if executing, stood down.
* **S1 silence is not failure** -- every failure declaration carries a quorum
  of ``max(2, floor((N-1)/2)+1)`` distinct voters.

``bound`` defaults to ``lease_timeout + rebroadcast_interval`` from the logged
timing: within one rebroadcast interval every live claim is re-offered, and
anything that is not re-offered expires within one lease, so no stale belief
can outlive that window on a working link.

Reconnection time is taken from the injected fault record when present (the
moment the last node lifted its drop filter), otherwise from the evidence: the
first instant after which every pair of live nodes stays mutually ``HEARD``.
The report also gives tasks completed, duplicate completions, total
concurrent-ownership time, and failure-detection behaviour.

On one machine every log shares one clock. Across hosts the wall field is only
as good as the hosts' clock synchronisation; the report says so rather than
pretending otherwise. Nothing here is ever fed back to a node.
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXIT_PASS = 0
EXIT_VIOLATION = 1
EXIT_INCONCLUSIVE = 2

_OWNER_STATES = {"OWNED_BY_SELF", "CLAIMED_BY_PEER_FRESH", "CLAIMED_BY_PEER_STALE"}


@dataclass(slots=True)
class NodeLog:
    node: int
    records: list[dict[str, Any]] = field(default_factory=list)
    walls: list[float] = field(default_factory=list)
    fail_stop_wall: float | None = None
    stop_wall: float | None = None
    timing: dict[str, float] = field(default_factory=dict)
    agents: list[int] = field(default_factory=list)
    required_votes: int | None = None

    @property
    def start(self) -> float:
        return self.walls[0]

    @property
    def end(self) -> float:
        if self.fail_stop_wall is not None:
            return self.fail_stop_wall
        return self.walls[-1]

    def live_at(self, wall: float) -> bool:
        return self.start <= wall <= self.end

    def record_at(self, wall: float) -> dict[str, Any] | None:
        index = bisect_right(self.walls, wall) - 1
        return None if index < 0 else self.records[index]


@dataclass(slots=True)
class LoadedLogs:
    nodes: dict[int, NodeLog]
    launcher: list[dict[str, Any]]
    malformed_lines: int


def _iter_paths(paths: Iterable[str | Path]) -> list[Path]:
    found: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            found.extend(sorted(path.glob("*.jsonl")))
        else:
            found.append(path)
    return found


def load_logs(paths: Iterable[str | Path]) -> LoadedLogs:
    """Read every record; malformed lines are counted, never fatal."""

    nodes: dict[int, NodeLog] = {}
    launcher: list[dict[str, Any]] = []
    malformed = 0
    for path in _iter_paths(paths):
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    # a node killed mid-write leaves at most one torn line.
                    malformed += 1
                    continue
                if not isinstance(record, dict):
                    malformed += 1
                    continue
                if record.get("source") == "launcher":
                    launcher.append(record)
                    continue
                node = record.get("node")
                wall = record.get("wall")
                if (
                    not isinstance(node, int)
                    or not isinstance(wall, (int, float))
                    or not isinstance(record.get("ownership"), dict)
                    or not isinstance(record.get("peers"), dict)
                ):
                    malformed += 1
                    continue
                nodes.setdefault(node, NodeLog(node=node)).records.append(record)
    for log in nodes.values():
        log.records.sort(key=lambda item: (item["wall"], item.get("seq", 0)))
        log.walls = [float(item["wall"]) for item in log.records]
        for record in log.records:
            kind = record.get("kind")
            if kind == "NODE_START":
                log.timing = dict(record.get("timing") or {})
                log.agents = list(record.get("agents") or [])
                log.required_votes = record.get("required_votes")
            elif kind == "FAIL_STOP" and log.fail_stop_wall is None:
                log.fail_stop_wall = float(record["wall"])
            elif kind == "NODE_STOP":
                log.stop_wall = float(record["wall"])
    launcher.sort(key=lambda item: item.get("wall", 0.0))
    return LoadedLogs(nodes=nodes, launcher=launcher, malformed_lines=malformed)


def task_value(view: object) -> tuple[Any, ...]:
    """Collapse one replica's task view to what replicas must agree on."""

    if not isinstance(view, dict):
        return ("UNKNOWN",)
    state = view.get("state")
    if state in _OWNER_STATES:
        return ("OWNER", view.get("owner"))
    return (str(state),)


@dataclass(frozen=True, slots=True)
class Episode:
    """An interval in which some property held for one task."""

    task: int
    start: float
    end: float
    nodes: tuple[int, ...]
    ongoing: bool

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(slots=True)
class Report:
    passed: bool = True
    inconclusive: bool = False
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def violate(self, message: str) -> None:
        self.passed = False
        self.violations.append(message)

    @property
    def exit_code(self) -> int:
        if not self.passed:
            return EXIT_VIOLATION
        if self.inconclusive:
            return EXIT_INCONCLUSIVE
        return EXIT_PASS


class _Timeline:
    """Piecewise-constant replica beliefs, sampled at every logged instant."""

    def __init__(self, logs: LoadedLogs) -> None:
        self.logs = logs
        self.nodes = logs.nodes
        self.instants = sorted(
            {wall for log in self.nodes.values() for wall in log.walls}
        )
        task_ids: set[int] = set()
        for log in self.nodes.values():
            for record in log.records:
                task_ids.update(int(task) for task in record["ownership"])
        self.task_ids = tuple(sorted(task_ids))

    def live(self, wall: float) -> list[NodeLog]:
        return [log for _, log in sorted(self.nodes.items()) if log.live_at(wall)]

    def views(self, wall: float) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for log in self.live(wall):
            record = log.record_at(wall)
            if record is not None:
                result[log.node] = record
        return result

    def agreement(self, wall: float) -> tuple[bool, list[int]]:
        """Return whether all live replicas agree, and the tasks they do not."""

        views = self.views(wall)
        disagreeing: list[int] = []
        for task in self.task_ids:
            values = {
                task_value(record["ownership"].get(str(task)))
                for record in views.values()
            }
            if len(values) != 1 or ("CONTESTED",) in values:
                disagreeing.append(task)
        return (not disagreeing and bool(views), disagreeing)

    def episodes(self, predicate_name: str) -> list[Episode]:
        """Intervals of per-task disagreement or of concurrent self-ownership."""

        open_since: dict[int, tuple[float, tuple[int, ...]]] = {}
        found: list[Episode] = []
        last = self.instants[-1] if self.instants else 0.0
        for wall in self.instants:
            views = self.views(wall)
            for task in self.task_ids:
                key = str(task)
                if predicate_name == "concurrent":
                    holders = tuple(
                        sorted(
                            node
                            for node, record in views.items()
                            if (record["ownership"].get(key) or {}).get("state")
                            == "OWNED_BY_SELF"
                        )
                    )
                    active = len(holders) >= 2
                else:
                    values = {
                        task_value(record["ownership"].get(key))
                        for record in views.values()
                    }
                    holders = tuple(sorted(views))
                    active = len(values) > 1 or ("CONTESTED",) in values
                if active and task not in open_since:
                    open_since[task] = (wall, holders)
                elif active:
                    since, previous = open_since[task]
                    merged = tuple(sorted(set(previous) | set(holders)))
                    open_since[task] = (since, merged)
                elif task in open_since:
                    since, holders_seen = open_since.pop(task)
                    found.append(Episode(task, since, wall, holders_seen, False))
        for task, (since, holders_seen) in open_since.items():
            found.append(Episode(task, since, last, holders_seen, True))
        found.sort(key=lambda item: (item.start, item.task))
        return found


def _events(logs: LoadedLogs, kind: str) -> list[dict[str, Any]]:
    return sorted(
        (
            record
            for log in logs.nodes.values()
            for record in log.records
            if record.get("kind") == kind
        ),
        key=lambda item: item["wall"],
    )


def _injected_windows(
    logs: LoadedLogs,
) -> tuple[list[tuple[float, float]], float | None]:
    """Partition windows and final heal time from the injected-fault record.

    Uses each node's own ``FILTER_CHANGED`` record (when it actually applied the
    filter); a window is open while any node blocks any peer.
    """

    changes: list[tuple[float, int, bool]] = []
    for log in logs.nodes.values():
        for record in log.records:
            if record.get("kind") == "FILTER_CHANGED":
                changes.append((record["wall"], log.node, bool(record.get("blocked"))))
    if not changes:
        return [], None
    changes.sort()
    blocking: set[int] = set()
    windows: list[tuple[float, float]] = []
    opened: float | None = None
    for wall, node, blocked in changes:
        if blocked:
            blocking.add(node)
        else:
            blocking.discard(node)
        if blocking and opened is None:
            opened = wall
        elif not blocking and opened is not None:
            windows.append((opened, wall))
            opened = None
    if opened is not None:
        return windows + [(opened, float("inf"))], None
    return windows, (windows[-1][1] if windows else None)


def _observed_reconnect(timeline: _Timeline) -> float | None:
    """First instant after which every live pair stays mutually HEARD."""

    survivors = [log for log in timeline.nodes.values() if log.fail_stop_wall is None]
    if len(survivors) < 2:
        return None
    survivor_ids = {log.node for log in survivors}
    latest = max(log.start for log in survivors)
    for log in survivors:
        for peer in survivor_ids - {log.node}:
            heard_since: float | None = None
            for record in log.records:
                status = record["peers"].get(str(peer))
                if status == "HEARD":
                    if heard_since is None:
                        heard_since = record["wall"]
                else:
                    heard_since = None
            if heard_since is None:
                return None
            latest = max(latest, heard_since)
    return latest


def _quorum(agent_count: int) -> int:
    return max(2, (agent_count - 1) // 2 + 1)


def evaluate(
    logs: LoadedLogs,
    *,
    bound: float | None = None,
    reconnect_at: float | None = None,
) -> Report:
    """Evaluate the guarantee over loaded logs."""

    report = Report()
    if len(logs.nodes) < 2:
        report.inconclusive = True
        report.notes.append("fewer than two node logs: nothing to compare")
        report.summary["nodes"] = sorted(logs.nodes)
        return report
    timeline = _Timeline(logs)
    any_log = next(iter(logs.nodes.values()))
    timing = any_log.timing
    if bound is None:
        lease = float(timing.get("lease_timeout", 1.5))
        rebroadcast = float(timing.get("rebroadcast_interval", 0.25))
        bound = lease + rebroadcast
    agent_count = len(any_log.agents) or len(logs.nodes)
    origin = min(log.start for log in logs.nodes.values())
    observation_end = max(log.end for log in logs.nodes.values())

    def rel(wall: float | None) -> float | None:
        return None if wall is None else round(wall - origin, 4)

    summary = report.summary
    summary["nodes"] = sorted(logs.nodes)
    summary["bound_s"] = bound
    summary["malformed_lines"] = logs.malformed_lines
    summary["observation_s"] = round(observation_end - origin, 4)
    summary["fail_stopped"] = {
        str(log.node): rel(log.fail_stop_wall)
        for log in logs.nodes.values()
        if log.fail_stop_wall is not None
    }

    windows, injected_heal = _injected_windows(logs)
    summary["partition_windows_s"] = [[rel(a), rel(b)] for a, b in windows]
    observed = _observed_reconnect(timeline)
    summary["reconnect_observed_s"] = rel(observed)
    summary["reconnect_injected_s"] = rel(injected_heal)
    if reconnect_at is not None:
        reconnect = reconnect_at
        source = "given"
    elif windows:
        reconnect = injected_heal
        source = "injected"
    else:
        reconnect = observed
        source = "observed"
    summary["reconnect_source"] = source
    summary["reconnect_s"] = rel(reconnect)

    # --- completions -----------------------------------------------------
    completions = _events(logs, "TASK_COMPLETED")
    completers: dict[int, list[int]] = {}
    for record in completions:
        completers.setdefault(int(record["task"]), []).append(int(record["node"]))
    duplicates = {
        str(task): sorted(set(nodes))
        for task, nodes in sorted(completers.items())
        if len(set(nodes)) > 1
    }
    summary["tasks_total"] = len(timeline.task_ids)
    summary["tasks_completed"] = len(completers)
    summary["completion_events"] = len(completions)
    summary["duplicate_completions"] = duplicates
    summary["last_completion_s"] = rel(completions[-1]["wall"]) if completions else None

    final_views = {
        log.node: log.records[-1]
        for log in logs.nodes.values()
        if log.fail_stop_wall is None
    }
    summary["final_complete_per_node"] = {
        str(node): sum(
            1
            for view in record["ownership"].values()
            if isinstance(view, dict) and view.get("state") == "COMPLETE"
        )
        for node, record in sorted(final_views.items())
    }

    # --- concurrent ownership -------------------------------------------
    concurrent = timeline.episodes("concurrent")
    disagreements = timeline.episodes("disagreement")

    def in_partition(episode: Episode) -> bool:
        return any(a <= episode.end and episode.start <= b for a, b in windows)

    summary["concurrent_ownership_total_s"] = round(
        sum(item.duration for item in concurrent), 4
    )
    summary["concurrent_ownership_episodes"] = [
        {
            "task": item.task,
            "start_s": rel(item.start),
            "duration_s": round(item.duration, 4),
            "owners": list(item.nodes),
            "overlaps_partition": in_partition(item),
            "ongoing": item.ongoing,
        }
        for item in concurrent
    ]
    connected_concurrent = [item for item in concurrent if not in_partition(item)]
    summary["concurrent_ownership_connected_max_s"] = round(
        max((item.duration for item in connected_concurrent), default=0.0), 4
    )

    # --- G1 / G2 ---------------------------------------------------------
    if reconnect is None:
        report.inconclusive = True
        report.notes.append(
            "no stable reconnection: some pair of live nodes never stayed HEARD"
        )
    else:
        converged_at: float | None = None
        divergent_at_reconnect: list[int] = []
        for wall in [reconnect] + [t for t in timeline.instants if t > reconnect]:
            agreed, disagreeing = timeline.agreement(wall)
            if wall == reconnect:
                divergent_at_reconnect = disagreeing
            if agreed:
                converged_at = wall
                break
        summary["divergent_tasks_at_reconnect"] = divergent_at_reconnect
        if converged_at is None:
            summary["convergence_s"] = None
            if observation_end - reconnect < bound:
                report.inconclusive = True
                report.notes.append(
                    "run ended before the convergence bound elapsed after reconnect"
                )
            else:
                report.violate(
                    f"G1: replicas never converged after reconnect at "
                    f"t={rel(reconnect)}s"
                )
        else:
            convergence = converged_at - reconnect
            summary["convergence_s"] = round(convergence, 4)
            if convergence > bound:
                report.violate(
                    f"G1: convergence took {convergence:.3f}s > bound {bound:.3f}s"
                )
            late_disagreements = [
                item
                for item in disagreements
                if item.end > converged_at and item.duration > bound
            ]
            late_concurrent = [
                item
                for item in concurrent
                if item.end > converged_at and item.duration > bound
            ]
            summary["max_disagreement_after_convergence_s"] = round(
                max(
                    (
                        item.end - max(item.start, converged_at)
                        for item in disagreements
                        if item.end > converged_at
                    ),
                    default=0.0,
                ),
                4,
            )
            for item in late_disagreements:
                report.violate(
                    f"G2: task {item.task} replicas disagreed for "
                    f"{item.duration:.3f}s after convergence"
                )
            for item in late_concurrent:
                report.violate(
                    f"G2: task {item.task} had concurrent owners {list(item.nodes)} "
                    f"for {item.duration:.3f}s after convergence"
                )

    # --- G3 stand-down ---------------------------------------------------
    reconciled = _events(logs, "RECONCILED")
    stood_down = _events(logs, "TASK_STOOD_DOWN")
    resolutions: list[dict[str, Any]] = []
    for item in concurrent:
        if item.ongoing:
            continue
        window_end = item.end + bound
        yielded = [
            record
            for record in reconciled
            if int(record["task"]) == item.task
            and record.get("yielded")
            and item.start <= record["wall"] <= window_end
            and int(record["node"]) in item.nodes
        ]
        if not yielded:
            resolutions.append(
                {"task": item.task, "start_s": rel(item.start), "resolution": "other"}
            )
            continue
        expected_winner = min(item.nodes)
        winners = sorted({int(record["winner"]) for record in yielded})
        yielders = sorted({int(record["node"]) for record in yielded})
        if winners != [expected_winner] or expected_winner in yielders:
            report.violate(
                f"G3: task {item.task} contest among {list(item.nodes)} resolved "
                f"to {winners} (yielders {yielders}); expected lowest ID "
                f"{expected_winner}"
            )
        for record in yielded:
            if record.get("current_task") != item.task:
                # a claimant that had not bound yet has nothing to stand down from.
                continue
            node = int(record["node"])
            if not any(
                int(other["node"]) == node
                and int(other["task"]) == item.task
                and record["wall"] <= other["wall"] <= window_end
                for other in stood_down
            ):
                report.violate(
                    f"G3: node {node} yielded task {item.task} while executing it "
                    "but never stood down"
                )
        resolutions.append(
            {
                "task": item.task,
                "start_s": rel(item.start),
                "resolution": "reconciled",
                "winner": expected_winner,
                "yielded": yielders,
            }
        )
    summary["contest_resolutions"] = resolutions
    summary["stand_downs"] = [
        {
            "node": int(record["node"]),
            "task": int(record["task"]),
            "t_s": rel(record["wall"]),
            "contested": bool(record.get("contested")),
        }
        for record in stood_down
    ]

    # --- failure detection / S1 -----------------------------------------
    required = _quorum(agent_count)
    declared = _events(logs, "FAILURE_DECLARED")
    applied = _events(logs, "FAILURE_DECLARATION_APPLIED")
    retracted = _events(logs, "FAILURE_RETRACTED")
    detection: list[dict[str, Any]] = []
    for record in declared:
        voters = sorted(set(record.get("voters") or []))
        peer = int(record["peer"])
        target_log = logs.nodes.get(peer)
        alive = target_log is not None and target_log.live_at(record["wall"])
        if len(voters) < required or record.get("required_votes") != required:
            report.violate(
                f"S1: node {record['node']} declared {peer} failed with voters "
                f"{voters} (quorum {required})"
            )
        detection.append(
            {
                "declarer": int(record["node"]),
                "peer": peer,
                "t_s": rel(record["wall"]),
                "voters": voters,
                "target_actually_alive": alive,
            }
        )
    summary["quorum"] = required
    summary["declarations"] = detection
    summary["declarations_applied_from_peers"] = len(applied)
    summary["false_declarations"] = sum(
        1 for item in detection if item["target_actually_alive"]
    )
    # a wrong declaration while partitioned is the expected cost of silence
    # being ambiguous; one on a connected network measures link loss.
    summary["false_declarations_connected"] = sum(
        1
        for item, record in zip(detection, declared)
        if item["target_actually_alive"]
        and not any(a <= record["wall"] <= b for a, b in windows)
    )
    summary["retractions"] = [
        {
            "node": int(record["node"]),
            "peer": int(record["peer"]),
            "t_s": rel(record["wall"]),
        }
        for record in retracted
    ]
    latencies: dict[str, dict[str, float | None]] = {}
    for log in logs.nodes.values():
        if log.fail_stop_wall is None:
            continue
        per_survivor: dict[str, float | None] = {}
        for other in logs.nodes.values():
            if other.node == log.node or other.fail_stop_wall is not None:
                continue
            first = next(
                (
                    record["wall"]
                    for record in other.records
                    if record.get("kind")
                    in ("FAILURE_DECLARED", "FAILURE_DECLARATION_APPLIED")
                    and record.get("peer") == log.node
                    and record["wall"] >= log.fail_stop_wall
                ),
                None,
            )
            per_survivor[str(other.node)] = (
                None if first is None else round(first - log.fail_stop_wall, 4)
            )
        latencies[str(log.node)] = per_survivor
    summary["fail_stop_detection_latency_s"] = latencies
    return report


def format_report(report: Report) -> str:
    """Human-readable summary."""

    summary = report.summary
    verdict = {
        EXIT_PASS: "PASS",
        EXIT_VIOLATION: "VIOLATED",
        EXIT_INCONCLUSIVE: "INCONCLUSIVE",
    }[report.exit_code]
    lines = [
        f"field report: {verdict}",
        f"  nodes                      {summary.get('nodes')}",
        f"  observation                {summary.get('observation_s')} s",
        f"  partition windows          {summary.get('partition_windows_s')}",
        f"  reconnect ({summary.get('reconnect_source')})"
        f"{'':<10}{summary.get('reconnect_s')} s",
        f"  divergent at reconnect     {summary.get('divergent_tasks_at_reconnect')}",
        f"  convergence after reconnect {summary.get('convergence_s')} s "
        f"(bound {summary.get('bound_s')} s)",
        f"  max disagreement after     {summary.get('max_disagreement_after_convergence_s')} s",
        f"  tasks completed            {summary.get('tasks_completed')}/"
        f"{summary.get('tasks_total')}",
        f"  last completion            {summary.get('last_completion_s')} s",
        f"  duplicate completions      {summary.get('duplicate_completions')}",
        f"  concurrent ownership total {summary.get('concurrent_ownership_total_s')} s",
        f"  concurrent (connected) max {summary.get('concurrent_ownership_connected_max_s')} s",
        f"  contest resolutions        {summary.get('contest_resolutions')}",
        f"  declarations (false)       {len(summary.get('declarations') or [])} "
        f"({summary.get('false_declarations')}; "
        f"{summary.get('false_declarations_connected')} outside partitions), "
        f"quorum {summary.get('quorum')}",
        f"  retractions                {len(summary.get('retractions') or [])}",
        f"  fail-stop detection        {summary.get('fail_stop_detection_latency_s')}",
    ]
    for message in report.violations:
        lines.append(f"  VIOLATION: {message}")
    for message in report.notes:
        lines.append(f"  note: {message}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eudis_swarm.field_report",
        description="Merge field-node logs and check the convergence guarantee.",
    )
    parser.add_argument("paths", nargs="+", help="JSONL logs or directories")
    parser.add_argument("--bound", type=float, help="convergence bound in seconds")
    parser.add_argument(
        "--reconnect-at", type=float, help="override reconnection wall time"
    )
    parser.add_argument("--json", help="also write the summary as JSON here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = evaluate(
        load_logs(args.paths), bound=args.bound, reconnect_at=args.reconnect_at
    )
    print(format_report(report))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "exit_code": report.exit_code,
                    "violations": report.violations,
                    "notes": report.notes,
                    "summary": report.summary,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return report.exit_code


__all__ = [
    "EXIT_INCONCLUSIVE",
    "EXIT_PASS",
    "EXIT_VIOLATION",
    "LoadedLogs",
    "NodeLog",
    "Report",
    "evaluate",
    "format_report",
    "load_logs",
    "main",
    "task_value",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
