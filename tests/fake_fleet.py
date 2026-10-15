"""Fake free-running autopilots for the multi-vehicle SITL tests.

A :class:`FakeWorld` owns one fake wall clock. Every :class:`FakeAutopilot`
derives its own vehicle clock from it -- ``offset + rate * wall`` -- and
reports position only at a fixed telemetry interval on that clock, exactly the
way ArduPilot SITL streams ``GLOBAL_POSITION_INT``. Reading telemetry advances
the shared wall clock, so while one vehicle is waited on the others keep
flying: the property that makes multi-vehicle lock-step drift in the first
place. Nothing here touches a socket, pymavlink, or real time.
"""

from __future__ import annotations

import math

from eudis_swarm.agent import Position, advance_point_mass


class FakeWorld:
    def __init__(self) -> None:
        self.wall_s = 0.0

    def advance(self, seconds: float) -> None:
        self.wall_s += seconds


class FakeAutopilot:
    """A free-running vehicle on a shared fake wall clock."""

    def __init__(
        self,
        world: FakeWorld,
        position: Position,
        *,
        rate: float = 5.0,
        offset_s: float = 10.0,
        report_interval_s: float = 0.1,
        speed: float = 10.0,
        read_cost_s: float = 0.01,
        hold_confirms: bool = True,
    ) -> None:
        self.world = world
        self.rate = rate
        self.offset_s = offset_s
        self.report_interval_s = report_interval_s
        self.speed = speed
        self.read_cost_s = read_cost_s
        self.hold_confirms = hold_confirms
        self._true_position = position
        self._true_time = self._clock()
        self._target: Position | None = None
        self._reported_position = position
        self._reported_time = 0.0
        self.mode = "STABILIZE"
        self.altitude_m = 0.0
        self.commands: list[Position] = []
        self.holds: list[str] = []
        self.modes: list[str] = []
        self.connected = False
        self.closed = False
        self.armed_to: float | None = None

    # --- the clock and the physics -------------------------------------
    def _clock(self) -> float:
        return self.offset_s + self.rate * self.world.wall_s

    def _integrate(self) -> None:
        now = self._clock()
        elapsed = now - self._true_time
        if elapsed > 0.0 and self._target is not None:
            self._true_position = advance_point_mass(
                self._true_position, self._target, self.speed, elapsed
            )
        self._true_time = now

    def _report(self) -> None:
        self._integrate()
        # The newest report on a stream that publishes every interval.
        latest = (
            math.floor(self._true_time / self.report_interval_s + 1e-9)
            * self.report_interval_s
        )
        if latest > self._reported_time:
            self._reported_time = latest
            self._reported_position = self._true_position

    # --- VehicleTelemetry -----------------------------------------------
    @property
    def position(self) -> Position:
        return self._reported_position

    @property
    def vehicle_time_s(self) -> float:
        return self._reported_time

    def command_toward(self, target: Position) -> None:
        self._integrate()
        if self.mode in ("BRAKE", "LOITER"):
            return  # a held autopilot ignores guided setpoints
        self._target = target
        self.commands.append(target)

    def observe(self, duration: float = 0.0) -> Position:
        self.world.advance(max(duration, self.read_cost_s))
        self._report()
        return self._reported_position

    def hold(self, mode: str = "BRAKE") -> bool:
        self._integrate()
        self.holds.append(mode)
        self._target = None
        if self.hold_confirms:
            self.mode = mode
        self.world.advance(self.read_cost_s)
        self._report()
        return self.hold_confirms

    # --- FleetVehicle -----------------------------------------------------
    def connect(self) -> None:
        self.connected = True
        self._report()

    def arm_and_takeoff(self, altitude_m: float) -> None:
        self.armed_to = altitude_m
        self.mode = "GUIDED"
        self.altitude_m = altitude_m
        self.world.advance(1.0)
        self._report()

    def set_mode(self, mode: str) -> bool:
        self.modes.append(mode)
        self.mode = mode
        return True

    def close(self) -> None:
        self.closed = True
