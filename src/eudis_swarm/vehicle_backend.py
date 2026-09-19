"""A common command-then-observe seam for driving a vehicle.

This exists so a future caller could run a mission against either the
deterministic point-mass model or a real autopilot without knowing which one
it is talking to. Two backends conform to :class:`VehicleBackend` today:

* :class:`PointMassBackend` -- the existing constant-speed, straight-line
  model (:func:`eudis_swarm.agent.advance_point_mass`), wrapped so it can be
  driven the same way a real vehicle is: command a target, then ask what
  actually happened.
* :class:`MavlinkBackend` -- a thin adapter over the existing standalone
  :class:`eudis_swarm.sitl.SitlVehicle` proof of concept.

**Neither backend is wired into :class:`eudis_swarm.simulation.Simulation` by
this module.** The mission still moves agents exactly as it always has, via
``Agent.move_toward``/``advance_point_mass`` directly -- ``agent.py`` and
``simulation.py`` are untouched. This module only proves the seam exists and
that both backends satisfy the same contract (see
``tests/test_vehicle_backend.py``), per the instruction to *begin* the seam
additively, without touching the working movement path or any
coordination-core file. Wiring it into the mission loop is deliberately left
as future work.

Command and observe are two separate calls, on purpose: that is the one
shape a straight-line arithmetic model and a real autopilot can honestly
share. A real vehicle does not teleport to a setpoint the instant it is
commanded (see ``advance_point_mass``'s own docstring), so "what did I just
tell it to do" and "how far did it get" cannot be one operation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .agent import Position, advance_point_mass
from .sitl import SitlVehicle


@runtime_checkable
class VehicleBackend(Protocol):
    """Command-then-observe seam every vehicle backend must satisfy."""

    @property
    def position(self) -> Position:
        """The last known position, in local east/north metres."""
        ...

    def command_toward(self, target: Position) -> None:
        """Set the vehicle's current target. Does not itself move anything."""
        ...

    def advance(self, elapsed: float) -> Position:
        """Let ``elapsed`` seconds pass and return where the vehicle ended up."""
        ...


class PointMassBackend:
    """The existing constant-speed, straight-line model, behind the seam.

    Produces bit-identical results to ``Agent.move_toward`` for the same
    command sequence, because both call the same
    ``eudis_swarm.agent.advance_point_mass`` function -- this class adds a
    command/observe shape around it, not a new motion model.
    """

    def __init__(self, position: Position, speed: float) -> None:
        self._position = position
        self._speed = speed
        self._target = position

    @property
    def position(self) -> Position:
        return self._position

    def command_toward(self, target: Position) -> None:
        self._target = target

    def advance(self, elapsed: float) -> Position:
        self._position = advance_point_mass(
            self._position, self._target, self._speed, elapsed
        )
        return self._position


class MavlinkBackend:
    """Adapt the existing standalone :class:`~eudis_swarm.sitl.SitlVehicle`.

    A thin adapter, deliberately: ``SitlVehicle`` already exposes exactly the
    three operations this seam needs (``position``, ``command_toward``,
    ``observe``), so this class adds no new behaviour, only the name match.
    Constructing one still requires an already-connected ``SitlVehicle`` --
    this module does not manage that connection's lifecycle, and never runs
    or requires a live autopilot itself (see
    ``tests/test_vehicle_backend.py``, which uses a mocked connection).
    Never used for a measured result: see ``SitlVehicle.deterministic``.
    """

    def __init__(self, vehicle: SitlVehicle) -> None:
        self._vehicle = vehicle

    @property
    def position(self) -> Position:
        return self._vehicle.position

    def command_toward(self, target: Position) -> None:
        self._vehicle.command_toward(target)

    def advance(self, elapsed: float) -> Position:
        return self._vehicle.observe(elapsed)
