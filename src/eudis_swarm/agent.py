"""Represent authoritative UAV state and simple two-dimensional movement.

This module is world truth: an :class:`Agent` is where an aircraft *actually*
is and whether it is *actually* flying. It is the boundary every other module is
defined against -- nothing here may be read by decision logic. A UAV reasons
only from :class:`Heartbeat` copies the modeled network delivers into its
receiver-local stores.

Two distinctions carry most of the project's meaning and are enforced here:

* ``responsive`` (physical: is the vehicle moving and transmitting?) is separate
  from ``status`` (belief: what the swarm has concluded). ``declare_failed()``
  sets the belief and never touches the physics, so a wrongly declared UAV
  keeps flying and keeps its task -- see :attr:`Agent.wrongly_declared`.
* ``current_task`` is an execution-intent cache, not ownership authority.
  Authority lives in each UAV's :class:`~eudis_swarm.task_claims.TaskClaimStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import hypot, isfinite
from numbers import Real
from typing import TypeAlias

from .validation import validate_positive_integer, validate_timestamp

Position: TypeAlias = tuple[float, float]


class AgentStatus(str, Enum):
    """Coordinator-visible UAV states."""

    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """A timestamped state snapshot advertised by one UAV.

    This is the only thing a UAV broadcasts about itself, and the only first-hand
    evidence its peers ever get. Immutable, so a delivered copy cannot drift from
    what was sent.
    """

    #: Which UAV this describes.
    agent_id: int
    #: Its position at emission, in world coordinates.
    position: Position
    #: Its coordinator-visible state at emission.
    status: AgentStatus
    #: The task it was executing, or ``None``.
    current_task: int | None
    #: Source-clock emission time. Receivers judge freshness by their own
    #: receipt time instead, because clocks may skew.
    timestamp: float

    def __post_init__(self) -> None:
        validate_positive_integer(self.agent_id, name="agent_id")
        if len(self.position) != 2 or not all(
            isinstance(value, Real) and not isinstance(value, bool) and isfinite(value)
            for value in self.position
        ):
            raise ValueError("position must contain two finite coordinates")
        validate_timestamp(self.timestamp)


def advance_point_mass(
    origin: Position, target: Position, speed: float, elapsed: float
) -> Position:
    """Return where a constant-speed point mass reaches, never overshooting.

    This is the single definition of how a vehicle moves. It is used both to
    move an aircraft and to predict where one would be, which today are the
    same arithmetic. Under a real autopilot they stop being the same thing --
    a commanded vehicle does not travel the straight line a planner assumes --
    so the two callers are deliberately kept distinguishable.
    """

    distance = hypot(target[0] - origin[0], target[1] - origin[1])
    if distance == 0.0:
        return origin
    travel = min(speed * elapsed, distance)
    ratio = travel / distance
    return (
        origin[0] + (target[0] - origin[0]) * ratio,
        origin[1] + (target[1] - origin[1]) * ratio,
    )


@dataclass(slots=True)
class Agent:
    """A point-mass UAV whose ``current_task`` is an intent, not authority."""

    #: Stable identity, 1-based.
    agent_id: int
    #: Current world position; mutated by :meth:`move_toward`.
    position: Position
    #: Constant ground speed, metres per simulated second.
    speed: float
    #: Coordinator-visible state (belief). Set to FAILED by
    #: :meth:`declare_failed` without any physical effect.
    status: AgentStatus = AgentStatus.IDLE
    #: Execution-intent cache -- what this UAV is heading for. Not ownership.
    current_task: int | None = None
    #: Source-clock time of the last heartbeat this UAV emitted.
    last_heartbeat: float = 0.0
    #: Physical truth: ``False`` means the vehicle has stopped and gone silent.
    #: Only :meth:`inject_failure` clears it; a declaration never does.
    responsive: bool = True
    #: When ``inject_failure`` fired, for observer accounting.
    failure_injected_at: float | None = None

    def __post_init__(self) -> None:
        validate_positive_integer(self.agent_id, name="agent_id")
        if len(self.position) != 2 or not all(
            isinstance(value, Real) and not isinstance(value, bool) and isfinite(value)
            for value in self.position
        ):
            raise ValueError("position must contain two finite coordinates")
        if (
            not isinstance(self.speed, Real)
            or isinstance(self.speed, bool)
            or not isfinite(self.speed)
            or self.speed <= 0.0
        ):
            raise ValueError("speed must be finite and greater than zero")

    @property
    def available(self) -> bool:
        """Whether the coordinator currently considers this UAV allocatable."""

        return self.status is AgentStatus.IDLE and self.current_task is None

    def distance_to(self, target: Position) -> float:
        return hypot(target[0] - self.position[0], target[1] - self.position[1])

    def assign_task(self, task_id: int) -> None:
        validate_positive_integer(task_id, name="task_id")
        if not self.available:
            raise ValueError(f"UAV {self.agent_id} is not available")
        self.current_task = task_id
        self.status = AgentStatus.ACTIVE

    def release_task(self, task_id: int) -> None:
        validate_positive_integer(task_id, name="task_id")
        if self.current_task != task_id:
            raise ValueError(f"UAV {self.agent_id} does not own Task {task_id}")
        self.current_task = None
        if self.status is not AgentStatus.FAILED:
            self.status = AgentStatus.IDLE

    def complete_task(self, task_id: int) -> None:
        self.release_task(task_id)

    def move_toward(self, target: Position, elapsed: float) -> None:
        """Compatibility entry point for point-mass movement outside Simulation."""

        from .vehicle_backend import PointMassBackend

        PointMassBackend().move_toward(self, target, elapsed)

    def send_heartbeat(self, timestamp: float) -> Heartbeat | None:
        """Return a state snapshot, or nothing after a physical failure."""

        timestamp = validate_timestamp(timestamp, previous=self.last_heartbeat)
        # being declared failed does not silence a radio that still works.
        if not self.responsive:
            return None
        self.last_heartbeat = timestamp
        return Heartbeat(
            agent_id=self.agent_id,
            position=self.position,
            status=self.status,
            current_task=self.current_task,
            timestamp=timestamp,
        )

    def inject_failure(self, timestamp: float) -> bool:
        """Make the UAV silent and immobile without declaring it failed."""

        timestamp = validate_timestamp(timestamp, previous=self.last_heartbeat)
        if not self.responsive:
            return False
        self.responsive = False
        self.failure_injected_at = timestamp
        return True

    def declare_failed(self) -> None:
        """Record the swarm's belief that this UAV has failed.

        A declaration is evidence, not a kill switch.  It never touches
        ``responsive``: if the quorum was wrong the vehicle keeps flying,
        keeps transmitting, and keeps the task it does not know it lost.
        """

        self.status = AgentStatus.FAILED

    @property
    def wrongly_declared(self) -> bool:
        """Whether the swarm believes this UAV is dead while it is still flying."""

        return self.status is AgentStatus.FAILED and self.responsive
