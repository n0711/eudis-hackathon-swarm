"""Versioned operator intent, sitting above local distributed execution.

A contract is what the operator asked for: which objectives matter most, how
few UAVs may still attempt the mission, and what to do when coordination
degrades. It is deliberately *not* a plan. Nothing here says who does what --
that stays with each UAV's own claim store, because the operator is not on the
network when the partition happens.

Contracts are immutable and versioned. Amending intent means issuing a new
version, which propagates like any other evidence and may reach different UAVs
at different times; a UAV acts on the newest version it has actually received,
never on one it has merely been told exists.

This module imports only ``validation`` on purpose. Operator intent is an
input to a UAV, so it must never become a route to world truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .validation import (
    validate_nonnegative_real,
    validate_positive_integer,
    validate_positive_real,
)

DEFAULT_PRIORITY = 1


class DegradationPolicy(str, Enum):
    """What the operator wants done when coordination evidence runs out.

    The choice is genuinely operational rather than technical: pressing on
    without coordination risks duplicated effort, and holding risks leaving
    objectives unserviced. Neither is universally right, so the operator says.
    """

    #: Keep working from local evidence alone. Accepts duplicated effort.
    CONTINUE = "CONTINUE"
    #: Finish work already owned, take no new objectives. Accepts idle capacity.
    HOLD = "HOLD"
    #: Release owned work and stop. For when acting uncoordinated is worse
    #: than not acting.
    ABORT = "ABORT"


@dataclass(frozen=True, slots=True)
class MissionContract:
    """One immutable version of operator intent."""

    version: int
    name: str
    #: ``(task_id, priority)`` pairs, highest priority first when tied on id.
    #: Objectives absent from this table carry ``DEFAULT_PRIORITY``.
    task_priorities: tuple[tuple[int, int], ...] = ()
    #: The operator's floor on how few UAVs may still attempt the mission.
    minimum_participants: int = 1
    degradation_policy: DegradationPolicy = DegradationPolicy.CONTINUE
    #: How long a UAV tolerates insufficient coordination evidence before the
    #: degradation policy engages. Measured on the receiver's own clock.
    degraded_grace: float = 5.0
    #: How long coordination must look healthy again before resuming.
    resume_after_stable_for: float = 2.0

    def __post_init__(self) -> None:
        validate_positive_integer(self.version, name="contract version")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("contract name must be a non-empty string")
        if not isinstance(self.degradation_policy, DegradationPolicy):
            raise ValueError("degradation_policy must be a DegradationPolicy")
        validate_positive_integer(
            self.minimum_participants, name="contract minimum_participants"
        )
        validate_positive_real(self.degraded_grace, name="contract degraded_grace")
        validate_nonnegative_real(
            self.resume_after_stable_for, name="contract resume_after_stable_for"
        )

        seen: set[int] = set()
        for entry in self.task_priorities:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("task_priorities entries must be (task_id, priority)")
            task_id, priority = entry
            validate_positive_integer(task_id, name="contract task_id")
            validate_positive_integer(priority, name="contract priority")
            if task_id in seen:
                raise ValueError(f"duplicate priority for task {task_id}")
            seen.add(task_id)
        if tuple(sorted(self.task_priorities)) != self.task_priorities:
            raise ValueError("task_priorities must be sorted for deterministic replay")

    @property
    def prioritised_task_ids(self) -> tuple[int, ...]:
        """Objectives the operator singled out, most important first."""

        return tuple(
            task_id
            for task_id, _ in sorted(
                self.task_priorities, key=lambda item: (-item[1], item[0])
            )
        )

    def priority_for(self, task_id: int) -> int:
        """Return the operator's priority for one objective."""

        validate_positive_integer(task_id, name="task_id")
        for candidate, priority in self.task_priorities:
            if candidate == task_id:
                return priority
        return DEFAULT_PRIORITY

    def supersedes(self, other: MissionContract | None) -> bool:
        """Whether this version replaces one a UAV already holds.

        Strictly newer only. A replayed or reordered delivery of an older
        version must never roll intent backwards.
        """

        return other is None or self.version > other.version


def build_contract(
    version: int,
    name: str,
    *,
    task_priorities: Mapping[int, int] | None = None,
    minimum_participants: int = 1,
    degradation_policy: DegradationPolicy = DegradationPolicy.CONTINUE,
    degraded_grace: float = 5.0,
    resume_after_stable_for: float = 2.0,
) -> MissionContract:
    """Build a contract from loose input, normalising order for replay."""

    pairs = sorted((task_priorities or {}).items())
    return MissionContract(
        version=version,
        name=name,
        task_priorities=tuple(pairs),
        minimum_participants=minimum_participants,
        degradation_policy=degradation_policy,
        degraded_grace=degraded_grace,
        resume_after_stable_for=resume_after_stable_for,
    )


__all__ = [
    "DEFAULT_PRIORITY",
    "DegradationPolicy",
    "MissionContract",
    "build_contract",
]
