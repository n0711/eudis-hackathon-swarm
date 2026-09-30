"""Run a bounded synthetic observation/discovery demo, without flight commands.

Usage: python -m eudis_swarm.observation_demo examples/isr_publications.jsonl
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .config import SimulationConfig
from .observation_replay import parse_record
from .observation_transport import ObservationPublication
from .simulation import Simulation


def load_publications(path: Path) -> tuple[ObservationPublication, ...]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            records.append(
                ObservationPublication(
                    payload["source_agent_id"],
                    payload["published_at"],
                    parse_record(payload["record"]),
                )
            )
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"line {number}: {error}") from error
    return tuple(records)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args(argv)
    config = SimulationConfig(
        agent_count=3,
        task_count=6,
        random_seed=args.seed,
        failure_time=100.0,
        communication_range=150.0,
        comm_fault_agent_id=1,
        comm_fault_start=0.0,
        comm_fault_end=2.0,
        max_simulation_time=30.0,
    )
    try:
        result = Simulation(
            config,
            observation_publications=load_publications(args.jsonl),
            capture_trace=True,
        ).run()
    except (ValueError, OSError) as error:
        parser.error(str(error))
    assert result.task_discoveries is not None
    config_evidence = asdict(config)
    config_evidence["blocked_links"] = sorted(config.blocked_links)
    evidence = {
        "evidence_type": "synthetic_simulation",
        "config": config_evidence,
        "mission_completed": result.metrics.mission_completed,
        "total_tasks": result.metrics.total_task_count,
        "completed_tasks": result.metrics.completed_task_count,
        "duration_s": result.metrics.simulation_duration,
        "discoveries": {
            str(receiver): [asdict(task) for task in store.tasks]
            for receiver, store in result.task_discoveries.items()
        },
        "observation_receipts": [
            asdict(receipt) for receipt in result.observation_receipts
        ],
    }
    content = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
