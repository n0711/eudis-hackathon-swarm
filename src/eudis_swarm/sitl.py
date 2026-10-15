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

import math
import time
from typing import Any

from .geodesy import LocalFrame, Position

#: What a caller is told when the optional extra is missing. Naming the exact
#: install command is the difference between a five-second fix and a search.
_MISSING_DEPENDENCY = (
    "software-in-the-loop support needs pymavlink, which is an optional extra. "
    'Install it with: pip install -e ".[sitl]"'
)


def _param_id(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 16:
        raise ValueError("a parameter name is 1-16 characters")
    return name.upper()


#: Upper bound on one backlog drain, so a link streaming faster than it can be
#: read still returns control.
_MAX_DRAIN_MESSAGES = 1000

#: Position-only setpoints: every velocity, acceleration and yaw field ignored.
_POSITION_ONLY_TYPE_MASK = 0b0000111111111000


def _require_mavutil() -> Any:
    """Import pymavlink on demand, or explain precisely what is missing."""

    try:
        # The `sitl` extra is deliberately absent from the default and CI
        # installs, so a type checker cannot resolve this import there.
        from pymavlink import mavutil  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError as error:  # pragma: no cover - needs the extra absent
        raise ModuleNotFoundError(_MISSING_DEPENDENCY) from error
    return mavutil


class SitlVehicle:
    """One ArduPilot vehicle, commanded and observed in local east/north metres.

    By default the local frame is fixed to wherever the vehicle first reports
    itself, so the simulator's ``(0.0, 0.0)`` is the vehicle's own starting
    point and no coordinates have to be agreed in advance. Several vehicles
    flown together pass one shared ``origin`` instead, so that one local
    metre means the same place for all of them.
    """

    #: Read by any caller that produces published numbers. Always False.
    deterministic = False

    def __init__(
        self,
        connection_string: str,
        *,
        stream_rate_hz: int = 5,
        timeout: float = 30.0,
        origin: tuple[float, float] | None = None,
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
        # A shared origin puts several vehicles in one frame, so their local
        # positions are directly comparable; without one, each vehicle's frame
        # is pinned to wherever it first reports itself.
        self._frame: LocalFrame | None = (
            None if origin is None else LocalFrame(origin[0], origin[1])
        )
        self._position: Position = (0.0, 0.0)
        self._altitude_m = 0.0
        self._cruise_altitude_m: float | None = None
        self._vehicle_time_s = 0.0
        self._velocity: Position = (0.0, 0.0)

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

    @property
    def vehicle_time_s(self) -> float:
        """Seconds on the autopilot's own clock, from its last position report.

        Not wall-clock. SITL is routinely run faster than real time, and under
        ``--speedup 10`` a wall-clock stopwatch reports a 60 m leg taking one
        second. Any timing measured against this vehicle must use this clock.
        """

        return self._vehicle_time_s

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
        return self._apply_position(message)

    def _pump_latest(self) -> bool:
        """Drain every queued position report, keeping only the newest.

        A link that has not been read for a while holds a backlog, and reading
        one message from it returns the *oldest*. When several vehicles are
        stepped in turn that backlog is the normal case, so reporting the head
        of the queue would present seconds-old telemetry as current. Block for
        one message only when nothing at all is queued.
        """

        assert self._connection is not None
        updated = False
        for _ in range(_MAX_DRAIN_MESSAGES):
            message = self._connection.recv_match(
                type="GLOBAL_POSITION_INT", blocking=False
            )
            if message is None:
                break
            updated = self._apply_position(message) or updated
        if updated:
            return True
        return self._pump_once()

    def _apply_position(self, message: Any) -> bool:
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
        self._vehicle_time_s = message.time_boot_ms / 1000.0
        # GLOBAL_POSITION_INT carries cm/s north (vx) and east (vy); the
        # local frame is east/north.
        self._velocity = (
            float(getattr(message, "vy", 0)) / 100.0,
            float(getattr(message, "vx", 0)) / 100.0,
        )
        return True

    @property
    def velocity(self) -> Position:
        """Ground velocity (east, north) in m/s, from the last position report."""

        return self._velocity

    def record(
        self, vehicle_seconds: float, *, wall_timeout: float = 120.0
    ) -> list[tuple[float, float, float, float, float, float]]:
        """Every position report for ``vehicle_seconds`` of vehicle time.

        Unlike :meth:`observe`, nothing is skipped: each report becomes one
        ``(t, east, north, altitude, v_east, v_north)`` sample, which is what a
        step-response or braking measurement needs.
        """

        if vehicle_seconds < 0.0:
            raise ValueError("vehicle_seconds must be non-negative")
        self._require_connection()
        samples: list[tuple[float, float, float, float, float, float]] = []
        start: float | None = None
        deadline = time.monotonic() + wall_timeout
        while time.monotonic() < deadline:
            if not self._pump_once():
                continue
            first = self._vehicle_time_s if start is None else start
            start = first
            samples.append(
                (
                    self._vehicle_time_s,
                    self._position[0],
                    self._position[1],
                    self._altitude_m,
                    self._velocity[0],
                    self._velocity[1],
                )
            )
            if self._vehicle_time_s - first >= vehicle_seconds:
                return samples
        raise TimeoutError(
            f"recorded only {len(samples)} reports before the wall-clock timeout"
        )

    def get_param(self, name: str, *, timeout: float = 10.0) -> float:
        """Read one autopilot parameter, retrying once a second."""

        connection = self._require_connection()
        wanted = _param_id(name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            connection.mav.param_request_read_send(
                connection.target_system, 1, wanted.encode("ascii"), -1
            )
            value = self._await_param(wanted, min(deadline, time.monotonic() + 1.0))
            if value is not None:
                return value
        raise TimeoutError(f"no value for parameter {name} within {timeout:.0f}s")

    def set_param(self, name: str, value: float, *, timeout: float = 10.0) -> float:
        """Set one autopilot parameter and return the value the autopilot reports.

        The autopilot answers a PARAM_SET with PARAM_VALUE; that echo, not the
        request, is what is returned, so a refused or clamped value is visible.
        """

        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("parameter value must be a finite number")
        connection = self._require_connection()
        mavutil = _require_mavutil()
        wanted = _param_id(name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            connection.mav.param_set_send(
                connection.target_system,
                1,
                wanted.encode("ascii"),
                float(value),
                mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
            )
            echoed = self._await_param(wanted, min(deadline, time.monotonic() + 1.0))
            if echoed is not None and math.isclose(
                echoed, float(value), rel_tol=1e-5, abs_tol=1e-6
            ):
                return echoed
        raise TimeoutError(f"parameter {name} was not confirmed as {value}")

    def _await_param(self, wanted: str, until: float) -> float | None:
        assert self._connection is not None
        while time.monotonic() < until:
            message = self._connection.recv_match(
                type="PARAM_VALUE", blocking=True, timeout=0.5
            )
            if message is None:
                continue
            name = message.param_id
            if isinstance(name, bytes):
                name = name.decode("ascii", "replace")
            if name.rstrip("\x00") == wanted:
                return float(message.param_value)
        return None

    def observe(self, duration: float = 0.0) -> Position:
        """Drain telemetry for ``duration`` seconds and return the newest position."""

        if duration < 0.0:
            raise ValueError("duration must be non-negative")
        self._require_connection()
        deadline = time.monotonic() + duration
        self._pump_latest()
        while time.monotonic() < deadline:
            self._pump_latest()
        return self._position

    def command_toward(
        self, target: Position, *, altitude_m: float | None = None
    ) -> None:
        """Send a position setpoint, in local east/north metres."""

        connection = self._require_connection()
        mavutil = _require_mavutil()
        if self._frame is None:
            raise RuntimeError("the local frame is not fixed yet; call connect() first")
        latitude, longitude = self._frame.to_global(target)
        # Re-commanding the *measured* altitude every step lets small errors
        # accumulate into a slow climb or sink; hold the takeoff altitude.
        if altitude_m is not None:
            altitude = altitude_m
        elif self._cruise_altitude_m is not None:
            altitude = self._cruise_altitude_m
        else:
            altitude = self._altitude_m
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

    def arm(self, *, settle_timeout: float = 90.0) -> None:
        """Put the vehicle in GUIDED and arm it, without leaving the ground.

        This is the whole start sequence for a ground vehicle (ArduRover), and
        the first half of :meth:`arm_and_takeoff` for a copter. Arming is
        refused until the autopilot's own pre-arm checks pass, which on a fresh
        SITL instance means waiting for the EKF to settle; that wait is why
        this takes tens of seconds and cannot be hurried.

        Only an acknowledgement *of the arm command itself* counts: pymavlink's
        ``set_mode`` produces its own accepted COMMAND_ACK, and taking that one
        for the arm result returns while the vehicle is still disarmed -- after
        which a rover silently ignores every position target.
        """

        connection = self._require_connection()
        mavutil = _require_mavutil()

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
                return
        raise TimeoutError("the vehicle never accepted an arm command")

    def arm_and_takeoff(
        self, altitude_m: float, *, settle_timeout: float = 90.0
    ) -> None:
        """Put the vehicle in GUIDED, arm it, and climb to ``altitude_m``.

        Every later setpoint holds ``altitude_m``, which is how several copters
        flown together keep to their own altitude band.
        """

        if altitude_m <= 0.0:
            raise ValueError("altitude_m must be positive")
        connection = self._require_connection()
        mavutil = _require_mavutil()
        self.arm(settle_timeout=settle_timeout)

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
        self._cruise_altitude_m = altitude_m
        climb_deadline = time.monotonic() + settle_timeout
        while time.monotonic() < climb_deadline:
            self._pump_once()
            if self._altitude_m >= altitude_m * 0.95:
                return
        raise TimeoutError(
            f"reached only {self._altitude_m:.1f} m of a {altitude_m:.1f} m takeoff"
        )

    @property
    def flight_mode(self) -> str | None:
        """The autopilot's mode name from its last heartbeat, if known."""

        if self._connection is None:
            return None
        mode = getattr(self._connection, "flightmode", None)
        return mode if isinstance(mode, str) else None

    def set_mode(self, mode: str, *, timeout: float = 5.0) -> bool:
        """Request a flight mode and report whether the autopilot confirmed it.

        The request is repeated once a second until a heartbeat reports the
        mode or ``timeout`` wall-clock seconds pass. ``False`` is a result, not
        an exception: a caller stopping a vehicle wants to record that the
        confirmation never came, not lose the record to a traceback.
        """

        if not isinstance(mode, str) or not mode:
            raise ValueError("mode must be a non-empty string")
        if timeout <= 0.0:
            raise ValueError("timeout must be positive")
        connection = self._require_connection()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            connection.set_mode(mode)
            resend_at = min(deadline, time.monotonic() + 1.0)
            while time.monotonic() < resend_at:
                # Position reports read while waiting are applied, not thrown
                # away, so the position after a mode change is current.
                message = connection.recv_match(
                    type=["HEARTBEAT", "GLOBAL_POSITION_INT"],
                    blocking=True,
                    timeout=0.5,
                )
                if message is not None and message.get_type() == "GLOBAL_POSITION_INT":
                    self._apply_position(message)
                if self.flight_mode == mode:
                    self._pump_latest()
                    return True
        return self.flight_mode == mode

    def hold(self, mode: str = "BRAKE") -> bool:
        """Stop horizontal motion and hold position, staying airborne.

        This is the SITL-side fail-stop: a simulated failure must actually
        stop the aircraft rather than leave it flying to its last setpoint.
        ArduCopter's BRAKE stops hardest; LOITER is gentler. Both hover.
        ArduRover's HOLD stops a ground vehicle where it is.
        """

        if mode not in ("BRAKE", "LOITER", "HOLD"):
            raise ValueError("mode must be BRAKE, LOITER or HOLD")
        return self.set_mode(mode)

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
