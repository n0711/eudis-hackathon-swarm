"""The VehicleBackend seam: both backends must satisfy the same contract.

``MavlinkBackend`` is tested against a fake MAVLink connection standing in
for ``pymavlink``'s ``mavutil`` -- no live ArduPilot, no SITL instance, and
no network are needed for these tests to pass, matching how ``sitl.py``
itself is guarded (``_require_mavutil`` is patched here the same way a real
``pymavlink`` module would be looked up).
"""

from __future__ import annotations

from typing import Any

import pytest

import eudis_swarm.sitl as sitl_module
from eudis_swarm.agent import advance_point_mass
from eudis_swarm.geodesy import LocalFrame
from eudis_swarm.sitl import SitlVehicle
from eudis_swarm.vehicle_backend import MavlinkBackend, PointMassBackend, VehicleBackend

ORIGIN_LATITUDE = 37.0
ORIGIN_LONGITUDE = -122.0


class _FakeMessage:
    """Stands in for a decoded ``GLOBAL_POSITION_INT`` MAVLink message."""

    def __init__(
        self, latitude: float, longitude: float, altitude_m: float, time_s: float
    ):
        self.lat = int(latitude * 1e7)
        self.lon = int(longitude * 1e7)
        self.relative_alt = int(altitude_m * 1000)
        self.time_boot_ms = int(time_s * 1000)


class _FakeMav:
    """Stands in for ``connection.mav``: records every setpoint sent."""

    def __init__(self) -> None:
        self.sent_setpoints: list[tuple[Any, ...]] = []

    def set_position_target_global_int_send(self, *args: Any) -> None:
        self.sent_setpoints.append(args)


class _FakeConnection:
    """Stands in for a ``pymavlink`` connection: no socket, no autopilot."""

    def __init__(self, messages: list[_FakeMessage]) -> None:
        self.target_system = 1
        self.mav = _FakeMav()
        self._messages = list(messages)

    def recv_match(
        self, *, type: str, blocking: bool, timeout: float
    ) -> _FakeMessage | None:
        if self._messages:
            return self._messages.pop(0)
        return None


class _FakeMavlinkConstants:
    MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6


class _FakeMavutil:
    mavlink = _FakeMavlinkConstants()


@pytest.fixture
def frame() -> LocalFrame:
    return LocalFrame(ORIGIN_LATITUDE, ORIGIN_LONGITUDE)


def _connected_vehicle(
    monkeypatch: pytest.MonkeyPatch, frame: LocalFrame, messages: list[_FakeMessage]
) -> tuple[SitlVehicle, _FakeConnection]:
    monkeypatch.setattr(sitl_module, "_require_mavutil", lambda: _FakeMavutil())
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    connection = _FakeConnection(messages)
    vehicle._connection = connection  # simulate an already-connected vehicle
    vehicle._frame = frame
    vehicle._position = (0.0, 0.0)
    return vehicle, connection


# --- PointMassBackend: must match the existing model exactly -------------


def test_point_mass_backend_starts_at_the_given_position() -> None:
    backend = PointMassBackend((1.0, 2.0), speed=5.0)
    assert backend.position == (1.0, 2.0)


def test_point_mass_backend_does_not_move_until_advanced() -> None:
    backend = PointMassBackend((1.0, 2.0), speed=5.0)
    backend.command_toward((100.0, 100.0))
    assert backend.position == (1.0, 2.0)


@pytest.mark.parametrize(
    "origin,target,speed,elapsed",
    [
        ((0.0, 0.0), (10.0, 0.0), 2.0, 1.0),
        ((0.0, 0.0), (10.0, 0.0), 2.0, 100.0),
        ((5.0, 5.0), (5.0, 5.0), 3.0, 4.0),
        ((0.0, 0.0), (3.0, 4.0), 1.5, 2.0),
    ],
)
def test_point_mass_backend_matches_advance_point_mass_directly(
    origin: tuple[float, float],
    target: tuple[float, float],
    speed: float,
    elapsed: float,
) -> None:
    backend = PointMassBackend(origin, speed)
    backend.command_toward(target)
    assert backend.advance(elapsed) == advance_point_mass(
        origin, target, speed, elapsed
    )


def test_point_mass_backend_accumulates_across_several_advances() -> None:
    origin = (0.0, 0.0)
    target = (10.0, 0.0)
    speed = 2.0
    backend = PointMassBackend(origin, speed)
    backend.command_toward(target)

    position = origin
    for _ in range(3):
        position = advance_point_mass(position, target, speed, 1.0)
        assert backend.advance(1.0) == position


def test_point_mass_backend_conforms_to_the_protocol() -> None:
    assert isinstance(PointMassBackend((0.0, 0.0), speed=1.0), VehicleBackend)


# --- MavlinkBackend: adapts SitlVehicle, no live autopilot needed --------


def test_mavlink_backend_conforms_to_the_protocol(
    monkeypatch: pytest.MonkeyPatch, frame: LocalFrame
) -> None:
    vehicle, _ = _connected_vehicle(monkeypatch, frame, messages=[])
    assert isinstance(MavlinkBackend(vehicle), VehicleBackend)


def test_mavlink_backend_reads_position_from_the_vehicle(
    monkeypatch: pytest.MonkeyPatch, frame: LocalFrame
) -> None:
    vehicle, _ = _connected_vehicle(monkeypatch, frame, messages=[])
    vehicle._position = (7.0, 9.0)
    backend = MavlinkBackend(vehicle)
    assert backend.position == (7.0, 9.0)


def test_mavlink_backend_command_toward_sends_a_setpoint(
    monkeypatch: pytest.MonkeyPatch, frame: LocalFrame
) -> None:
    vehicle, connection = _connected_vehicle(monkeypatch, frame, messages=[])
    backend = MavlinkBackend(vehicle)

    backend.command_toward((50.0, 25.0))

    assert len(connection.mav.sent_setpoints) == 1
    expected_latitude, expected_longitude = frame.to_global((50.0, 25.0))
    sent = connection.mav.sent_setpoints[0]
    # positional layout mirrors SitlVehicle.command_toward's call, latitude
    # and longitude are the two integer 1e7-scaled fields.
    sent_latitude_1e7, sent_longitude_1e7 = sent[5], sent[6]
    assert sent_latitude_1e7 == pytest.approx(int(expected_latitude * 1e7), abs=1)
    assert sent_longitude_1e7 == pytest.approx(int(expected_longitude * 1e7), abs=1)


def test_mavlink_backend_advance_reads_back_real_telemetry(
    monkeypatch: pytest.MonkeyPatch, frame: LocalFrame
) -> None:
    target_local = (12.0, -4.0)
    target_latitude, target_longitude = frame.to_global(target_local)
    message = _FakeMessage(
        target_latitude, target_longitude, altitude_m=15.0, time_s=3.0
    )
    vehicle, _ = _connected_vehicle(monkeypatch, frame, messages=[message])
    backend = MavlinkBackend(vehicle)

    observed = backend.advance(0.0)

    assert observed[0] == pytest.approx(target_local[0], abs=1e-2)
    assert observed[1] == pytest.approx(target_local[1], abs=1e-2)
    assert backend.position == observed


def test_mavlink_backend_command_toward_requires_a_connected_vehicle() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    backend = MavlinkBackend(vehicle)
    with pytest.raises(RuntimeError, match="not connected"):
        backend.command_toward((1.0, 1.0))


# --- Shared contract: both backends must behave the same way ------------


@pytest.fixture(params=["point_mass", "mavlink"])
def backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, frame: LocalFrame
):
    if request.param == "point_mass":
        return PointMassBackend((0.0, 0.0), speed=5.0)
    message = _FakeMessage(
        ORIGIN_LATITUDE, ORIGIN_LONGITUDE, altitude_m=0.0, time_s=0.0
    )
    vehicle, _ = _connected_vehicle(monkeypatch, frame, messages=[message, message])
    return MavlinkBackend(vehicle)


def test_every_backend_satisfies_the_protocol(backend: VehicleBackend) -> None:
    assert isinstance(backend, VehicleBackend)


def test_every_backend_reports_a_two_element_position(backend: VehicleBackend) -> None:
    position = backend.position
    assert len(position) == 2


def test_every_backend_leaves_position_untouched_by_command_alone(
    backend: VehicleBackend,
) -> None:
    before = backend.position
    backend.command_toward((999.0, 999.0))
    assert backend.position == before
