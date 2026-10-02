"""Start N field nodes on one machine, drive a fault schedule, collect the logs.

This is the one-laptop lab harness::

    python -m eudis_swarm.field_launch \\
        --mission examples/field/mission_4uav.json \\
        --faults examples/field/partition_2x2.json \\
        --duration 30 --logs logs/run1 --report

It rewrites the mission's unicast addresses to ``127.0.0.1:<base-port + id>``
(so a mission written for a LAN also runs locally), starts one
``python -m eudis_swarm.field_node`` process per agent with a common start
time, and at each scheduled instant atomically rewrites the shared control file
the nodes poll -- ``partition``, ``heal``, ``fail_stop``. Every fault it
injects is recorded in ``launcher.jsonl`` for the observer report; the nodes
themselves learn of it only as missing datagrams.

The launcher is observer and harness. It never sends a protocol datagram and
never reads a node's state other than its finished log.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .field_faults import (
    ControlState,
    ScheduledFault,
    load_fault_schedule,
    write_control,
)
from .field_mission import FieldMission, load_mission


def localhost_mission(mission: FieldMission, base_port: int) -> FieldMission:
    """Return the mission with every agent on 127.0.0.1 and unicast addressing."""

    from .field_mission import FieldNetwork

    agents = tuple(
        replace(agent, address=("127.0.0.1", base_port + agent.agent_id))
        for agent in mission.agents
    )
    return replace(mission, agents=agents, network=FieldNetwork(mode="unicast"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _log(stream: Any, record: dict[str, Any]) -> None:
    stream.write(json.dumps({"source": "launcher", **record}, sort_keys=True) + "\n")
    stream.flush()


def launch(
    mission_path: str | Path,
    *,
    logs: str | Path,
    duration: float,
    faults: Sequence[ScheduledFault] = (),
    base_port: int = 47000,
    startup_delay: float = 1.5,
    loss: float = 0.0,
    python: str = sys.executable,
) -> Path:
    """Run one localhost experiment and return the log directory."""

    log_dir = Path(logs)
    log_dir.mkdir(parents=True, exist_ok=True)
    mission = localhost_mission(load_mission(mission_path), base_port)
    resolved_mission = log_dir / "mission.json"
    _write_json(resolved_mission, mission.to_json())
    control = log_dir / "control.json"
    write_control(control, ControlState())
    start_at = time.time() + startup_delay
    processes: list[subprocess.Popen[bytes]] = []
    stderr_files = []
    with (log_dir / "launcher.jsonl").open("w", encoding="utf-8") as launcher_log:
        _log(
            launcher_log,
            {
                "kind": "LAUNCH",
                "wall": time.time(),
                "start_at": start_at,
                "duration": duration,
                "agents": list(mission.agent_ids),
                "mission_id": mission.mission_id,
                "loss": loss,
            },
        )
        for agent in mission.agents:
            command = [
                python,
                "-m",
                "eudis_swarm.field_node",
                "--mission",
                str(resolved_mission),
                "--agent",
                str(agent.agent_id),
                "--log",
                str(log_dir / f"node-{agent.agent_id}.jsonl"),
                "--duration",
                str(duration),
                "--control",
                str(control),
                "--start-at",
                repr(start_at),
            ]
            if loss > 0.0:
                command += ["--loss", str(loss), "--loss-seed", str(agent.agent_id)]
            stderr = (log_dir / f"node-{agent.agent_id}.stderr").open("wb")
            stderr_files.append(stderr)
            processes.append(
                subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr)
            )
        try:
            for event in faults:
                delay = start_at + event.at - time.time()
                if delay > 0.0:
                    time.sleep(delay)
                write_control(control, event.state)
                _log(
                    launcher_log,
                    {
                        "kind": "FAULT",
                        "label": event.label,
                        "at": event.at,
                        "wall": time.time(),
                        "control": event.state.to_json(),
                    },
                )
            deadline = start_at + duration + 10.0
            for process in processes:
                remaining = max(0.1, deadline - time.time())
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    process.terminate()
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:  # pragma: no cover
                        process.kill()
            for stderr in stderr_files:
                stderr.close()
        _log(
            launcher_log,
            {
                "kind": "FINISHED",
                "wall": time.time(),
                "exit_codes": {
                    str(agent.agent_id): process.returncode
                    for agent, process in zip(mission.agents, processes)
                },
            },
        )
    return log_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eudis_swarm.field_launch",
        description="Run N field nodes on localhost with a fault schedule.",
    )
    parser.add_argument("--mission", required=True)
    parser.add_argument("--faults", help="fault schedule JSON (partition/heal/...)")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--logs", required=True, help="directory for all logs")
    parser.add_argument("--base-port", type=int, default=47000)
    parser.add_argument("--startup-delay", type=float, default=1.5)
    parser.add_argument("--loss", type=float, default=0.0)
    parser.add_argument(
        "--report", action="store_true", help="run field_report afterwards"
    )
    parser.add_argument("--bound", type=float, help="report convergence bound (s)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    schedule = () if args.faults is None else load_fault_schedule(args.faults)
    log_dir = launch(
        args.mission,
        logs=args.logs,
        duration=args.duration,
        faults=schedule,
        base_port=args.base_port,
        startup_delay=args.startup_delay,
        loss=args.loss,
    )
    print(f"logs written to {log_dir}")
    if not args.report:
        return 0
    from .field_report import main as report_main

    report_args = [str(log_dir), "--json", str(log_dir / "report.json")]
    if args.bound is not None:
        report_args += ["--bound", str(args.bound)]
    return report_main(report_args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
