"""Pure receiver-local utility scoring for task claim intent.

The scorer accepts only an agent's own identity and position, immutable task
objectives, and costs computed by that same receiver.  It deliberately has no
access to mission state, network topology, mutable tasks, or another UAV.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from math import hypot, isfinite
from numbers import Real
from typing import TypeAlias

from .validation import validate_nonnegative_real, validate_positive_integer

Position: TypeAlias = tuple[float, float]
ScalarCost: TypeAlias = float | int
LocalCostInput: TypeAlias = ScalarCost | Mapping[int, ScalarCost]


def _validated_position(position: object, *, name: str) -> Position:
    if (
        not isinstance(position, (tuple, list))
        or len(position) != 2
        or not all(
            isinstance(value, Real) and not isinstance(value, bool) and isfinite(value)
            for value in position
        )
    ):
        raise ValueError(f"{name} must contain two finite coordinates")
    return (float(position[0]), float(position[1]))


def _validated_cost_source(
    value: LocalCostInput, *, name: str
) -> float | dict[int, float]:
    if isinstance(value, Mapping):
        costs: dict[int, float] = {}
        for task_id, cost in value.items():
            validated_task_id = validate_positive_integer(
                task_id,
                name=f"{name} task_id",
            )
            costs[validated_task_id] = validate_nonnegative_real(
                cost,
                name=f"{name} for Task {validated_task_id}",
            )
        return costs
    return validate_nonnegative_real(value, name=name)


def _cost_for_task(source: float | Mapping[int, float], task_id: int) -> float:
    if isinstance(source, Mapping):
        return source.get(task_id, 0.0)
    return source


@dataclass(frozen=True, slots=True)
class TaskObjective:
    """Immutable mission objective data that every receiver may know."""

    task_id: int
    position: Position

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_id",
            validate_positive_integer(self.task_id, name="objective task_id"),
        )
        object.__setattr__(
            self,
            "position",
            _validated_position(self.position, name="objective position"),
        )


@dataclass(frozen=True, slots=True)
class TaskUtilityWeights:
    """Non-negative weights for the extensible local cost terms."""

    distance: float = 1.0
    resource: float = 0.0
    communication: float = 0.0
    role: float = 0.0

    def __post_init__(self) -> None:
        for name in ("distance", "resource", "communication", "role"):
            object.__setattr__(
                self,
                name,
                validate_nonnegative_real(
                    getattr(self, name),
                    name=f"{name} utility weight",
                ),
            )


@dataclass(frozen=True, slots=True)
class LocalTaskUtility:
    """One receiver's validated cost breakdown for one objective."""

    agent_id: int
    task_id: int
    travel_cost: float
    resource_cost: float
    communication_cost: float
    role_cost: float
    total_cost: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "agent_id",
            validate_positive_integer(self.agent_id, name="utility agent_id"),
        )
        object.__setattr__(
            self,
            "task_id",
            validate_positive_integer(self.task_id, name="utility task_id"),
        )
        for name in (
            "travel_cost",
            "resource_cost",
            "communication_cost",
            "role_cost",
            "total_cost",
        ):
            object.__setattr__(
                self,
                name,
                validate_nonnegative_real(
                    getattr(self, name),
                    name=name.replace("_", " "),
                ),
            )


@dataclass(frozen=True, slots=True)
class ReceiverLocalTaskUtility:
    """Evaluate and rank task intent without consulting global state."""

    weights: TaskUtilityWeights = field(default_factory=TaskUtilityWeights)

    def __post_init__(self) -> None:
        if not isinstance(self.weights, TaskUtilityWeights):
            raise TypeError("weights must be TaskUtilityWeights")

    def evaluate(
        self,
        agent_id: int,
        own_position: Position,
        objective: TaskObjective,
        *,
        resource_cost: LocalCostInput = 0.0,
        communication_cost: LocalCostInput = 0.0,
        role_cost: LocalCostInput = 0.0,
    ) -> LocalTaskUtility:
        """Score one objective from values available to this receiver alone."""

        validated_agent_id = validate_positive_integer(agent_id, name="agent_id")
        position = _validated_position(own_position, name="own position")
        if not isinstance(objective, TaskObjective):
            raise TypeError("objective must be TaskObjective")

        resource_source = _validated_cost_source(
            resource_cost,
            name="resource cost",
        )
        communication_source = _validated_cost_source(
            communication_cost,
            name="communication cost",
        )
        role_source = _validated_cost_source(role_cost, name="role cost")
        return self._evaluate_validated(
            validated_agent_id,
            position,
            objective,
            resource_source,
            communication_source,
            role_source,
        )

    def _evaluate_validated(
        self,
        agent_id: int,
        own_position: Position,
        objective: TaskObjective,
        resource_source: float | Mapping[int, float],
        communication_source: float | Mapping[int, float],
        role_source: float | Mapping[int, float],
    ) -> LocalTaskUtility:
        task_id = objective.task_id
        travel = hypot(
            objective.position[0] - own_position[0],
            objective.position[1] - own_position[1],
        )
        resource = _cost_for_task(resource_source, task_id)
        communication = _cost_for_task(communication_source, task_id)
        role = _cost_for_task(role_source, task_id)
        total = (
            self.weights.distance * travel
            + self.weights.resource * resource
            + self.weights.communication * communication
            + self.weights.role * role
        )
        return LocalTaskUtility(
            agent_id=agent_id,
            task_id=task_id,
            travel_cost=travel,
            resource_cost=resource,
            communication_cost=communication,
            role_cost=role,
            total_cost=total,
        )

    def rank(
        self,
        agent_id: int,
        own_position: Position,
        objectives: Iterable[TaskObjective],
        *,
        resource_cost: LocalCostInput = 0.0,
        communication_cost: LocalCostInput = 0.0,
        role_cost: LocalCostInput = 0.0,
    ) -> tuple[LocalTaskUtility, ...]:
        """Return objectives ordered by ``(total_cost, task_id)``."""

        validated_agent_id = validate_positive_integer(agent_id, name="agent_id")
        position = _validated_position(own_position, name="own position")
        objective_items = tuple(objectives)
        if not all(isinstance(item, TaskObjective) for item in objective_items):
            raise TypeError("objectives must contain only TaskObjective values")
        task_ids = tuple(item.task_id for item in objective_items)
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("objectives must use unique task IDs")

        resource_source = _validated_cost_source(
            resource_cost,
            name="resource cost",
        )
        communication_source = _validated_cost_source(
            communication_cost,
            name="communication cost",
        )
        role_source = _validated_cost_source(role_cost, name="role cost")
        ranked = (
            self._evaluate_validated(
                validated_agent_id,
                position,
                objective,
                resource_source,
                communication_source,
                role_source,
            )
            for objective in objective_items
        )
        return tuple(sorted(ranked, key=lambda item: (item.total_cost, item.task_id)))


__all__ = [
    "LocalTaskUtility",
    "ReceiverLocalTaskUtility",
    "TaskObjective",
    "TaskUtilityWeights",
]
