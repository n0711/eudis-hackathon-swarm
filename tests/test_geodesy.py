"""The local frame that a software-in-the-loop backend commands through.

The simulator thinks in metres and an autopilot thinks in degrees, so every
command and every telemetry sample crosses this conversion. It is pure, so it
is tested without a vehicle anywhere near it.
"""

from __future__ import annotations

import pytest

from eudis_swarm.geodesy import (
    EARTH_RADIUS_M,
    LocalFrame,
    bearing_degrees,
    local_frame_error_metres,
)

#: Cyprus, where the aircraft this models would actually fly.
NICOSIA = (35.1264, 33.4299)


def test_the_origin_is_the_simulators_zero() -> None:
    frame = LocalFrame(*NICOSIA)
    assert frame.to_local(NICOSIA) == (0.0, 0.0)
    assert frame.to_global((0.0, 0.0)) == pytest.approx(NICOSIA)


@pytest.mark.parametrize(
    "position", [(0.0, 0.0), (100.0, 100.0), (-50.0, 25.0), (999.0, -1234.5)]
)
def test_a_round_trip_returns_the_same_position(position) -> None:
    frame = LocalFrame(*NICOSIA)
    assert frame.to_local(frame.to_global(position)) == pytest.approx(
        position, abs=1e-6
    )


def test_east_and_north_are_x_and_y() -> None:
    """Axis order is a silent-failure risk, so it is pinned explicitly."""

    frame = LocalFrame(*NICOSIA)
    latitude, longitude = frame.to_global((0.0, 100.0))
    assert latitude > NICOSIA[0], "positive y must go north"
    assert longitude == pytest.approx(NICOSIA[1]), "y must not move longitude"

    latitude, longitude = frame.to_global((100.0, 0.0))
    assert longitude > NICOSIA[1], "positive x must go east"
    assert latitude == pytest.approx(NICOSIA[0]), "x must not move latitude"


def test_a_hundred_metres_north_is_a_hundred_metres() -> None:
    frame = LocalFrame(*NICOSIA)
    latitude, _ = frame.to_global((0.0, 100.0))
    metres_per_degree = EARTH_RADIUS_M * 3.141592653589793 / 180.0
    assert (latitude - NICOSIA[0]) * metres_per_degree == pytest.approx(100.0)


def test_longitude_scaling_follows_latitude() -> None:
    """A degree of longitude is shorter further from the equator."""

    equator = LocalFrame(0.0, 0.0)
    north = LocalFrame(60.0, 0.0)
    _, equator_longitude = equator.to_global((1000.0, 0.0))
    _, north_longitude = north.to_global((1000.0, 0.0))
    # cos(60 degrees) is a half, so the same distance spans twice the longitude.
    assert north_longitude == pytest.approx(equator_longitude * 2.0, rel=1e-6)


def test_the_projection_error_is_negligible_here_and_stated_anyway() -> None:
    assert local_frame_error_metres(100.0) < 1e-6
    assert local_frame_error_metres(1000.0) < 1e-3
    # Honest about where it stops being usable.
    assert local_frame_error_metres(100_000.0) > 1.0


@pytest.mark.parametrize(
    "target,expected",
    [((0.0, 1.0), 0.0), ((1.0, 0.0), 90.0), ((0.0, -1.0), 180.0), ((-1.0, 0.0), 270.0)],
)
def test_bearings_use_the_autopilot_convention(target, expected) -> None:
    """North is zero and angles increase clockwise, not the maths convention."""

    assert bearing_degrees((0.0, 0.0), target) == pytest.approx(expected)


@pytest.mark.parametrize("latitude", [91.0, -91.0, float("nan")])
def test_an_impossible_origin_latitude_is_refused(latitude) -> None:
    with pytest.raises(ValueError):
        LocalFrame(latitude, 0.0)


@pytest.mark.parametrize("latitude", [89.5, -89.5])
def test_a_polar_origin_is_refused(latitude) -> None:
    """At a pole the east axis collapses, so the frame is meaningless."""

    with pytest.raises(ValueError, match="89 degrees"):
        LocalFrame(latitude, 0.0)


def test_an_impossible_longitude_is_refused() -> None:
    with pytest.raises(ValueError):
        LocalFrame(0.0, 181.0)
    with pytest.raises(ValueError):
        LocalFrame(*NICOSIA).to_local((0.0, 181.0))


def test_a_boolean_is_not_a_coordinate() -> None:
    with pytest.raises(TypeError):
        LocalFrame(True, 0.0)  # type: ignore[arg-type]
