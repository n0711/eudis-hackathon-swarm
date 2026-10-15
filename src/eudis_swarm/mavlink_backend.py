"""A VehicleBackend that drives physical positions from live SITL telemetry.

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
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from .agent import Agent, Position
from .validation import validate_timestamp
from .vehicle_backend import PointMassBackend

#: ArduCopter flight modes that stop horizontal motion and hold position.
#: BRAKE decelerates as hard as the vehicle allows and then holds; LOITER
#: decelerates more gently and also holds. Both keep the aircraft in the air,
#: which is the safe thing for a simulated "engine still running, controller
#: gone" failure -- a motor cut or a disarm would drop it.
SAFE_HOLD_MODES = ("BRAKE", "LOITER")

#: Wall-clock seconds between telemetry pumps while waiting on a vehicle clock.
_POLL_SECONDS = 0.05


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

    def hold(self, mode: str = "BRAKE") -> bool: ...


@dataclass(frozen=True, slots=True)
class LockStepSample:
    """One integrated vehicle at one lock-step boundary, as it was observed.

    ``drift_s`` is ``vehicle_time_s - target_vehicle_time_s``: how far the
    vehicle's own clock ran *ahead* of the logical time it was stepped to.
    It is never negative, because a step does not report until the vehicle
    has caught up. A positive value is the price of stepping several
    free-running autopilots one after another: while one is waited on, the
    others keep flying.
    """

    agent_id: int
    #: Logical seconds since this vehicle's anchor (the first integrated step).
    logical_elapsed_s: float
    target_vehicle_time_s: float
    vehicle_time_s: float
    drift_s: float
    #: Wall-clock seconds spent waiting in this step. Diagnostic only.
    waited_wall_s: float
    position: Position
    #: ``True`` when a setpoint was sent this step, ``False`` for a hover step.
    commanded: bool


@dataclass(frozen=True, slots=True)
class FailStopEvent:
    """The one safe-hold command sent to a physically failed vehicle."""

    agent_id: int
    logical_time: float
    mode: str
    #: Whether the autopilot reported the requested mode afterwards.
    confirmed: bool
    #: Vehicle clock and observed position immediately before the command.
    vehicle_time_s: float
    position: Position


@dataclass(slots=True)
class _Anchor:
    """Per-vehicle mapping from logical time to that vehicle's own clock."""

    vehicle_time_s: float
    logical_elapsed_s: float = 0.0
    last_reported_vehicle_time_s: float | None = None


class MavlinkBackend:
    """Integrate one or more live vehicles into an otherwise normal mission.

    Every other agent -- any ``agent_id`` not named in ``vehicles`` -- still
    moves through :class:`~.vehicle_backend.PointMassBackend`, unchanged.

    **Anchored lock-step.** Every vehicle is anchored together, at the first
    integrated step: each vehicle's clock reading at that moment is paired
    with logical elapsed zero.
    Every later step targets ``anchor + cumulative logical elapsed`` on that
    vehicle's *own* clock -- never wall-clock, since SITL is routinely run
    under ``--speedup``. A step waits only while the vehicle is behind its
    target, always pumps fresh telemetry before reporting, and refuses to
    report a sample that is not strictly newer than the previous one it
    reported. With several independent autopilots stepped one after another
    each clock keeps running while the others are waited on; anchoring keeps
    that from accumulating, and the residual is recorded per step as
    :attr:`LockStepSample.drift_s` so it can be measured rather than assumed.
    A step that cannot be satisfied within ``step_timeout`` wall-clock seconds
    raises ``TimeoutError``.

    **Fail-stop.** :meth:`apply_physical_faults` sends a vehicle whose agent has
    become physically non-responsive (``Agent.responsive`` is ``False``)
    exactly one safe-hold mode change, so its motion actually stops rather
    than continuing toward the last setpoint. A failure *declaration* never
    triggers it: belief is not a kill switch.

    ``move_toward`` alone keeps the single-vehicle behaviour working with a
    plain :class:`~.simulation.Simulation`. :meth:`synchronize` and
    :meth:`apply_physical_faults` are the extra per-boundary hooks a
    multi-vehicle run needs; ``eudis_swarm.live_mission`` calls them.
    """

    #: Read by any caller that produces published numbers. Always False.
    deterministic = False

    def __init__(
        self,
        vehicles: dict[int, VehicleTelemetry],
        *,
        step_timeout: float = 30.0,
        fail_stop_mode: str | None = "BRAKE",
    ) -> None:
        if not vehicles:
            raise ValueError("vehicles must name at least one agent")
        if step_timeout <= 0.0:
            raise ValueError("step_timeout must be positive")
        if fail_stop_mode is not None and fail_stop_mode not in SAFE_HOLD_MODES:
            raise ValueError(
                f"fail_stop_mode must be one of {SAFE_HOLD_MODES} or None, "
                f"not {fail_stop_mode!r}"
            )
        self._vehicles = vehicles
        self._step_timeout = step_timeout
        self._fail_stop_mode = fail_stop_mode
        self._point_mass = PointMassBackend()
        self._anchors: dict[int, _Anchor] = {}
        self._stepped_since_sync: set[int] = set()
        self._last_sync_time = 0.0
        self._held: set[int] = set()
        self._samples: list[LockStepSample] = []
        self._fail_stops: list[FailStopEvent] = []

    @property
    def agent_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._vehicles))

    @property
    def fail_stop_mode(self) -> str | None:
        return self._fail_stop_mode

    @property
    def fail_stops(self) -> tuple[FailStopEvent, ...]:
        return tuple(self._fail_stops)

    @property
    def samples(self) -> tuple[LockStepSample, ...]:
        return tuple(self._samples)

    def drain_samples(self) -> tuple[LockStepSample, ...]:
        """Return the samples recorded since the previous drain, then forget them."""

        drained = tuple(self._samples)
        self._samples.clear()
        return drained

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
        self._step(agent, vehicle, elapsed, commanded=True)

    def synchronize(self, agents: Iterable[Agent], timestamp: float) -> None:
        """Bring every integrated vehicle to logical ``timestamp``.

        Call once per mission boundary, after the simulator's own movement
        pass. Vehicles already stepped by ``move_toward`` at this boundary are
        left alone; a responsive vehicle with no executable task is stepped as
        a hover so its clock stays anchored and its reported position stays
        observed rather than remembered.
        """

        timestamp = validate_timestamp(
            timestamp, previous=self._last_sync_time, name="synchronize timestamp"
        )
        elapsed = timestamp - self._last_sync_time
        for agent in sorted(agents, key=lambda item: item.agent_id):
            vehicle = self._vehicles.get(agent.agent_id)
            if vehicle is None or agent.agent_id in self._stepped_since_sync:
                continue
            if not agent.responsive:
                continue
            self._step(agent, vehicle, elapsed, commanded=False)
        self._stepped_since_sync.clear()
        self._last_sync_time = timestamp

    def apply_physical_faults(self, agents: Iterable[Agent], timestamp: float) -> None:
        """Send each newly non-responsive integrated vehicle one safe hold.

        Only ``Agent.responsive`` -- physical truth -- is read. ``Agent.status``
        (the swarm's belief) is deliberately ignored, so a wrongly declared UAV
        is never stopped by this.
        """

        if self._fail_stop_mode is None:
            return
        for agent in sorted(agents, key=lambda item: item.agent_id):
            vehicle = self._vehicles.get(agent.agent_id)
            if vehicle is None or agent.responsive or agent.agent_id in self._held:
                continue
            # Marked before the command so a raising link can never cause a
            # second, duplicate hold on the next boundary.
            self._held.add(agent.agent_id)
            vehicle.observe(0.0)
            commanded_time = vehicle.vehicle_time_s
            commanded_position = vehicle.position
            confirmed = vehicle.hold(self._fail_stop_mode)
            self._fail_stops.append(
                FailStopEvent(
                    agent_id=agent.agent_id,
                    logical_time=timestamp,
                    mode=self._fail_stop_mode,
                    confirmed=confirmed,
                    vehicle_time_s=commanded_time,
                    position=commanded_position,
                )
            )

    def observe_held(self, agent_id: int) -> tuple[float, Position] | None:
        """Pump and return ``(vehicle_time_s, position)`` of a held vehicle.

        For the evidence log only: it shows whether the hold actually stopped
        the aircraft. It never writes ``Agent.position`` -- a physically failed
        agent's truth stays where the simulator stopped it.
        """

        if agent_id not in self._held:
            return None
        vehicle = self._vehicles[agent_id]
        vehicle.observe(0.0)
        return vehicle.vehicle_time_s, vehicle.position

    def _anchor_all(self) -> None:
        """Pin every vehicle's clock to logical zero in one quick pass.

        Anchoring each vehicle only when it is first stepped would let the
        first vehicle's clock run on while the later ones are waited on, and
        that head start would persist as a constant drift for the whole run.
        The first pass drains whatever telemetry backlog built up during
        takeoff (the slow part); the second reads every clock back to back.
        """

        for agent_id in sorted(self._vehicles):
            self._vehicles[agent_id].observe(0.0)
        for agent_id in sorted(self._vehicles):
            vehicle = self._vehicles[agent_id]
            vehicle.observe(0.0)
            self._anchors[agent_id] = _Anchor(vehicle_time_s=vehicle.vehicle_time_s)

    def _step(
        self,
        agent: Agent,
        vehicle: VehicleTelemetry,
        elapsed: float,
        *,
        commanded: bool,
    ) -> None:
        started = time.monotonic()
        deadline = started + self._step_timeout
        if not self._anchors:
            self._anchor_all()
        anchor = self._anchors.get(agent.agent_id)
        if anchor is None:  # pragma: no cover - _anchor_all covers every vehicle
            raise RuntimeError(f"agent {agent.agent_id} has no lock-step anchor")
        anchor.logical_elapsed_s += elapsed
        target_vehicle_time = anchor.vehicle_time_s + anchor.logical_elapsed_s
        previous = anchor.last_reported_vehicle_time_s

        # Always pump once before reporting, even when the vehicle is already
        # ahead: the position handed back must be the newest one available.
        vehicle.observe(0.0)
        while vehicle.vehicle_time_s < target_vehicle_time or (
            previous is not None and vehicle.vehicle_time_s <= previous
        ):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"agent {agent.agent_id}: telemetry stalled after "
                    f"{self._step_timeout:.1f}s waiting for vehicle time "
                    f"{target_vehicle_time:.2f}s (last seen "
                    f"{vehicle.vehicle_time_s:.2f}s) -- refusing to report a "
                    "stale position as fresh"
                )
            vehicle.observe(duration=_POLL_SECONDS)

        observed_time = vehicle.vehicle_time_s
        anchor.last_reported_vehicle_time_s = observed_time
        agent.position = vehicle.position
        self._stepped_since_sync.add(agent.agent_id)
        self._samples.append(
            LockStepSample(
                agent_id=agent.agent_id,
                logical_elapsed_s=anchor.logical_elapsed_s,
                target_vehicle_time_s=target_vehicle_time,
                vehicle_time_s=observed_time,
                drift_s=observed_time - target_vehicle_time,
                waited_wall_s=time.monotonic() - started,
                position=agent.position,
                commanded=commanded,
            )
        )
