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


@dataclass(frozen=True)
class DiscoveredTask:
    objective: TaskObjective
    source_uav_id: str
    track_id: int
    discovered_at: float


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

    def discover(
        self, observations: ReceiverObservations, *, now: float
    ) -> tuple[DiscoveredTask, ...]:
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
