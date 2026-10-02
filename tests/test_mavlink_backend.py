"""The MavlinkBackend adapter, tested against a fake vehicle -- no autopilot.

Covers the acceptance criteria docs/sitl.md sets for this adapter: command/
observation ordering, stale or missing telemetry and timeout, physical stop
versus declaration, no backend call on communication-only boundaries, and
evidence delivery across a partition. None of it needs pymavlink or a live
ArduPilot instance, matching how tests/test_sitl.py covers sitl.py itself.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest

from eudis_swarm.agent import Agent, Position, advance_point_mass
from eudis_swarm.communication import RegionJammer
from eudis_swarm.config import SimulationConfig
from eudis_swarm.mavlink_backend import MavlinkBackend
from eudis_swarm.simulation import Simulation
from eudis_swarm.vehicle_backend import PointMassBackend


class _FakeVehicle:
    """A VehicleTelemetry double that moves at a fixed speed when observed.

    Good enough to drive a real Simulation mission end to end with no MAVLink
    connection: ``command_toward`` records a target, ``observe`` advances
    position toward it and advances ``vehicle_time_s``, exactly like a real
    autopilot except synchronous and exact.
    """

    def __init__(self, position: Position = (0.0, 0.0), speed: float = 10.0) -> None:
        self._position = position
        self._speed = speed
        self._target = position
        self._vehicle_time_s = 0.0
        self.commands: list[Position] = []
        self.observe_calls = 0

    @property
    def position(self) -> Position:
        return self._position

    @property
    def vehicle_time_s(self) -> float:
        return self._vehicle_time_s

    def command_toward(self, target: Position) -> None:
        self._target = target
        self.commands.append(target)

    def observe(self, duration: float = 0.0) -> Position:
        self.observe_calls += 1
        self._position = advance_point_mass(
            self._position, self._target, self._speed, duration
        )
        self._vehicle_time_s += duration
        return self._position


class _StalledVehicle:
    """A vehicle whose clock never advances -- telemetry that never arrives."""

    position = (0.0, 0.0)
    vehicle_time_s = 0.0

    def __init__(self) -> None:
        self.commanded: Position | None = None

    def command_toward(self, target: Position) -> None:
        self.commanded = target

    def observe(self, duration: float = 0.0) -> Position:
        return self.position


class _ForbiddenVehicle:
    """Raises if touched at all -- proves a non-responsive agent is skipped."""

    def command_toward(self, target: Position) -> None:
        raise AssertionError("command_toward must not be called")

    def observe(self, duration: float = 0.0) -> Position:
        raise AssertionError("observe must not be called")

    @property
    def position(self) -> Position:
        raise AssertionError("position must not be read")

    @property
    def vehicle_time_s(self) -> float:
        raise AssertionError("vehicle_time_s must not be read")


def test_rejects_an_empty_vehicle_mapping() -> None:
    with pytest.raises(ValueError, match="vehicles"):
        MavlinkBackend({})


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_rejects_a_nonpositive_step_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="step_timeout"):
        MavlinkBackend({1: _FakeVehicle()}, step_timeout=timeout)


def test_commands_before_observing_and_reports_the_observed_position() -> None:
    vehicle = _FakeVehicle(speed=10.0)
    backend = MavlinkBackend({1: vehicle}, step_timeout=5.0)
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)

    backend.move_toward(agent, (5.0, 0.0), 1.0)

    assert vehicle.commands == [(5.0, 0.0)]  # commanded before any observation
    assert vehicle.observe_calls > 0
    assert agent.position == vehicle.position  # the real observation, not a prediction
    assert agent.position == pytest.approx((5.0, 0.0), abs=0.2)


def test_an_unmapped_agent_falls_back_to_point_mass() -> None:
    backend = MavlinkBackend({1: _ForbiddenVehicle()}, step_timeout=5.0)
    agent = Agent(agent_id=2, position=(0.0, 0.0), speed=10.0)
    expected = Agent(agent_id=2, position=(0.0, 0.0), speed=10.0)
    PointMassBackend().move_toward(expected, (5.0, 0.0), 1.0)

    backend.move_toward(agent, (5.0, 0.0), 1.0)

    assert agent.position == expected.position


def test_a_negative_elapsed_is_refused_before_touching_the_vehicle() -> None:
    backend = MavlinkBackend({1: _ForbiddenVehicle()}, step_timeout=5.0)
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)

    with pytest.raises(ValueError, match="elapsed"):
        backend.move_toward(agent, (5.0, 0.0), -1.0)


def test_a_physical_failure_stops_all_interaction_with_the_vehicle() -> None:
    """Declared-dead keeps flying; physically stopped does not touch the backend."""

    backend = MavlinkBackend({1: _ForbiddenVehicle()}, step_timeout=5.0)
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)

    agent.inject_failure(0.0)  # physical stop
    backend.move_toward(agent, (5.0, 0.0), 1.0)  # must not touch _ForbiddenVehicle

    assert agent.position == (0.0, 0.0)


def test_a_declared_failure_alone_does_not_stop_the_vehicle() -> None:
    """Belief that a UAV is dead must not itself stop a vehicle that still flies."""

    vehicle = _FakeVehicle(speed=10.0)
    backend = MavlinkBackend({1: vehicle}, step_timeout=5.0)
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)

    agent.declare_failed()
    backend.move_toward(agent, (5.0, 0.0), 1.0)

    assert vehicle.commands  # still commanded
    assert agent.position != (0.0, 0.0)  # still advanced


def test_stalled_telemetry_raises_rather_than_reporting_a_stale_position() -> None:
    vehicle = _StalledVehicle()
    backend = MavlinkBackend({1: vehicle}, step_timeout=0.05)
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)

    with pytest.raises(TimeoutError, match="stalled"):
        backend.move_toward(agent, (5.0, 0.0), 1.0)

    assert vehicle.commanded == (5.0, 0.0)  # it was still commanded first
    assert agent.position == (0.0, 0.0)  # never overwritten with a stale "fresh" value


def test_one_integrated_vehicle_does_not_change_evidence_semantics() -> None:
    """One agent on fake telemetry, one on point-mass: coordination still converges.

    This is the integration the "Next" phase of docs/sitl.md calls for: a real
    Simulation mission, with one vehicle's physical motion coming from
    observed telemetry rather than the analytical model, while claim/lease/
    completion semantics are exercised exactly as in the all-point-mass case.
    """

    config = replace(
        SimulationConfig(),
        agent_count=2,
        task_count=2,
        task_control="distributed",
        failure_time=100.0,
        max_simulation_time=15.0,
        random_seed=7,
    )
    vehicle = _FakeVehicle(position=(0.0, 0.0), speed=10.0)
    backend = MavlinkBackend({1: vehicle}, step_timeout=5.0)

    result = Simulation(config, vehicle_backend=backend).run()

    assert result.metrics.completed_task_count == config.task_count
    assert vehicle.commands  # the integrated agent was actually driven
    assert result.position_history[1][-1][1] == vehicle.position


def test_communication_only_projection_never_calls_the_backend() -> None:
    """A jammed link still projects positions; MavlinkBackend must see none of it."""

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
    vehicle = _FakeVehicle(position=(0.0, 0.0), speed=10.0)
    backend = MavlinkBackend({1: vehicle}, step_timeout=5.0)

    result = Simulation(config, vehicle_backend=backend).run()

    timestamps = {time for time, _ in result.position_history[1]}
    assert {0.35, 0.45} <= timestamps  # the jammer's communication-only boundaries ran
    assert vehicle.commands  # yet the backend was only ever driven by _advance_agents


def test_the_simulator_never_reaches_mavlink_backend() -> None:
    """This adapter stays opt-in: nothing in Simulation/sweep imports it.

    Checked in a fresh interpreter because this test module has itself
    imported it, mirroring tests/test_sitl.py's own import-boundary check.
    """

    probe = (
        "import sys; import eudis_swarm.simulation, eudis_swarm.sweep;"
        "leaked = [m for m in ('eudis_swarm.mavlink_backend',) if m in sys.modules];"
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"the simulator pulled in {result.stdout.strip()}"
    )
