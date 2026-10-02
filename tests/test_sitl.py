"""The software-in-the-loop backend, tested without an autopilot attached.

CI has no vehicle, and it should not need one. What can be checked here is
everything that guards the rules in docs/sitl.md: that the module imports with
or without the optional extra, that a SITL run declares itself
non-deterministic, and that using it wrongly fails with something a person can
act on rather than an AttributeError three frames deep.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from eudis_swarm.sitl import _MISSING_DEPENDENCY, SitlVehicle


def test_a_sitl_run_declares_itself_non_deterministic() -> None:
    """The rule that keeps real firmware out of published numbers.

    Readable off the class, so a caller producing measured results can refuse a
    backend without first connecting to one.
    """

    assert SitlVehicle.deterministic is False


def test_the_missing_extra_names_the_command_that_fixes_it() -> None:
    assert "pymavlink" in _MISSING_DEPENDENCY
    assert '".[sitl]"' in _MISSING_DEPENDENCY


def test_the_simulator_never_reaches_the_sitl_backend() -> None:
    """The optional extra must stay optional.

    If anything in the simulator's import graph reached this module, a missing
    pymavlink would break the whole package rather than one backend. Checked in
    a fresh interpreter because this test module has itself imported it.
    """

    probe = (
        "import sys; import eudis_swarm.simulation, eudis_swarm.sweep;"
        "leaked = [m for m in ('eudis_swarm.sitl', 'pymavlink') if m in sys.modules];"
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


@pytest.mark.parametrize("connection_string", ["", None, 5])
def test_an_unusable_connection_string_is_refused(connection_string) -> None:
    with pytest.raises(ValueError, match="connection_string"):
        SitlVehicle(connection_string)  # type: ignore[arg-type]


@pytest.mark.parametrize("rate", [0, -1])
def test_a_nonpositive_stream_rate_is_refused(rate: int) -> None:
    with pytest.raises(ValueError, match="stream_rate_hz"):
        SitlVehicle("tcp:127.0.0.1:5760", stream_rate_hz=rate)


@pytest.mark.parametrize("timeout", [0.0, -3.0])
def test_a_nonpositive_timeout_is_refused(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        SitlVehicle("tcp:127.0.0.1:5760", timeout=timeout)


def test_a_fresh_vehicle_is_not_connected() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    assert vehicle.connected is False
    assert vehicle.frame is None
    assert vehicle.position == (0.0, 0.0)


def test_the_vehicle_reports_its_own_clock_not_this_machines() -> None:
    """Timing against SITL must use the autopilot's clock.

    SITL is routinely run faster than real time. Under --speedup 10 a
    wall-clock stopwatch reports a 60 m leg taking one second, which is how the
    first version of the model-error experiment produced nonsense.
    """

    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    assert vehicle.vehicle_time_s == 0.0


def test_commanding_before_connecting_says_so() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    with pytest.raises(RuntimeError, match="not connected"):
        vehicle.command_toward((10.0, 10.0))
    with pytest.raises(RuntimeError, match="not connected"):
        vehicle.observe()
    with pytest.raises(RuntimeError, match="not connected"):
        vehicle.wait_until_within((1.0, 1.0), tolerance_m=1.0)


def test_a_nonpositive_takeoff_altitude_is_refused() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    with pytest.raises(ValueError, match="altitude_m"):
        vehicle.arm_and_takeoff(0.0)


def test_a_nonpositive_tolerance_is_refused() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    with pytest.raises(ValueError, match="tolerance_m"):
        vehicle.wait_until_within((1.0, 1.0), tolerance_m=0.0)


def test_closing_an_unconnected_vehicle_is_harmless() -> None:
    SitlVehicle("tcp:127.0.0.1:5760").close()


def test_distance_is_measured_in_local_metres() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    assert vehicle.distance_to((3.0, 4.0)) == pytest.approx(5.0)


# --- behaviour against a fake MAVLink connection --------------------------------


class _Message:
    def __init__(self, kind: str, **fields: float) -> None:
        self._kind = kind
        self.__dict__.update(fields)

    def get_type(self) -> str:
        return self._kind


def _position(time_ms: int, lat: float, lon: float, alt_mm: int = 20000) -> _Message:
    return _Message(
        "GLOBAL_POSITION_INT",
        time_boot_ms=time_ms,
        lat=int(lat * 1e7),
        lon=int(lon * 1e7),
        relative_alt=alt_mm,
    )


class _FakeConnection:
    """Serves queued messages; reports a mode once set_mode has been asked."""

    def __init__(self, queue: list[_Message]) -> None:
        self.queue = queue
        self.flightmode = "GUIDED"
        self.requested_modes: list[str] = []
        self.target_system = 1
        self.mav = self
        self.setpoints: list[tuple] = []
        self.confirm_modes = True

    def recv_match(self, type=None, blocking=False, timeout=None):  # noqa: A002
        wanted = type if isinstance(type, list) else [type]
        while self.queue:
            message = self.queue.pop(0)
            if message.get_type() == "HEARTBEAT" and self.requested_modes:
                if self.confirm_modes:
                    self.flightmode = self.requested_modes[-1]
            if message.get_type() in wanted:
                return message
        return None

    def set_mode(self, mode: str) -> None:
        self.requested_modes.append(mode)

    def set_position_target_global_int_send(self, *fields) -> None:
        self.setpoints.append(fields)


class _FakeMavutil:
    class mavlink:  # noqa: N801 - mirrors pymavlink's attribute name
        MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6


def _connected(queue: list[_Message], origin=(35.0, 33.0)) -> SitlVehicle:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760", origin=origin)
    vehicle._connection = _FakeConnection(queue)  # noqa: SLF001
    return vehicle


def test_a_shared_origin_fixes_the_frame_before_any_telemetry() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760", origin=(35.0, 33.0))
    assert vehicle.frame is not None
    assert (vehicle.frame.origin_latitude, vehicle.frame.origin_longitude) == (
        35.0,
        33.0,
    )


def test_observe_reports_the_newest_queued_position_not_the_oldest() -> None:
    """A backlog must never be read head-first and presented as current."""

    vehicle = _connected(
        [
            _position(1000, 35.0, 33.0),
            _Message("HEARTBEAT"),
            _position(1100, 35.0001, 33.0),
            _position(1200, 35.0002, 33.0),
        ]
    )
    vehicle.observe()
    assert vehicle.vehicle_time_s == 1.2
    assert vehicle.position[1] == pytest.approx(22.26, abs=0.01)
    assert vehicle.altitude_m == 20.0


def test_a_pre_ekf_null_island_report_is_ignored() -> None:
    vehicle = _connected([_position(500, 0.0, 0.0)])
    vehicle.observe()
    assert vehicle.vehicle_time_s == 0.0


def test_set_mode_confirms_from_a_heartbeat_and_keeps_positions() -> None:
    vehicle = _connected(
        [
            _position(2000, 35.0, 33.0),
            _Message("HEARTBEAT"),
            _position(2100, 35.0, 33.0),
        ]
    )
    assert vehicle.flight_mode == "GUIDED"
    assert vehicle.hold("LOITER") is True
    assert vehicle.flight_mode == "LOITER"
    assert vehicle.vehicle_time_s == 2.1  # reports read while waiting were applied


def test_an_unconfirmed_mode_change_returns_false_rather_than_raising() -> None:
    vehicle = _connected([_Message("HEARTBEAT")])
    vehicle._connection.confirm_modes = False  # noqa: SLF001
    assert vehicle.set_mode("BRAKE", timeout=0.05) is False


@pytest.mark.parametrize("mode", ["RTL", "LAND", ""])
def test_hold_accepts_only_a_hover_mode(mode: str) -> None:
    with pytest.raises(ValueError, match="mode"):
        _connected([]).hold(mode)


def test_set_mode_validates_its_arguments() -> None:
    vehicle = _connected([])
    with pytest.raises(ValueError, match="mode"):
        vehicle.set_mode("")
    with pytest.raises(ValueError, match="timeout"):
        vehicle.set_mode("BRAKE", timeout=0.0)
    assert SitlVehicle("tcp:127.0.0.1:5760").flight_mode is None


def test_setpoints_hold_the_takeoff_altitude_once_it_is_known(monkeypatch) -> None:
    import eudis_swarm.sitl as sitl

    monkeypatch.setattr(sitl, "_require_mavutil", lambda: _FakeMavutil)
    vehicle = _connected([_position(1000, 35.0, 33.0, alt_mm=18700)])
    vehicle.observe()
    vehicle.command_toward((10.0, 0.0))
    vehicle._cruise_altitude_m = 20.0  # noqa: SLF001 - as arm_and_takeoff sets it
    vehicle.command_toward((10.0, 0.0))
    vehicle.command_toward((10.0, 0.0), altitude_m=30.0)
    altitudes = [fields[7] for fields in vehicle._connection.setpoints]  # noqa: SLF001
    assert altitudes == [18.7, 20.0, 30.0]
