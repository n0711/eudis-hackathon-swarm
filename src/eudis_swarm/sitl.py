"""A software-in-the-loop vehicle, addressed in the simulator's local metres.

This is the world-truth side of the system. It commands a real ArduPilot
instance and reports where that vehicle actually is; it never touches any UAV's
beliefs, and nothing in the coordination layer imports it.

Two rules from ``docs/sitl.md`` are enforced here rather than assumed:

* ``pymavlink`` is imported on demand, so the package still imports, tests still
  run and the core still has no runtime dependencies when the ``sitl`` extra is
  absent.
* :attr:`SitlVehicle.deterministic` is ``False``. A run backed by real firmware
  is not reproducible and must never be used to produce a measured result.
"""

from __future__ import annotations

import time
from typing import Any

from .geodesy import LocalFrame, Position

#: What a caller is told when the optional extra is missing. Naming the exact
#: install command is the difference between a five-second fix and a search.
_MISSING_DEPENDENCY = (
    "software-in-the-loop support needs pymavlink, which is an optional extra. "
    'Install it with: pip install -e ".[sitl]"'
)

#: Position-only setpoints: every velocity, acceleration and yaw field ignored.
_POSITION_ONLY_TYPE_MASK = 0b0000111111111000


def _require_mavutil() -> Any:
    """Import pymavlink on demand, or explain precisely what is missing."""

    try:
        from pymavlink import mavutil
    except ModuleNotFoundError as error:  # pragma: no cover - needs the extra absent
        raise ModuleNotFoundError(_MISSING_DEPENDENCY) from error
    return mavutil


class SitlVehicle:
    """One ArduPilot vehicle, commanded and observed in local east/north metres.

    The local frame is fixed to wherever the vehicle first reports itself, so
    the simulator's ``(0.0, 0.0)`` is the vehicle's own starting point and no
    coordinates have to be agreed in advance.
    """

    #: Read by any caller that produces published numbers. Always False.
    deterministic = False

    def __init__(
        self,
        connection_string: str,
        *,
        stream_rate_hz: int = 5,
        timeout: float = 30.0,
    ) -> None:
        if not isinstance(connection_string, str) or not connection_string:
            raise ValueError("connection_string must be a non-empty string")
        if stream_rate_hz <= 0:
            raise ValueError("stream_rate_hz must be positive")
        if timeout <= 0.0:
            raise ValueError("timeout must be positive")
        self.connection_string = connection_string
        self.stream_rate_hz = stream_rate_hz
        self.timeout = timeout
        self._connection: Any = None
        self._frame: LocalFrame | None = None
        self._position: Position = (0.0, 0.0)
        self._altitude_m = 0.0

    @property
    def connected(self) -> bool:
        return self._connection is not None

    @property
    def frame(self) -> LocalFrame | None:
        """The local frame, once the vehicle has reported a position."""

        return self._frame

    @property
    def position(self) -> Position:
        """The last observed position, in local east/north metres."""

        return self._position

    @property
    def altitude_m(self) -> float:
        return self._altitude_m

    def connect(self) -> None:
        """Open the link, ask for telemetry, and pin the local frame origin."""

        mavutil = _require_mavutil()
        connection = mavutil.mavlink_connection(
            self.connection_string, source_system=255
        )
        heartbeat = connection.wait_heartbeat(timeout=self.timeout)
        if heartbeat is None:
            raise TimeoutError(
                f"no MAVLink heartbeat from {self.connection_string} "
                f"within {self.timeout:.0f}s"
            )
        self._connection = connection
        # SITL streams nothing until a ground station asks, so a silent link
        # here means an unasked-for stream rather than a broken vehicle.
        connection.mav.request_data_stream_send(
            connection.target_system,
            1,
            mavutil.mavlink.MAV_DATA_STREAM_ALL,
            self.stream_rate_hz,
            1,
        )
        self._await_first_position()

    def _await_first_position(self) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._pump_once() and self._frame is not None:
                return
        raise TimeoutError("no usable GLOBAL_POSITION_INT before the timeout")

    def _pump_once(self) -> bool:
        """Consume one message, updating position if it carried one."""

        assert self._connection is not None
        message = self._connection.recv_match(
            type="GLOBAL_POSITION_INT", blocking=True, timeout=2
        )
        if message is None:
            return False
        latitude = message.lat / 1e7
        longitude = message.lon / 1e7
        # A pre-EKF vehicle reports the null island; it is not a location.
        if latitude == 0.0 and longitude == 0.0:
            return False
        if self._frame is None:
            self._frame = LocalFrame(latitude, longitude)
        self._position = self._frame.to_local((latitude, longitude))
        self._altitude_m = message.relative_alt / 1000.0
        return True

    def observe(self, duration: float = 0.0) -> Position:
        """Drain telemetry for ``duration`` seconds and return the position."""

        if duration < 0.0:
            raise ValueError("duration must be non-negative")
        self._require_connection()
        deadline = time.monotonic() + duration
        self._pump_once()
        while time.monotonic() < deadline:
            self._pump_once()
        return self._position

    def command_toward(
        self, target: Position, *, altitude_m: float | None = None
    ) -> None:
        """Send a position setpoint, in local east/north metres."""

        mavutil = _require_mavutil()
        connection = self._require_connection()
        if self._frame is None:
            raise RuntimeError("the local frame is not fixed yet; call connect() first")
        latitude, longitude = self._frame.to_global(target)
        altitude = self._altitude_m if altitude_m is None else altitude_m
        connection.mav.set_position_target_global_int_send(
            0,
            connection.target_system,
            1,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            _POSITION_ONLY_TYPE_MASK,
            int(latitude * 1e7),
            int(longitude * 1e7),
            altitude,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    def arm_and_takeoff(
        self, altitude_m: float, *, settle_timeout: float = 90.0
    ) -> None:
        """Put the vehicle in GUIDED, arm it, and climb to ``altitude_m``.

        Arming is refused until the autopilot's own pre-arm checks pass, which
        on a fresh SITL instance means waiting for the EKF to settle. That wait
        is the reason this takes tens of seconds and cannot be hurried.
        """

        if altitude_m <= 0.0:
            raise ValueError("altitude_m must be positive")
        mavutil = _require_mavutil()
        connection = self._require_connection()

        connection.set_mode("GUIDED")
        deadline = time.monotonic() + settle_timeout
        while time.monotonic() < deadline:
            connection.mav.command_long_send(
                connection.target_system,
                1,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            ack = connection.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)
            if (
                ack is not None
                and ack.command == (mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM)
                and ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED
            ):
                break
        else:
            raise TimeoutError("the vehicle never accepted an arm command")

        connection.mav.command_long_send(
            connection.target_system,
            1,
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            altitude_m,
        )
        climb_deadline = time.monotonic() + settle_timeout
        while time.monotonic() < climb_deadline:
            self._pump_once()
            if self._altitude_m >= altitude_m * 0.95:
                return
        raise TimeoutError(
            f"reached only {self._altitude_m:.1f} m of a {altitude_m:.1f} m takeoff"
        )

    def distance_to(self, target: Position) -> float:
        east = target[0] - self._position[0]
        north = target[1] - self._position[1]
        return (east * east + north * north) ** 0.5

    def wait_until_within(
        self, target: Position, tolerance_m: float, timeout: float = 120.0
    ) -> bool:
        """Pump telemetry until the vehicle is close to ``target``."""

        if tolerance_m <= 0.0:
            raise ValueError("tolerance_m must be positive")
        self._require_connection()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._pump_once()
            if self.distance_to(target) <= tolerance_m:
                return True
        return False

    def _require_connection(self) -> Any:
        if self._connection is None:
            raise RuntimeError("not connected; call connect() first")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
