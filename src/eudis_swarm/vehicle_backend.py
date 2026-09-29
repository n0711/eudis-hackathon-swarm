"""Physical movement boundary for simulated vehicles.

The backend advances world state. Communication-only position projections remain
predictions and do not call this interface or mutate vehicle state.
"""

from __future__ import annotations

from typing import Protocol

from .agent import Agent, Position, advance_point_mass
from .validation import validate_timestamp


class VehicleBackend(Protocol):
    """Advance an executing vehicle toward a target over simulated time."""

    def move_toward(self, agent: Agent, target: Position, elapsed: float) -> None:
        """Update the vehicle's physical position in place."""
        ...


class PointMassBackend:
    """Preserve the existing constant-speed, no-overshoot movement model."""

    def move_toward(self, agent: Agent, target: Position, elapsed: float) -> None:
        elapsed = validate_timestamp(elapsed, previous=0.0, name="elapsed")
        # Physical responsiveness is independent of a failure declaration.
        if not agent.responsive:
            return
        agent.position = advance_point_mass(
            agent.position, target, agent.speed, elapsed
        )
