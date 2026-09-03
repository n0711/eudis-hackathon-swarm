"""Failure detection depends on connectivity, and can silently stop working.

Declaring a UAV dead needs a quorum of peers that can still hear one another.
Nothing guarantees that quorum exists. These tests pin the point at which it
stops forming on the default scenario, so the limitation stays visible instead
of being rediscovered as a surprise.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from eudis_swarm.agent import AgentStatus
from eudis_swarm.config import SimulationConfig
from eudis_swarm.mission import MissionEventKind
from eudis_swarm.simulation import Simulation


def _run(communication_range: float):
    return Simulation(
        replace(SimulationConfig(), communication_range=communication_range)
    ).run()


def test_a_well_connected_swarm_detects_the_loss() -> None:
    result = _run(130.0)
    metrics = result.metrics

    assert metrics.physically_failed_count == 1
    assert metrics.failed_agent_count == 1
    assert metrics.undetected_failure_count == 0
    assert result.mission.agents[2].status is AgentStatus.FAILED


def test_a_sparse_swarm_loses_an_aircraft_without_noticing() -> None:
    """The mission still finishes, and that is exactly what makes this dangerous.

    UAV 2 stops flying at t=4.00. No quorum ever forms, so it is never declared,
    its coordinator status stays ACTIVE for the whole mission, and no task is
    ever released. All twenty tasks still complete -- ownership leases expire on
    their own -- so nothing in the mission outcome reveals the loss.
    """

    result = _run(50.0)
    metrics = result.metrics
    lost = result.mission.agents[2]

    # ground truth: the aircraft is gone
    assert lost.responsive is False
    assert lost.failure_injected_at == 4.0
    assert metrics.physically_failed_count == 1

    # belief: the swarm never finds out
    assert metrics.failed_agent_count == 0
    assert metrics.undetected_failure_count == 1
    assert lost.status is AgentStatus.ACTIVE
    assert not [
        event
        for event in result.mission.events
        if event.kind is MissionEventKind.FAILURE_DECLARED
    ]

    # and the mission outcome hides it completely
    assert metrics.mission_completed is True
    assert metrics.completed_task_count == 20


@pytest.mark.parametrize(
    ("communication_range", "detected"),
    [(90.0, True), (78.0, True), (75.0, False), (70.0, False)],
)
def test_detection_has_a_connectivity_floor(
    communication_range: float, detected: bool
) -> None:
    """On the default scenario the detector stops working between 78 and 75.

    The exact numbers matter less than the shape: detection degrades as a cliff,
    not gradually, and the mission result gives no warning that it happened.
    """

    metrics = _run(communication_range).metrics

    assert metrics.physically_failed_count == 1
    assert metrics.failed_agent_count == (1 if detected else 0)
    assert metrics.undetected_failure_count == (0 if detected else 1)
    assert metrics.completed_task_count == 20
