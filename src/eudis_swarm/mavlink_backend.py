"""A VehicleBackend that drives one physical position from live SITL telemetry.

Kept separate from vehicle_backend.py so eudis_swarm.simulation and
eudis_swarm.sweep never import this module, the same import boundary
tests/test_sitl.py already enforces for sitl.py itself: a missing ``sitl``
extra must disable one backend, never the package. This module does not
import sitl.py or pymavlink at runtime either -- it only needs the small
slice of SitlVehicle described by VehicleTelemetry below, so it is fully
testable with a plain fake and carries no optional dependency of its own.
"""

from __future__ import annotations

import time
from typing import Protocol

from .agent import Agent, Position
from .validation import validate_timestamp
from .vehicle_backend import PointMassBackend


class VehicleTelemetry(Protocol):
    """The slice of ``SitlVehicle`` this backend needs, spelled out structurally.

    Naming the protocol here rather than importing ``SitlVehicle`` keeps this
    module decoupled from the ``sitl`` extra: a test fake, or a future backend
    for a different autopilot, only has to satisfy this shape.
    """

    @property
    def position(self) -> Position: ...

    @property
    def vehicle_time_s(self) -> float: ...

    def command_toward(self, target: Position) -> None: ...

    def observe(self, duration: float = 0.0) -> Position: ...


class MavlinkBackend:
    """Integrate one (or a few) live vehicles into an otherwise normal mission.

    Every other agent -- any ``agent_id`` not named in ``vehicles`` -- still
    moves through :class:`~.vehicle_backend.PointMassBackend`, unchanged. This
    is deliberately "one vehicle integrated", per docs/sitl.md's own phased
    plan, not a fully SITL-driven swarm.

    Lock-step per that document: command, then block until the vehicle's own
    clock -- never wall-clock, since SITL is routinely run under
    ``--speedup`` -- shows the step's worth of elapsed time, or raise after
    ``step_timeout`` wall-clock seconds so a stalled vehicle fails loudly
    rather than a stale position ever being presented as a fresh observation.
    """

    #: Read by any caller that produces published numbers. Always False.
    deterministic = False

    def __init__(
        self,
        vehicles: dict[int, VehicleTelemetry],
        *,
        step_timeout: float = 30.0,
    ) -> None:
        if not vehicles:
            raise ValueError("vehicles must name at least one agent")
        if step_timeout <= 0.0:
            raise ValueError("step_timeout must be positive")
        self._vehicles = vehicles
        self._step_timeout = step_timeout
        self._point_mass = PointMassBackend()

    def move_toward(self, agent: Agent, target: Position, elapsed: float) -> None:
        elapsed = validate_timestamp(elapsed, previous=0.0, name="elapsed")
        vehicle = self._vehicles.get(agent.agent_id)
        if vehicle is None:
            self._point_mass.move_toward(agent, target, elapsed)
            return
        # Physical responsiveness is independent of a failure declaration --
        # the same rule PointMassBackend enforces, so a UAV "declared dead" by
        # a quorum that is actually still flying keeps flying here too.
        if not agent.responsive:
            return
        vehicle.command_toward(target)
        target_vehicle_time = vehicle.vehicle_time_s + elapsed
        deadline = time.monotonic() + self._step_timeout
        while vehicle.vehicle_time_s < target_vehicle_time:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"agent {agent.agent_id}: telemetry stalled after "
                    f"{self._step_timeout:.1f}s waiting for {elapsed:.2f}s of "
                    "vehicle time -- refusing to report a stale position as fresh"
                )
            vehicle.observe(duration=0.05)
        agent.position = vehicle.position
