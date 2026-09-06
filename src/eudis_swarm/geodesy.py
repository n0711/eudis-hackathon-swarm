"""Convert between the simulator's local metres and WGS84 latitude/longitude.

The simulator reasons in a flat local frame: positions are metres east and
north of an origin. An autopilot reasons in latitude and longitude. Any
software-in-the-loop backend has to cross that boundary on every command and
every telemetry sample, so the conversion lives here, on its own, where it can
be tested without a vehicle attached.

This is an equirectangular projection about a fixed origin. It is deliberately
not a general-purpose geodesy library: it is accurate for the small areas this
simulator uses and degrades with distance from the origin, which
``local_frame_error_metres`` makes explicit rather than leaving implied.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan2, cos, degrees, isfinite, radians

from .validation import validate_positive_real

#: WGS84 semi-major axis. The projection is a sphere of this radius, which is
#: the usual small-area simplification and the reason accuracy is bounded.
EARTH_RADIUS_M = 6_378_137.0

Position = tuple[float, float]
Global = tuple[float, float]


def _validate_latitude(value: float, *, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be a real number")
    if not isfinite(value) or not -90.0 <= value <= 90.0:
        raise ValueError(f"{name} must be a finite latitude in [-90, 90]")
    return float(value)


def _validate_longitude(value: float, *, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be a real number")
    if not isfinite(value) or not -180.0 <= value <= 180.0:
        raise ValueError(f"{name} must be a finite longitude in [-180, 180]")
    return float(value)


@dataclass(frozen=True, slots=True)
class LocalFrame:
    """A flat east/north frame tangent to the globe at one origin.

    The origin is the simulator's ``(0.0, 0.0)``. East is +x and north is +y,
    matching the way positions are already written everywhere else.
    """

    origin_latitude: float
    origin_longitude: float

    def __post_init__(self) -> None:
        _validate_latitude(self.origin_latitude, name="origin_latitude")
        _validate_longitude(self.origin_longitude, name="origin_longitude")
        # A frame tangent at a pole has no usable east axis: the scaling
        # factor collapses and longitude stops meaning a distance.
        if abs(self.origin_latitude) > 89.0:
            raise ValueError("origin_latitude must be within 89 degrees of the equator")

    @property
    def _metres_per_degree_longitude(self) -> float:
        return radians(1.0) * EARTH_RADIUS_M * cos(radians(self.origin_latitude))

    @property
    def _metres_per_degree_latitude(self) -> float:
        return radians(1.0) * EARTH_RADIUS_M

    def to_global(self, position: Position) -> Global:
        """Return the latitude/longitude of a local east/north position."""

        east, north = position
        if not (isfinite(east) and isfinite(north)):
            raise ValueError("position must contain two finite coordinates")
        latitude = self.origin_latitude + north / self._metres_per_degree_latitude
        longitude = self.origin_longitude + east / self._metres_per_degree_longitude
        return (latitude, longitude)

    def to_local(self, location: Global) -> Position:
        """Return the local east/north position of a latitude/longitude."""

        latitude = _validate_latitude(location[0], name="latitude")
        longitude = _validate_longitude(location[1], name="longitude")
        north = (latitude - self.origin_latitude) * self._metres_per_degree_latitude
        east = (longitude - self.origin_longitude) * self._metres_per_degree_longitude
        return (east, north)


def local_frame_error_metres(extent_metres: float) -> float:
    """Bound the projection error at a given distance from the origin.

    A flat frame under-measures a curved surface. The leading term of that
    error grows with the cube of the distance, which is why a projection that
    is irrelevant over a football pitch is unusable over a country. Returned so
    that a caller can assert the frame is fit for the area in use rather than
    assuming it.
    """

    extent_metres = validate_positive_real(extent_metres, name="extent_metres")
    return extent_metres**3 / (6.0 * EARTH_RADIUS_M**2)


def bearing_degrees(origin: Position, target: Position) -> float:
    """Return the compass bearing from one local position to another.

    Zero is north and angles increase clockwise, which is the autopilot
    convention rather than the mathematical one.
    """

    east = target[0] - origin[0]
    north = target[1] - origin[1]
    return degrees(atan2(east, north)) % 360.0
