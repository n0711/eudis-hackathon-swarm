"""Evaluator inputs remain paired and never mutate the standard scenario."""

from __future__ import annotations

import io

from eudis_swarm.config import SimulationConfig
from eudis_swarm.robustness import (
    ROBUSTNESS_COLUMNS,
    _ring_agents,
    run_robustness,
    write_csv,
)
from eudis_swarm.simulation import build_scenario


def test_ring_is_a_distinct_repeatable_initial_layout() -> None:
    config = SimulationConfig(agent_count=4, task_count=8)
    standard, _ = build_scenario(config)
    ring = _ring_agents(config)

    assert len(ring) == 4
    assert [agent.agent_id for agent in ring] == [1, 2, 3, 4]
    assert [agent.position for agent in ring] != [agent.position for agent in standard]
    assert [agent.position for agent in ring] == [
        agent.position for agent in _ring_agents(config)
    ]


def test_robustness_rows_pair_controls_and_record_injected_fault() -> None:
    rows = list(run_robustness((1,)))

    assert len(rows) == 48
    assert all(tuple(row) == ROBUSTNESS_COLUMNS for row in rows)
    assert all(row["failure_injected"] == "1" for row in rows)
    assert {(row["layout"], row["failure_time"]) for row in rows} == {
        ("default", "2.000000"),
        ("default", "6.000000"),
        ("ring", "2.000000"),
        ("ring", "6.000000"),
    }
    for distributed, centralized in zip(rows[::2], rows[1::2], strict=True):
        assert distributed["task_control"] == "distributed"
        assert centralized["task_control"] == "centralized"
        for key in (
            "layout",
            "failure_time",
            "agents",
            "tasks",
            "area_side",
            "communication_range",
            "topology",
            "seed",
        ):
            assert distributed[key] == centralized[key]

    stream = io.StringIO()
    write_csv(iter(rows), stream)
    assert len(stream.getvalue().splitlines()) == 49
    assert "\r\n" not in stream.getvalue()
