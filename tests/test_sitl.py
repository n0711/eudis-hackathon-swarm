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
    def __init__(self, kind: str, **fields: object) -> None:
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


class _FakeArmMavutil:
    class mavlink:  # noqa: N801 - mirrors pymavlink's attribute name
        MAV_CMD_COMPONENT_ARM_DISARM = 400
        MAV_CMD_DO_SET_MODE = 176
        MAV_RESULT_ACCEPTED = 0
        MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6


def test_arm_ignores_the_set_mode_acknowledgement(monkeypatch) -> None:
    """Only the arm command's own ACK counts; set_mode's must not end the wait."""

    import eudis_swarm.sitl as sitl

    monkeypatch.setattr(sitl, "_require_mavutil", lambda: _FakeArmMavutil)
    vehicle = _connected(
        [
            _Message("COMMAND_ACK", command=176, result=0),  # set_mode's ACK
            _Message("COMMAND_ACK", command=400, result=4),  # arm refused (EKF)
            _Message("COMMAND_ACK", command=400, result=0),  # arm accepted
            _Message("COMMAND_ACK", command=400, result=0),
        ]
    )
    sent: list[tuple] = []
    vehicle._connection.command_long_send = lambda *a: sent.append(a)  # noqa: SLF001
    vehicle.arm(settle_timeout=5.0)
    assert vehicle._connection.requested_modes == ["GUIDED"]  # noqa: SLF001
    assert len(sent) == 3  # retried through the stale and the refused ACKs
    assert len(vehicle._connection.queue) == 1  # noqa: SLF001 - stopped at the first accept


def test_arm_times_out_when_never_accepted(monkeypatch) -> None:
    import eudis_swarm.sitl as sitl

    monkeypatch.setattr(sitl, "_require_mavutil", lambda: _FakeArmMavutil)
    vehicle = _connected([])
    vehicle._connection.command_long_send = lambda *a: None  # noqa: SLF001
    with pytest.raises(TimeoutError, match="arm command"):
        vehicle.arm(settle_timeout=0.01)


def test_a_rover_hold_is_accepted() -> None:
    vehicle = _connected([_Message("HEARTBEAT")])
    assert vehicle.hold("HOLD") is True


def test_two_vehicles_with_different_homes_share_one_frame(monkeypatch) -> None:
    """One mission frame: positions comparable, a target is one global point."""

    import eudis_swarm.sitl as sitl

    monkeypatch.setattr(sitl, "_require_mavutil", lambda: _FakeMavutil)
    origin = (35.1264, 33.4299)
    west = _connected([_position(1000, 35.1264, 33.4299)], origin=origin)
    east = _connected([_position(1000, 35.1264, 33.4302)], origin=origin)
    west.observe()
    east.observe()

    # MAVLink carries degrees * 1e7 as integers: centimetre-level rounding.
    assert west.position == pytest.approx((0.0, 0.0), abs=0.02)
    assert east.position[0] == pytest.approx(27.3, abs=0.1)  # 0.0003 deg east
    assert east.position[1] == pytest.approx(west.position[1], abs=1e-9)

    west.command_toward((50.0, 40.0), altitude_m=20.0)
    east.command_toward((50.0, 40.0), altitude_m=25.0)
    (west_fields,), (east_fields,) = (
        west._connection.setpoints,  # noqa: SLF001
        east._connection.setpoints,  # noqa: SLF001
    )
    assert west_fields[5:7] == east_fields[5:7]  # identical lat/lon setpoint
    assert (west_fields[7], east_fields[7]) == (20.0, 25.0)  # own altitude bands

    # Without a shared origin each frame pins to its own home: not comparable.
    alone = SitlVehicle("tcp:127.0.0.1:5770")
    alone._connection = _FakeConnection([_position(1000, 35.1264, 33.4302)])  # noqa: SLF001
    alone.observe()
    assert alone.position == (0.0, 0.0)


class _ParamConnection(_FakeConnection):
    """Echoes PARAM_SET as PARAM_VALUE, optionally clamped or never."""

    def __init__(
        self, queue: list[_Message], *, clamp: float | None = None, echo: bool = True
    ) -> None:
        super().__init__(queue)
        self.params: dict[str, float] = {"PSC_NE_POS_P": 1.0}
        self.clamp = clamp
        self.echo = echo

    def param_set_send(
        self, _system, _component, name: bytes, value: float, _type
    ) -> None:
        stored = value if self.clamp is None else min(value, self.clamp)
        self.params[name.decode()] = stored
        if self.echo:
            self.queue.append(
                _Message("PARAM_VALUE", param_id=name.decode(), param_value=stored)
            )

    def param_request_read_send(self, _system, _component, name: bytes, _index) -> None:
        key = name.decode()
        if key in self.params and self.echo:
            self.queue.append(
                _Message(
                    "PARAM_VALUE",
                    param_id=key.encode() + b"\x00\x00",
                    param_value=self.params[key],
                )
            )


class _FakeParamMavutil(_FakeMavutil):
    class mavlink(_FakeMavutil.mavlink):  # noqa: N801
        MAV_PARAM_TYPE_REAL32 = 9


def test_set_param_returns_the_autopilots_own_echo(monkeypatch) -> None:
    import eudis_swarm.sitl as sitl

    monkeypatch.setattr(sitl, "_require_mavutil", lambda: _FakeParamMavutil)
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    vehicle._connection = _ParamConnection([])  # noqa: SLF001
    assert vehicle.set_param("psc_ne_pos_p", 2.0) == 2.0
    assert vehicle.get_param("PSC_NE_POS_P") == 2.0  # null-padded bytes id decoded

    clamped = SitlVehicle("tcp:127.0.0.1:5760")
    clamped._connection = _ParamConnection([], clamp=1.5)  # noqa: SLF001
    with pytest.raises(TimeoutError, match="not confirmed"):
        clamped.set_param("PSC_NE_POS_P", 2.0, timeout=0.3)
    silent = SitlVehicle("tcp:127.0.0.1:5760")
    silent._connection = _ParamConnection([], echo=False)  # noqa: SLF001
    with pytest.raises(TimeoutError, match="no value"):
        silent.get_param("PSC_NE_POS_P", timeout=0.3)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("", 1.0, "1-16 characters"),
        ("X" * 17, 1.0, "1-16"),
        ("OK", float("inf"), "finite"),
    ],
)
def test_set_param_validates_its_arguments(
    name: str, value: float, message: str
) -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    vehicle._connection = _ParamConnection([])  # noqa: SLF001
    with pytest.raises(ValueError, match=message):
        vehicle.set_param(name, value)


def _moving(time_ms: int, lat: float, vx: int, vy: int) -> _Message:
    message = _position(time_ms, lat, 33.0)
    message.vx, message.vy = vx, vy  # type: ignore[attr-defined]
    return message


def test_record_keeps_every_report_with_velocity_in_east_north() -> None:
    vehicle = _connected(
        [
            _moving(1000, 35.0, 100, 300),
            _Message("HEARTBEAT"),
            _moving(1100, 35.00001, 100, 300),
            _moving(1200, 35.00002, 0, -50),
        ]
    )
    samples = vehicle.record(0.15)
    assert [round(s[0], 3) for s in samples] == [1.0, 1.1, 1.2]
    assert samples[0][4:] == (3.0, 1.0)  # (east = vy, north = vx) in m/s
    assert vehicle.velocity == (-0.5, 0.0)
    with pytest.raises(ValueError, match="non-negative"):
        vehicle.record(-1.0)
    with pytest.raises(TimeoutError, match="recorded only"):
        _connected([]).record(5.0, wall_timeout=0.05)
