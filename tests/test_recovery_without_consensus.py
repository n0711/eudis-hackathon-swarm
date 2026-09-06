"""Recovering from a loss the swarm is unable to declare.

The quorum threshold is ``max(2, floor((N - 1) / 2) + 1)``, so one observer can
never declare a peer failed. With two aircraft that makes failure permanently
undeclarable: the survivor is the only witness. What happens next separates the
two control models, and it is the sharpest argument in the project.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.simulation import Simulation


def _run(agent_count: int, task_control: str):
    return Simulation(
        replace(
            SimulationConfig(),
            agent_count=agent_count,
            task_count=3,
            communication_range=130.0,
            task_control=task_control,
            failure_agent_id=2,
            failure_time=4.0,
            max_simulation_time=60.0,
        )
    ).run()


def test_two_aircraft_cannot_declare_a_loss_at_all() -> None:
    """One survivor is one witness, and one witness is never a quorum."""

    for mode in ("centralized", "distributed"):
        metrics = _run(2, mode).metrics
        assert metrics.physically_failed_count == 1
        assert metrics.failed_agent_count == 0, mode
        assert metrics.undetected_failure_count == 1, mode


def test_distributed_ownership_recovers_the_work_anyway() -> None:
    """Lease expiry needs no consensus, so the survivor simply re-claims."""

    metrics = _run(2, "distributed").metrics

    assert metrics.mission_completed is True
    assert metrics.completed_task_count == 3


def test_central_control_strands_the_work_it_cannot_reassign() -> None:
    """Reassignment is gated on a declaration that can never arrive.

    The coordinator holds the task assigned to an aircraft that has stopped.
    Nothing releases it, so the mission cannot finish -- not because the work
    is impossible, but because the recovery path requires agreement that two
    aircraft are structurally unable to reach.
    """

    result = _run(2, "centralized")
    metrics = result.metrics

    assert metrics.mission_completed is False
    assert metrics.completed_task_count == 2

    stranded = [
        task
        for task in result.mission.tasks.values()
        if task.assigned_agent == 2 and task.status.value != "COMPLETED"
    ]
    assert stranded, "expected the dead aircraft's task to remain assigned to it"


@pytest.mark.parametrize("mode", ["centralized", "distributed"])
def test_a_third_aircraft_restores_the_quorum_and_both_modes_recover(
    mode: str,
) -> None:
    """The floor is about witness count, not about the control model."""

    metrics = _run(3, mode).metrics

    assert metrics.failed_agent_count == 1
    assert metrics.undetected_failure_count == 0
    assert metrics.mission_completed is True
    assert metrics.completed_task_count == 3
