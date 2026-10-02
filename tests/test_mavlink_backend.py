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
from fake_fleet import FakeAutopilot, FakeWorld

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
        self.holds: list[str] = []

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

    def hold(self, mode: str = "BRAKE") -> bool:
        self.holds.append(mode)
        self._target = self._position
        return True


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

    def hold(self, mode: str = "BRAKE") -> bool:
        return False


class _ForbiddenVehicle:
    """Raises if touched at all -- proves a non-responsive agent is skipped."""

    def command_toward(self, target: Position) -> None:
        raise AssertionError("command_toward must not be called")

    def observe(self, duration: float = 0.0) -> Position:
        raise AssertionError("observe must not be called")

    def hold(self, mode: str = "BRAKE") -> bool:
        raise AssertionError("hold must not be called")

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


# --- multi-vehicle anchored lock-step and fail-stop ---------------------------


def _fleet(
    rates: tuple[float, ...] = (5.0, 5.0, 5.0),
) -> tuple[FakeWorld, dict[int, FakeAutopilot], list[Agent]]:
    world = FakeWorld()
    vehicles = {
        index + 1: FakeAutopilot(
            world, (10.0 * index, 0.0), rate=rate, offset_s=10.0 + 3.0 * index
        )
        for index, rate in enumerate(rates)
    }
    agents = [
        Agent(agent_id=agent_id, position=vehicle.position, speed=10.0)
        for agent_id, vehicle in vehicles.items()
    ]
    return world, vehicles, agents


def _step_all(
    backend: MavlinkBackend,
    agents: list[Agent],
    steps: int,
    dt: float = 0.25,
    *,
    first: int = 1,
) -> None:
    for index in range(first, first + steps):
        for agent in agents:
            backend.move_toward(agent, (agent.agent_id * 10.0, 500.0), dt)
        backend.synchronize(agents, round(index * dt, 12))


def test_anchored_drift_stays_bounded_over_many_steps() -> None:
    """Three free-running clocks stepped in turn never drift apart.

    The pre-anchor rule (``last observed + elapsed``) rounds every step up to
    the next telemetry report, so its error grew by up to one report interval
    per step; anchoring to cumulative logical time bounds it instead.
    """

    _world, vehicles, agents = _fleet()
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)

    _step_all(backend, agents, 200)

    samples = backend.samples
    assert len(samples) == 600
    assert all(sample.drift_s >= 0.0 for sample in samples)  # never reported early
    interval = 0.1
    poll_in_vehicle_seconds = 0.05 * 5.0
    assert max(sample.drift_s for sample in samples) <= (
        interval + poll_in_vehicle_seconds + 1e-9
    )
    for agent_id, vehicle in vehicles.items():
        last = [sample for sample in samples if sample.agent_id == agent_id][-1]
        assert last.logical_elapsed_s == pytest.approx(50.0)
        anchor = last.target_vehicle_time_s - last.logical_elapsed_s
        # Cumulative: vehicle time tracks anchor + 50 s, not 200 rounded steps.
        assert vehicle.vehicle_time_s - anchor == pytest.approx(50.0, abs=0.4)


def test_every_vehicle_is_anchored_in_one_pass_before_any_waits() -> None:
    _world, vehicles, agents = _fleet()
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)

    _step_all(backend, agents, 1)

    first = {sample.agent_id: sample for sample in backend.samples}
    anchors = {
        agent_id: sample.target_vehicle_time_s - sample.logical_elapsed_s
        for agent_id, sample in first.items()
    }
    # Each clock read back to back: no vehicle got a waited-on head start.
    assert all(sample.drift_s <= 0.35 for sample in first.values())
    assert sorted(anchors) == [1, 2, 3]


def test_a_vehicle_whose_clock_runs_fast_shows_up_as_measured_drift() -> None:
    """A faster instance cannot be slowed down; its lead is recorded, not hidden."""

    _world, vehicles, agents = _fleet(rates=(5.0, 5.0, 6.0))
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)

    _step_all(backend, agents, 100)

    fast = [sample.drift_s for sample in backend.samples if sample.agent_id == 3]
    steady = [sample.drift_s for sample in backend.samples if sample.agent_id == 1]
    assert fast[-1] > fast[10] > 0.5  # grows with the rate difference
    assert max(steady) < 0.5


def test_a_hovering_vehicle_is_stepped_by_synchronize_without_a_setpoint() -> None:
    _world, vehicles, agents = _fleet()
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)

    backend.move_toward(agents[0], (0.0, 50.0), 0.25)
    backend.synchronize(agents, 0.25)

    by_agent = {sample.agent_id: sample for sample in backend.drain_samples()}
    assert by_agent[1].commanded is True
    assert by_agent[2].commanded is False and by_agent[3].commanded is False
    assert vehicles[2].commands == [] and vehicles[3].commands == []
    assert agents[1].position == vehicles[2].position  # observed, not remembered
    assert backend.drain_samples() == ()  # drained exactly once


def test_synchronize_refuses_to_go_back_in_time() -> None:
    _world, vehicles, agents = _fleet()
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)
    backend.synchronize(agents, 1.0)

    with pytest.raises(ValueError, match="synchronize timestamp"):
        backend.synchronize(agents, 0.5)


def test_a_report_must_be_strictly_newer_than_the_last_one() -> None:
    """Telemetry that stops arriving is a timeout even if the clock was ahead."""

    world = FakeWorld()
    vehicle = FakeAutopilot(world, (0.0, 0.0), rate=5.0)
    backend = MavlinkBackend({1: vehicle}, step_timeout=0.2)
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)
    backend.move_toward(agent, (0.0, 10.0), 0.25)
    reported = agent.position

    vehicle.rate = 0.0  # the stream freezes: no newer report will ever come
    with pytest.raises(TimeoutError, match="stale"):
        backend.move_toward(agent, (0.0, 10.0), 0.0)
    assert agent.position == reported


def test_fail_stop_is_sent_exactly_once_on_physical_failure() -> None:
    _world, vehicles, agents = _fleet()
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0, fail_stop_mode="LOITER")
    _step_all(backend, agents, 4)

    agents[1].inject_failure(1.0)
    for timestamp in (1.0, 1.25, 1.5):
        backend.apply_physical_faults(agents, timestamp)

    assert vehicles[2].holds == ["LOITER"]  # once, however often it is asked
    assert vehicles[1].holds == [] and vehicles[3].holds == []
    (event,) = backend.fail_stops
    assert (event.agent_id, event.logical_time, event.mode) == (2, 1.0, "LOITER")
    assert event.confirmed is True


def test_fail_stop_actually_stops_the_vehicle_and_it_is_never_stepped_again() -> None:
    _world, vehicles, agents = _fleet()
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)
    _step_all(backend, agents, 4)
    agents[1].inject_failure(1.0)
    backend.apply_physical_faults(agents, 1.0)
    frozen_truth = agents[1].position
    first = backend.observe_held(2)
    assert first is not None

    _step_all(backend, agents, 4, first=5)  # move_toward and synchronize skip it
    later = backend.observe_held(2)

    assert later is not None and later[0] > first[0]  # still observed...
    assert later[1] == first[1]  # ...and no longer moving
    assert agents[1].position == frozen_truth  # truth is never rewritten
    assert backend.observe_held(1) is None  # not held: nothing to observe


def test_a_declared_failure_never_triggers_the_fail_stop() -> None:
    """A wrong quorum must not stop a vehicle that is still flying."""

    _world, vehicles, agents = _fleet()
    backend = MavlinkBackend(dict(vehicles), step_timeout=5.0)
    agents[1].declare_failed()

    backend.apply_physical_faults(agents, 0.0)
    _step_all(backend, agents, 2)

    assert vehicles[2].holds == []
    assert backend.fail_stops == ()
    assert vehicles[2].commands  # still flying its task


def test_an_unconfirmed_hold_is_recorded_as_unconfirmed_and_not_retried() -> None:
    world = FakeWorld()
    vehicle = FakeAutopilot(world, (0.0, 0.0), hold_confirms=False)
    backend = MavlinkBackend({1: vehicle}, step_timeout=5.0)
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)
    agent.inject_failure(0.0)

    backend.apply_physical_faults([agent], 0.0)
    backend.apply_physical_faults([agent], 0.25)

    assert vehicle.holds == ["BRAKE"]
    assert backend.fail_stops[0].confirmed is False


def test_fail_stop_can_be_disabled() -> None:
    backend = MavlinkBackend({1: _ForbiddenVehicle()}, fail_stop_mode=None)
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)
    agent.inject_failure(0.0)

    backend.apply_physical_faults([agent], 0.0)  # must not touch the vehicle

    assert backend.fail_stop_mode is None
    assert backend.fail_stops == ()


def test_unmapped_agents_are_ignored_by_the_live_hooks() -> None:
    backend = MavlinkBackend({1: _ForbiddenVehicle()})
    other = Agent(agent_id=2, position=(0.0, 0.0), speed=10.0)
    other.inject_failure(0.0)

    backend.apply_physical_faults([other], 0.0)
    backend.synchronize([other], 0.25)

    assert backend.agent_ids == (1,)


@pytest.mark.parametrize("mode", ["RTL", "LAND", "", "brake"])
def test_only_a_safe_hold_mode_is_accepted(mode: str) -> None:
    with pytest.raises(ValueError, match="fail_stop_mode"):
        MavlinkBackend({1: _FakeVehicle()}, fail_stop_mode=mode)
