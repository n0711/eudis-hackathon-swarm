"""Real end-to-end: four node processes on loopback, a 2+2 partition, a heal.

This is the lab claim in miniature. Four ``python -m eudis_swarm.field_node``
processes exchange real UDP datagrams on 127.0.0.1; the launcher imposes a
2+2 partition and lifts it; the observer report must find a genuine split
brain at reconnection and convergence within its bound.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from field_fakes import mission_document

from eudis_swarm.field_faults import load_fault_schedule
from eudis_swarm.field_launch import launch, localhost_mission, main
from eudis_swarm.field_mission import parse
from eudis_swarm.field_report import EXIT_PASS, evaluate, load_logs


def _free_base_port(count: int) -> int:
    """Find a base port whose next ``count`` ports are all bindable."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    start = probe.getsockname()[1]
    probe.close()
    for base in list(range(max(1024, start - 2000), 60000, 10)) + [20000]:
        sockets = []
        try:
            for offset in range(1, count + 1):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sockets.append(sock)
                sock.bind(("127.0.0.1", base + offset))
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()
        return base
    raise RuntimeError("no free port range")


def test_localhost_mission_rewrites_addresses() -> None:
    mission = parse(
        mission_document(
            network={"mode": "broadcast", "broadcast_address": "10.0.0.255", "port": 1}
        )
    )
    local = localhost_mission(mission, 50000)
    assert local.network.mode == "unicast"
    assert local.agent(3).address == ("127.0.0.1", 50003)


def test_four_processes_partition_heal_and_converge(tmp_path: Path) -> None:
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(json.dumps(mission_document()))
    faults_path = tmp_path / "faults.json"
    faults_path.write_text(
        json.dumps(
            {
                "events": [
                    {"at": 1.0, "partition": [[1, 2], [3, 4]]},
                    {"at": 3.5, "heal": True},
                ]
            }
        )
    )
    logs = launch(
        mission_path,
        logs=tmp_path / "logs",
        duration=8.0,
        faults=load_fault_schedule(faults_path),
        base_port=_free_base_port(4),
        startup_delay=2.0,
    )
    stderr = {path.name: path.read_text() for path in logs.glob("*.stderr")}
    report = evaluate(load_logs([logs]))
    assert report.exit_code == EXIT_PASS, (report.violations, report.notes, stderr)
    summary = report.summary
    # the partition really happened and really split ownership...
    assert summary["reconnect_source"] == "injected"
    assert summary["divergent_tasks_at_reconnect"], summary
    assert any(
        episode["overlaps_partition"]
        for episode in summary["concurrent_ownership_episodes"]
    )
    # ...the losers stood down, and every replica converged within the bound.
    assert any(item["contested"] for item in summary["stand_downs"])
    assert summary["convergence_s"] is not None
    assert summary["convergence_s"] <= summary["bound_s"]
    assert summary["tasks_completed"] == summary["tasks_total"] == 4
    assert summary["retractions"]
    launcher = [
        json.loads(line) for line in (logs / "launcher.jsonl").read_text().splitlines()
    ]
    assert [item["kind"] for item in launcher] == [
        "LAUNCH",
        "FAULT",
        "FAULT",
        "FINISHED",
    ]
    assert set(launcher[-1]["exit_codes"].values()) == {0}


def test_launcher_command_line_runs_the_report(tmp_path: Path, capsys) -> None:
    mission_path = tmp_path / "mission.json"
    document = mission_document(
        agents=[
            {"id": 1, "start": [0.0, 0.0]},
            {"id": 2, "start": [0.0, 1.0]},
        ],
        tasks=[{"id": 1, "position": [1.0, 0.0]}],
    )
    mission_path.write_text(json.dumps(document))
    code = main(
        [
            "--mission",
            str(mission_path),
            "--duration",
            "2.5",
            "--logs",
            str(tmp_path / "logs"),
            "--base-port",
            str(_free_base_port(2)),
            "--startup-delay",
            "1.0",
            "--loss",
            "0.05",
            "--report",
            "--bound",
            "2.0",
        ]
    )
    output = capsys.readouterr().out
    assert code == EXIT_PASS, output
    assert "field report: PASS" in output
    assert (tmp_path / "logs" / "report.json").exists()
