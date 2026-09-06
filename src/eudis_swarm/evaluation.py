"""Evaluator-side measurement of coordination quality.

Nothing in this module is available to a UAV. Every measurement here reads
*every* agent's private claim store at the same instant, which is precisely the
god's-eye view the swarm does not have and must never be given. These numbers
exist to judge the protocol from outside it, not to inform it.

The quantity that matters is not how often two UAVs finished the same task --
that is rare and mostly harmless. It is how much *flight time* was spent
converging on work somebody else already owned. That is an integral over
concurrent ownership, and its counterpart is the time a task sat abandoned
while still incomplete. Improving either one at the expense of the other is
not progress, so both are reported together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Collection, Mapping

from .task import TaskOwnershipState, TaskStatus
from .validation import validate_timestamp

if TYPE_CHECKING:
    from .metrics import SimulationMetrics
    from .task import Task
    from .task_claims import TaskClaimStore


@dataclass(frozen=True, slots=True)
class OwnershipSample:
    """Which tasks were in each pathological ownership condition at one instant."""

    concurrently_owned_task_ids: tuple[int, ...]
    abandoned_task_ids: tuple[int, ...]
    dropped_task_ids: tuple[int, ...]
    contested_task_ids: tuple[int, ...]

    @property
    def concurrent_ownership_count(self) -> int:
        return len(self.concurrently_owned_task_ids)

    @property
    def abandoned_count(self) -> int:
        return len(self.abandoned_task_ids)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped_task_ids)

    @property
    def contested_count(self) -> int:
        return len(self.contested_task_ids)


EMPTY_SAMPLE = OwnershipSample((), (), (), ())


def sample_ownership(
    claim_stores: Mapping[int, TaskClaimStore],
    tasks: Mapping[int, Task],
    timestamp: float,
    *,
    previously_owned_task_ids: Collection[int] = (),
) -> OwnershipSample:
    """Read every replica at one instant and classify each task.

    A task is *concurrently owned* when two or more UAVs each believe they own
    it. Neither of them can detect that on its own -- it exists only in the
    comparison, which is why this is an evaluator measurement.

    A task is *abandoned* when no UAV claims it and it is not finished. This is
    the cost of being cautious, and it is the reason concurrent ownership must
    never be reported alone. Note that it includes work nobody has started yet,
    so with a large backlog it is dominated by queue depth rather than by
    coordination quality -- ``dropped_task_ids`` is the discriminating subset,
    counting only tasks that were owned by somebody and then were not.
    """

    concurrently_owned: list[int] = []
    abandoned: list[int] = []
    dropped: list[int] = []
    contested: list[int] = []

    for task_id in sorted(tasks):
        self_owner_count = 0
        any_contested = False
        for store in claim_stores.values():
            view = store.view(task_id, timestamp)
            if view.state is TaskOwnershipState.OWNED_BY_SELF:
                self_owner_count += 1
            if view.contested:
                any_contested = True
        if self_owner_count >= 2:
            concurrently_owned.append(task_id)
        elif (
            self_owner_count == 0 and tasks[task_id].status is not TaskStatus.COMPLETED
        ):
            abandoned.append(task_id)
            if task_id in previously_owned_task_ids:
                dropped.append(task_id)
        if any_contested:
            contested.append(task_id)

    return OwnershipSample(
        concurrently_owned_task_ids=tuple(concurrently_owned),
        abandoned_task_ids=tuple(abandoned),
        dropped_task_ids=tuple(dropped),
        contested_task_ids=tuple(contested),
    )


def _self_owner_counts(
    claim_stores: Mapping[int, TaskClaimStore],
    tasks: Mapping[int, Task],
    timestamp: float,
) -> dict[int, int]:
    """Count how many replicas claim each task for themselves right now."""

    counts: dict[int, int] = {}
    for task_id in sorted(tasks):
        counts[task_id] = sum(
            store.view(task_id, timestamp).state is TaskOwnershipState.OWNED_BY_SELF
            for store in claim_stores.values()
        )
    return counts


class OwnershipEvaluator:
    """Integrate ownership pathologies over logical time.

    The simulation advances in irregular event-driven steps, so each sample is
    held constant until the next one -- a left Riemann sum over intervals whose
    boundaries are exactly the instants at which state can change.
    """

    def __init__(self, metrics: SimulationMetrics) -> None:
        self._metrics = metrics
        self._last_timestamp: float | None = None
        self._last_sample: OwnershipSample = EMPTY_SAMPLE
        self._ever_owned_task_ids: set[int] = set()

    @property
    def last_sample(self) -> OwnershipSample:
        return self._last_sample

    def observe(
        self,
        timestamp: float,
        claim_stores: Mapping[int, TaskClaimStore],
        tasks: Mapping[int, Task],
    ) -> OwnershipSample:
        """Close the interval since the previous sample, then take a new one."""

        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="ownership evaluation timestamp",
        )
        self._accumulate(timestamp)
        owner_counts = _self_owner_counts(claim_stores, tasks, timestamp)
        self._last_sample = sample_ownership(
            claim_stores,
            tasks,
            timestamp,
            previously_owned_task_ids=self._ever_owned_task_ids,
        )
        for task_id, owner_count in owner_counts.items():
            if owner_count:
                self._ever_owned_task_ids.add(task_id)
        self._last_timestamp = timestamp
        return self._last_sample

    def finish(self, timestamp: float) -> None:
        """Close the final interval at mission end."""

        timestamp = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="ownership evaluation timestamp",
        )
        self._accumulate(timestamp)
        self._last_timestamp = timestamp
        self._last_sample = EMPTY_SAMPLE

    def _accumulate(self, timestamp: float) -> None:
        if self._last_timestamp is None:
            return
        elapsed = timestamp - self._last_timestamp
        if elapsed <= 0.0:
            return
        metrics = self._metrics
        sample = self._last_sample
        metrics.concurrent_ownership_duration += sample.concurrent_ownership_count * (
            elapsed
        )
        metrics.abandoned_task_time += sample.abandoned_count * elapsed
        metrics.dropped_ownership_time += sample.dropped_count * elapsed
        metrics.contested_duration += sample.contested_count * elapsed
        for task_id in sample.concurrently_owned_task_ids:
            metrics.concurrent_ownership_by_task[task_id] = (
                metrics.concurrent_ownership_by_task.get(task_id, 0.0) + elapsed
            )
        for task_id in sample.abandoned_task_ids:
            metrics.abandoned_time_by_task[task_id] = (
                metrics.abandoned_time_by_task.get(task_id, 0.0) + elapsed
            )
        for task_id in sample.dropped_task_ids:
            metrics.dropped_time_by_task[task_id] = (
                metrics.dropped_time_by_task.get(task_id, 0.0) + elapsed
            )
        for task_id in sample.contested_task_ids:
            metrics.contested_duration_by_task[task_id] = (
                metrics.contested_duration_by_task.get(task_id, 0.0) + elapsed
            )


__all__ = [
    "EMPTY_SAMPLE",
    "OwnershipEvaluator",
    "OwnershipSample",
    "sample_ownership",
]
