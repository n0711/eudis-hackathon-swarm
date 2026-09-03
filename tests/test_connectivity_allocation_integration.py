"""Compare deterministic distance and receiver-local utility outcomes.

The integration checks preserve exact decisions while enforcing `HEARD` eligibility
and proving the legacy centralized allocator is not in the operational path.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.mission import MissionEventKind
from eudis_swarm.simulation import Simulation


def _comparison_config() -> SimulationConfig:
    return SimulationConfig(
        random_seed=1,
        failure_time=100.0,
        communication_range=35.0,
        allocation_policy="distance",
    )


class _ForbiddenAllocator:
    def allocate(self, *_args: object) -> None:
        raise AssertionError(
            "normal simulation must not call the centralized allocator"
        )


def test_connectivity_policy_makes_an_explainable_different_decision() -> None:
    distance_simulation = Simulation(_comparison_config())
    distance_simulation.mission.allocator = _ForbiddenAllocator()  # type: ignore[assignment]
    distance = distance_simulation.run()
    connectivity_simulation = Simulation(
        replace(_comparison_config(), allocation_policy="connectivity")
    )
    connectivity_simulation.mission.allocator = _ForbiddenAllocator()  # type: ignore[assignment]
    connectivity = connectivity_simulation.run()

    distance_records = distance.metrics.allocation_decisions
    connectivity_records = connectivity.metrics.allocation_decisions
    first_difference = next(
        index
        for index, (baseline, aware) in enumerate(
            zip(distance_records, connectivity_records, strict=True)
        )
        if (
            baseline.allocation.agent_id,
            baseline.allocation.task_id,
        )
        != (aware.allocation.agent_id, aware.allocation.task_id)
    )

    assert first_difference == 11
    baseline = distance_records[first_difference]
    aware = connectivity_records[first_difference]
    assert baseline.timestamp == aware.timestamp == 4.5
    assert (baseline.allocation.agent_id, baseline.allocation.task_id) == (2, 2)
    assert baseline.allocation.distance == pytest.approx(11.179640564866514)
    assert (aware.allocation.agent_id, aware.allocation.task_id) == (2, 3)
    assert aware.allocation.distance == pytest.approx(24.198519921711643)
    assert aware.allocation.predicted_peer_degree == 1
    assert aware.allocation.predicted_isolation is False
    assert distance.metrics.mission_completed is True
    assert connectivity.metrics.mission_completed is True
    assert distance.metrics.completed_task_count == 20
    assert connectivity.metrics.completed_task_count == 20
    assert distance.metrics.simulation_duration == 12.0
    assert connectivity.metrics.simulation_duration == 17.25
    assert distance.metrics.isolation_event_count == 4
    assert connectivity.metrics.isolation_event_count == 1
    assert distance.metrics.minimum_link_count == 0
    assert connectivity.metrics.minimum_link_count == 0
    assert distance.metrics.maximum_connected_component_count == 4
    assert connectivity.metrics.maximum_connected_component_count == 4
    assert distance.metrics.total_communication_degraded_duration == 8.25
    assert connectivity.metrics.total_communication_degraded_duration == 8.25
    assert connectivity.metrics.connectivity_aware_assignment_count == 22
    # one raw-fresh but non-heard peer is now correctly excluded from scoring.
    assert connectivity.metrics.predicted_isolation_assignment_count == 12


def test_connectivity_comparison_is_deterministic() -> None:
    config = replace(_comparison_config(), allocation_policy="connectivity")
    first = Simulation(config).run()
    second = Simulation(config).run()

    assert first.metrics.allocation_decisions == second.metrics.allocation_decisions


def test_stale_peer_state_under_the_connectivity_policy_still_loses_the_uav() -> None:
    result = Simulation(
        SimulationConfig(
            failure_time=100.0,
            communication_range=130.0,
            comm_fault_agent_id=2,
            comm_fault_start=4.0,
            comm_fault_end=8.0,
            peer_state_stale_after=2.5,
            allocation_policy="connectivity",
        )
    ).run()

    assert result.metrics.mission_completed is True
    assert result.metrics.simulation_duration == 12.0
    # the allocation policy does not change the detector's verdict.
    assert result.metrics.declaration_retraction_count == 1
    assert result.metrics.failed_agent_count == 0
    assert result.metrics.orphaned_task_count == 0
    assert result.metrics.reassigned_task_count == 0
    assert result.metrics.peer_state_stale_transition_count == 6
    assert result.metrics.peer_state_refresh_transition_count == 6
    # the declaration is belief only: the vehicle is untouched and, once the
    # link returns, reinstated.
    assert result.mission.agents[2].responsive is True
    assert result.mission.agents[2].wrongly_declared is False
    assert result.mission.agents[2].failure_injected_at is None
    assert result.metrics.duplicated_task_completion_count == 0
    event_kinds = {event.kind for event in result.mission.events}
    assert MissionEventKind.FAILURE_DECLARED in event_kinds
    assert MissionEventKind.TASK_REASSIGNED not in event_kinds
