"""Receiver-local, stationary inspection objectives from georeferenced tracks.

The offline world catalog reserves IDs for the fixed task-claim protocol. It is
not agent knowledge. Each receiver grants eligibility only from its own fresh
observations. Track identity is scoped to a producer, never cross-UAV identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .observation_replay import ReceiverObservations, TrackObservation
from .observation_transport import ObservationPublication
from .task_utility import TaskObjective
from .validation import (
    validate_nonnegative_real,
    validate_positive_integer,
    validate_timestamp,
)


@dataclass(frozen=True)
class TaskDescription:
    """Immutable accepted inspection work, not a current sensor observation."""

    objective: TaskObjective
    source_uav_id: str
    track_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_uav_id, str) or not self.source_uav_id.strip():
            raise ValueError("description source_uav_id must be nonempty")
        if (
            isinstance(self.track_id, bool)
            or not isinstance(self.track_id, int)
            or self.track_id < 0
        ):
            raise ValueError("description track_id must be a nonnegative integer")


@dataclass(frozen=True)
class DiscoveredTask:
    objective: TaskObjective
    source_uav_id: str
    track_id: int
    discovered_at: float
    learned_via: str = "observation"
    forwarder_agent_id: int | None = None

    @property
    def description(self) -> TaskDescription:
        return TaskDescription(self.objective, self.source_uav_id, self.track_id)


def build_discovery_catalog(
    publications: Sequence[ObservationPublication],
    *,
    first_task_id: int,
    width: float,
    height: float,
) -> dict[tuple[str, int], TaskObjective]:
    """Reserve stable IDs; reject moving targets rather than invent tracking."""
    positions: dict[tuple[str, int], tuple[float, float]] = {}
    for publication in publications:
        record = publication.record
        if (
            not isinstance(record, TrackObservation)
            or record.mission_position_m is None
        ):
            continue
        position = record.mission_position_m
        if not (0 <= position[0] <= width and 0 <= position[1] <= height):
            raise ValueError("observation mission position is outside the mission area")
        key = (record.uav_id, record.track_id)
        if key in positions and positions[key] != position:
            raise ValueError("inspection track coordinates must be stationary")
        positions[key] = position
    return {
        key: TaskObjective(first_task_id + index, position)
        for index, (key, position) in enumerate(sorted(positions.items()))
    }


class ReceiverTaskDiscovery:
    """Discover once; source loss does not retract an already accepted objective."""

    def __init__(
        self,
        task_ids: Mapping[tuple[str, int], int],
        *,
        minimum_confidence: float = 0.6,
    ) -> None:
        if (
            isinstance(minimum_confidence, bool)
            or not isinstance(minimum_confidence, (int, float))
            or not 0 <= minimum_confidence <= 1
        ):
            raise ValueError("minimum_confidence must be in [0, 1]")
        self._task_ids_by_track = dict(task_ids)
        self.minimum_confidence = minimum_confidence
        self._tasks: dict[int, DiscoveredTask] = {}
        self._last_timestamp = 0.0

    def receive_description(
        self,
        description: TaskDescription,
        *,
        received_at: float,
        forwarder_agent_id: int,
    ) -> bool:
        """Learn coordinates from a delivered description, without sensor freshness."""
        validate_positive_integer(forwarder_agent_id, name="description forwarder")
        received_at = validate_timestamp(
            received_at, previous=self._last_timestamp, name="description receipt time"
        )
        self._last_timestamp = received_at
        key = (description.source_uav_id, description.track_id)
        task_id = description.objective.task_id
        if self._task_ids_by_track.get(key) != task_id:
            raise ValueError("description identity does not match reserved task ID")
        existing = self._tasks.get(task_id)
        if existing is not None:
            if existing.description != description:
                raise ValueError("conflicting stationary task description")
            return False
        self._tasks[task_id] = DiscoveredTask(
            description.objective,
            *key,
            received_at,
            "task_description",
            forwarder_agent_id,
        )
        return True

    def discover(
        self, observations: ReceiverObservations, *, now: float
    ) -> tuple[DiscoveredTask, ...]:
        now = validate_nonnegative_real(now, name="discovery time")
        validate_timestamp(now, previous=self._last_timestamp, name="discovery time")
        self._last_timestamp = now
        added = []
        for record in observations.active_tracks(now=now):
            if (
                record.track_status != "observed"
                or record.confidence < self.minimum_confidence
                or record.mission_position_m is None
            ):
                continue
            key = (record.uav_id, record.track_id)
            task_id = self._task_ids_by_track.get(key)
            if task_id is None or task_id in self._tasks:
                continue
            objective = TaskObjective(task_id, record.mission_position_m)
            task = DiscoveredTask(objective, *key, now)
            self._tasks[objective.task_id] = task
            added.append(task)
        return tuple(added)

    @property
    def task_ids(self) -> frozenset[int]:
        return frozenset(self._tasks)

    @property
    def tasks(self) -> tuple[DiscoveredTask, ...]:
        return tuple(self._tasks[key] for key in sorted(self._tasks))
