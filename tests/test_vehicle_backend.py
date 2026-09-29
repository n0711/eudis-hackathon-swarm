"""The movement boundary preserves point-mass outcomes and event ordering."""

from __future__ import annotations

from dataclasses import replace

import pytest

from eudis_swarm.agent import Agent, Position
from eudis_swarm.communication import RegionJammer
from eudis_swarm.config import SimulationConfig
from eudis_swarm.simulation import Simulation
from eudis_swarm.vehicle_backend import PointMassBackend, VehicleBackend


def test_point_mass_backend_preserves_physical_failure_semantics() -> None:
    backend = PointMassBackend()
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)

    backend.move_toward(agent, (5.0, 0.0), 1.0)
    assert agent.position == (5.0, 0.0)  # never overshoots

    agent.declare_failed()  # belief does not stop the vehicle
    backend.move_toward(agent, (20.0, 0.0), 1.0)
    assert agent.position == (15.0, 0.0)

    agent.inject_failure(2.0)  # physical stop does
    backend.move_toward(agent, (20.0, 0.0), 1.0)
    assert agent.position == (15.0, 0.0)
    with pytest.raises(ValueError, match="elapsed"):
        backend.move_toward(agent, (20.0, 0.0), -1.0)


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[int, Position, float]] = []
        self.point_mass = PointMassBackend()

    def move_toward(self, agent: Agent, target: Position, elapsed: float) -> None:
        self.calls.append((agent.agent_id, target, elapsed))
        self.point_mass.move_toward(agent, target, elapsed)


def test_simulation_routes_motion_through_backend_without_changing_results() -> None:
    config = replace(
        SimulationConfig(),
        agent_count=2,
        task_count=1,
        task_control="centralized",
        failure_time=100.0,
        max_simulation_time=10.0,
        random_seed=7,
    )
    backend = RecordingBackend()
    typed_backend: VehicleBackend = backend

    default = Simulation(config).run()
    injected = Simulation(config, vehicle_backend=typed_backend).run()

    assert backend.calls
    assert all(
        agent_id == 1 and elapsed == 0.25 for agent_id, _, elapsed in backend.calls
    )
    assert injected.position_history == default.position_history
    assert injected.metrics == default.metrics
    assert injected.position_history[1][-1] == (
        3.5,
        (35.63850559819929, 21.9198692284873),
    )
    assert injected.position_history[2][-1] == (3.5, (95.0, 5.0))
    assert injected.metrics.completed_task_count == 1


def test_communication_only_boundaries_do_not_advance_backend() -> None:
    config = replace(
        SimulationConfig(),
        agent_count=2,
        task_count=1,
        task_control="centralized",
        failure_time=100.0,
        max_simulation_time=10.0,
        random_seed=7,
        region_jammer=RegionJammer(50.0, 50.0, 10.0, 0.35, 0.45),
    )
    backend = RecordingBackend()

    result = Simulation(config, vehicle_backend=backend).run()

    timestamps = {time for time, _ in result.position_history[1]}
    assert {0.35, 0.45} <= timestamps
    assert backend.calls
    assert all(elapsed == 0.25 for _, _, elapsed in backend.calls)
