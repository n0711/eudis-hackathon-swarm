"""The observer report on synthetic logs: pass, violation and inconclusive cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eudis_swarm.field_report import (
    EXIT_INCONCLUSIVE,
    EXIT_PASS,
    EXIT_VIOLATION,
    evaluate,
    format_report,
    load_logs,
    main,
    task_value,
)

TIMING = {"lease_timeout": 1.0, "rebroadcast_interval": 0.25}


def _view(owner: int | None, me: int, state: str | None = None) -> dict[str, Any]:
    if state is None:
        state = (
            "UNCLAIMED"
            if owner is None
            else "OWNED_BY_SELF"
            if owner == me
            else "CLAIMED_BY_PEER_FRESH"
        )
    return {"state": state, "owner": owner}


class LogWriter:
    def __init__(self, directory: Path, agents=(1, 2)) -> None:
        self.directory = directory
        self.agents = agents
        self.lines: dict[int, list[dict[str, Any]]] = {a: [] for a in agents}
        for agent in agents:
            self.add(
                agent,
                0.0,
                "NODE_START",
                {1: None},
                timing=TIMING,
                agents=list(agents),
                required_votes=2,
            )

    def add(
        self,
        node: int,
        wall: float,
        kind: str,
        owners: dict[int, Any],
        peers: dict[int, str] | None = None,
        **extra: Any,
    ) -> None:
        ownership = {
            str(task): (value if isinstance(value, dict) else _view(value, node))
            for task, value in owners.items()
        }
        record = {
            "node": node,
            "seq": len(self.lines[node]) + 1,
            "wall": wall,
            "local_t": wall,
            "kind": kind,
            "ownership": ownership,
            "peers": {
                str(p): (peers or {}).get(p, "HEARD") for p in self.agents if p != node
            },
            "current_task": extra.pop("current_task", None),
        }
        record.update(extra)
        self.lines[node].append(record)

    def write(self) -> Path:
        for node, records in self.lines.items():
            with (self.directory / f"node-{node}.jsonl").open("w") as stream:
                for record in records:
                    stream.write(json.dumps(record) + "\n")
        return self.directory


def _partitioned_run(tmp_path: Path, *, winner: int = 1, converge: bool = True):
    log = LogWriter(tmp_path)
    for node in (1, 2):
        log.add(node, 1.0, "FILTER_CHANGED", {1: node}, blocked=[3 - node])
        log.add(node, 1.5, "BELIEF", {1: node}, peers={3 - node: "SILENT"})
    log.add(
        1,
        1.6,
        "FAILURE_DECLARED",
        {1: 1},
        peers={2: "DECLARED_FAILED"},
        peer=2,
        voters=[1, 3],
        required_votes=2,
    )
    for node in (1, 2):
        log.add(node, 3.0, "FILTER_CHANGED", {1: node}, blocked=[])
    loser = 3 - winner
    if converge:
        log.add(
            loser,
            3.1,
            "RECONCILED",
            {1: winner},
            task=1,
            winner=winner,
            losers=[loser],
            yielded=True,
            current_task=1,
        )
        log.add(loser, 3.1, "TASK_STOOD_DOWN", {1: winner}, task=1, contested=True)
    log.add(
        winner,
        3.1,
        "RECONCILED",
        {1: winner},
        task=1,
        winner=winner,
        losers=[loser],
        yielded=False,
    )
    log.add(1, 6.0, "TICK", {1: winner})
    log.add(2, 6.0, "TICK", {1: winner if converge else loser})
    return log


def test_task_value_collapses_owner_states() -> None:
    assert task_value({"state": "OWNED_BY_SELF", "owner": 2}) == ("OWNER", 2)
    assert task_value({"state": "CLAIMED_BY_PEER_STALE", "owner": 2}) == ("OWNER", 2)
    assert task_value({"state": "COMPLETE", "owner": 1}) == ("COMPLETE",)
    assert task_value(None) == ("UNKNOWN",)


def test_a_split_brain_that_heals_passes(tmp_path: Path) -> None:
    _partitioned_run(tmp_path).write()
    report = evaluate(load_logs([tmp_path]))
    assert report.exit_code == EXIT_PASS, report.violations
    summary = report.summary
    assert summary["reconnect_source"] == "injected"
    assert summary["partition_windows_s"] == [[1.0, 3.0]]
    assert summary["divergent_tasks_at_reconnect"] == [1]
    assert summary["convergence_s"] == 0.1
    assert summary["contest_resolutions"][0]["winner"] == 1
    assert summary["concurrent_ownership_episodes"][0]["overlaps_partition"]
    assert summary["false_declarations"] == 1
    assert summary["false_declarations_connected"] == 0
    assert "PASS" in format_report(report)


def test_the_wrong_winner_is_a_violation(tmp_path: Path) -> None:
    _partitioned_run(tmp_path, winner=2).write()
    report = evaluate(load_logs([tmp_path]))
    assert report.exit_code == EXIT_VIOLATION
    assert any(item.startswith("G3") for item in report.violations)


def test_never_converging_is_a_violation(tmp_path: Path) -> None:
    _partitioned_run(tmp_path, converge=False).write()
    report = evaluate(load_logs([tmp_path]))
    assert report.exit_code == EXIT_VIOLATION
    assert any(item.startswith("G1") for item in report.violations)
    assert "VIOLATION" in format_report(report)


def test_ending_before_the_bound_is_inconclusive(tmp_path: Path) -> None:
    log = LogWriter(tmp_path)
    for node in (1, 2):
        log.add(node, 1.0, "FILTER_CHANGED", {1: node}, blocked=[3 - node])
        log.add(node, 3.0, "FILTER_CHANGED", {1: node}, blocked=[])
    log.write()
    report = evaluate(load_logs([tmp_path]))
    assert report.exit_code == EXIT_INCONCLUSIVE


def test_persistent_concurrent_ownership_after_convergence_violates_g2(
    tmp_path: Path,
) -> None:
    log = LogWriter(tmp_path)
    log.add(1, 0.5, "BELIEF", {1: 1})
    log.add(2, 0.5, "BELIEF", {1: 1})
    log.add(1, 1.0, "BELIEF", {1: 1})
    log.add(2, 1.0, "BELIEF", {1: _view(2, 2)})
    log.add(1, 5.0, "TICK", {1: 1})
    log.add(2, 5.0, "TICK", {1: 2})
    log.write()
    report = evaluate(load_logs([tmp_path]), bound=1.0)
    assert report.summary["reconnect_source"] == "observed"
    assert report.exit_code == EXIT_VIOLATION
    assert any(item.startswith("G2") for item in report.violations)


def test_a_declaration_without_quorum_violates_s1(tmp_path: Path) -> None:
    log = LogWriter(tmp_path)
    log.add(1, 1.0, "FAILURE_DECLARED", {1: None}, peer=2, voters=[1], required_votes=1)
    log.add(1, 4.0, "TICK", {1: None})
    log.add(2, 4.0, "TICK", {1: None})
    log.write()
    report = evaluate(load_logs([tmp_path]))
    assert any(item.startswith("S1") for item in report.violations)


def test_fail_stop_latency_duplicates_and_malformed_lines(tmp_path: Path) -> None:
    log = LogWriter(tmp_path, agents=(1, 2, 3))
    log.add(3, 1.0, "FAIL_STOP", {1: None})
    log.add(
        1,
        1.8,
        "FAILURE_DECLARED",
        {1: None},
        peers={3: "DECLARED_FAILED"},
        peer=3,
        voters=[1, 2],
        required_votes=2,
    )
    log.add(
        2,
        1.9,
        "FAILURE_DECLARATION_APPLIED",
        {1: None},
        peers={3: "DECLARED_FAILED"},
        peer=3,
    )
    log.add(
        1,
        2.0,
        "TASK_COMPLETED",
        {1: _view(1, 1, "COMPLETE")},
        peers={3: "DECLARED_FAILED"},
        task=1,
    )
    log.add(
        2,
        2.0,
        "TASK_COMPLETED",
        {1: _view(2, 2, "COMPLETE")},
        peers={3: "DECLARED_FAILED"},
        task=1,
    )
    log.add(1, 5.0, "TICK", {1: _view(1, 1, "COMPLETE")}, peers={3: "DECLARED_FAILED"})
    log.add(2, 5.0, "TICK", {1: _view(1, 2, "COMPLETE")}, peers={3: "DECLARED_FAILED"})
    directory = log.write()
    with (directory / "node-1.jsonl").open("a") as stream:
        stream.write('{torn line\n[1]\n{"node": 1}\n\n')
    (directory / "launcher.jsonl").write_text(
        json.dumps({"source": "launcher", "kind": "LAUNCH", "wall": 0.0}) + "\n"
    )
    report = evaluate(load_logs([directory]))
    summary = report.summary
    assert summary["malformed_lines"] == 3
    assert summary["fail_stopped"] == {"3": 1.0}
    assert summary["fail_stop_detection_latency_s"] == {"3": {"1": 0.8, "2": 0.9}}
    assert summary["duplicate_completions"] == {"1": [1, 2]}
    assert summary["tasks_completed"] == 1
    assert summary["false_declarations"] == 0


def test_too_few_logs_and_no_reconnection_are_inconclusive(tmp_path: Path) -> None:
    single = tmp_path / "single"
    single.mkdir()
    LogWriter(single, agents=(1,)).write()
    assert evaluate(load_logs([single])).exit_code == EXIT_INCONCLUSIVE
    silent = tmp_path / "silent"
    silent.mkdir()
    log = LogWriter(silent)
    log.add(1, 1.0, "TICK", {1: None}, peers={2: "SILENT"})
    log.add(2, 1.0, "TICK", {1: None}, peers={1: "SILENT"})
    log.write()
    report = evaluate(load_logs([silent / "node-1.jsonl", silent / "node-2.jsonl"]))
    assert report.exit_code == EXIT_INCONCLUSIVE


def test_main_prints_and_writes_json(tmp_path: Path, capsys) -> None:
    _partitioned_run(tmp_path).write()
    output = tmp_path / "report.json"
    code = main([str(tmp_path), "--json", str(output), "--bound", "2.0"])
    assert code == EXIT_PASS
    assert "field report: PASS" in capsys.readouterr().out
    written = json.loads(output.read_text())
    assert written["exit_code"] == 0
    assert written["summary"]["bound_s"] == 2.0
    assert main([str(tmp_path), "--reconnect-at", "3.0"]) == EXIT_PASS
