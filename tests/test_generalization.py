"""The generalization comparison is paired and reproducible."""

from __future__ import annotations

import io

from eudis_swarm.config import SimulationConfig
from eudis_swarm.generalization import CSV_COLUMNS, run_generalization, write_csv
from eudis_swarm.simulation import Simulation


def test_each_seed_pairs_controls_under_the_same_conditions() -> None:
    rows = list(run_generalization((1,)))

    assert len(rows) == 12
    assert all(tuple(row) == CSV_COLUMNS for row in rows)
    for distributed, centralized in zip(rows[::2], rows[1::2], strict=True):
        assert distributed["task_control"] == "distributed"
        assert centralized["task_control"] == "centralized"
        for key in (
            "agents",
            "tasks",
            "area_side",
            "communication_range",
            "topology",
            "seed",
        ):
            assert distributed[key] == centralized[key]
    assert {(row["agents"], row["tasks"]) for row in rows} == {
        ("4", "8"),
        ("6", "12"),
    }
    # The sparse 4-UAV case is a known counterexample to universal completion,
    # even though the 6-UAV case under the same scaled setup can finish.
    assert rows[3]["mission_completed"] == "0"


def test_identical_seed_ranges_produce_identical_csv() -> None:
    first, second = io.StringIO(), io.StringIO()
    write_csv(run_generalization((1,)), first)
    write_csv(run_generalization((1,)), second)

    assert first.getvalue() == second.getvalue()
    assert first.getvalue().startswith(",".join(CSV_COLUMNS) + "\n")
    assert "\r\n" not in first.getvalue()


def test_central_overlap_can_include_a_physically_stopped_claimant() -> None:
    config = SimulationConfig(
        agent_count=4,
        task_count=8,
        communication_range=130.0,
        task_control="centralized",
        random_seed=7,
        failure_time=4.0,
        max_simulation_time=30.0,
        time_step=0.5,
    )

    result = Simulation(config, capture_trace=True).run()

    assert result.metrics.concurrent_ownership_duration == 0.5
    assert result.trace is not None
    frame = next(frame for frame in result.trace.frames if frame.timestamp == 6.0)
    owners = {
        view.observer_agent_id
        for view in frame.ownership
        if view.task_id == 5 and view.state == "OWNED_BY_SELF"
    }
    assert owners == {1, 2}
    assert {
        agent.agent_id: agent.responsive
        for agent in frame.agents
        if agent.agent_id in owners
    } == {
        1: True,
        2: False,
    }
