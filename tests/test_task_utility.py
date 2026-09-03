"""Tests for pure receiver-local task-intent utility."""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError
from math import inf, nan
from pathlib import Path

import pytest

from eudis_swarm.task_utility import (
    LocalTaskUtility,
    ReceiverLocalTaskUtility,
    TaskObjective,
    TaskUtilityWeights,
)


def test_default_distance_ranking_uses_task_id_for_equal_costs() -> None:
    utility = ReceiverLocalTaskUtility()
    objectives = (
        TaskObjective(9, (3.0, 4.0)),
        TaskObjective(3, (0.0, 2.0)),
        TaskObjective(2, (-3.0, -4.0)),
    )

    ranked = utility.rank(7, (0.0, 0.0), objectives)

    assert [item.task_id for item in ranked] == [3, 2, 9]
    assert [item.total_cost for item in ranked] == [2.0, 5.0, 5.0]
    assert all(item.agent_id == 7 for item in ranked)
    assert all(item.resource_cost == 0.0 for item in ranked)


def test_weighted_scalar_and_per_task_costs_are_kept_in_the_breakdown() -> None:
    utility = ReceiverLocalTaskUtility(
        TaskUtilityWeights(
            distance=0.5,
            resource=2.0,
            communication=3.0,
            role=4.0,
        )
    )

    result = utility.evaluate(
        4,
        (0.0, 0.0),
        TaskObjective(8, (3.0, 4.0)),
        resource_cost=1.5,
        communication_cost={8: 2.0, 9: 99.0},
        role_cost={8: 0.25},
    )

    assert result == LocalTaskUtility(
        agent_id=4,
        task_id=8,
        travel_cost=5.0,
        resource_cost=1.5,
        communication_cost=2.0,
        role_cost=0.25,
        total_cost=12.5,
    )


def test_ranking_is_independent_of_objective_and_mapping_order() -> None:
    utility = ReceiverLocalTaskUtility(
        TaskUtilityWeights(distance=1.0, communication=2.0)
    )
    forward = (
        TaskObjective(5, (1.0, 0.0)),
        TaskObjective(2, (3.0, 0.0)),
        TaskObjective(8, (2.0, 0.0)),
    )
    reverse = tuple(reversed(forward))

    first = utility.rank(
        3,
        (0.0, 0.0),
        forward,
        communication_cost={5: 2.0, 2: 0.0, 8: 0.5},
    )
    second = utility.rank(
        3,
        (0.0, 0.0),
        reverse,
        communication_cost={8: 0.5, 2: 0.0, 5: 2.0},
    )

    assert first == second
    assert [item.task_id for item in first] == [2, 8, 5]


@pytest.mark.parametrize(
    "value",
    [-1.0, inf, nan, True],
)
def test_weights_reject_invalid_values(value: float) -> None:
    with pytest.raises(ValueError, match="weight"):
        TaskUtilityWeights(communication=value)


@pytest.mark.parametrize(
    "task_id, position",
    [
        (0, (0.0, 0.0)),
        (True, (0.0, 0.0)),
        (1, (nan, 0.0)),
        (1, (0.0, inf)),
        (1, (0.0,)),
    ],
)
def test_objective_rejects_invalid_identity_or_position(
    task_id: int,
    position: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError):
        TaskObjective(task_id, position)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"agent_id": 0},
        {"own_position": (nan, 0.0)},
        {"resource_cost": -1.0},
        {"communication_cost": {1: inf}},
        {"role_cost": {True: 1.0}},
    ],
)
def test_evaluation_rejects_invalid_local_inputs(kwargs: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        "agent_id": 1,
        "own_position": (0.0, 0.0),
        "objective": TaskObjective(1, (1.0, 0.0)),
    }
    arguments.update(kwargs)

    with pytest.raises(ValueError):
        ReceiverLocalTaskUtility().evaluate(**arguments)  # type: ignore[arg-type]


def test_values_are_immutable_and_duplicate_objectives_are_rejected() -> None:
    objective = TaskObjective(1, [1.0, 2.0])  # type: ignore[arg-type]
    assert objective.position == (1.0, 2.0)
    with pytest.raises(FrozenInstanceError):
        objective.task_id = 2  # type: ignore[misc]

    utility = ReceiverLocalTaskUtility()
    with pytest.raises(ValueError, match="unique task IDs"):
        utility.rank(1, (0.0, 0.0), (objective, objective))

    with pytest.raises(ValueError, match="agent_id"):
        utility.rank(0, (0.0, 0.0), ())
    with pytest.raises(ValueError, match="own position"):
        utility.rank(1, (inf, 0.0), ())


def test_scorer_rejects_non_objective_dependencies() -> None:
    with pytest.raises(TypeError, match="weights"):
        ReceiverLocalTaskUtility(weights=object())  # type: ignore[arg-type]

    utility = ReceiverLocalTaskUtility()
    with pytest.raises(TypeError, match="objective must"):
        utility.evaluate(1, (0.0, 0.0), object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="objectives must"):
        utility.rank(1, (0.0, 0.0), (object(),))  # type: ignore[arg-type]


def test_public_methods_expose_only_receiver_local_inputs() -> None:
    evaluate_parameters = set(
        inspect.signature(ReceiverLocalTaskUtility.evaluate).parameters
    )
    rank_parameters = set(inspect.signature(ReceiverLocalTaskUtility.rank).parameters)
    expected_common = {
        "self",
        "agent_id",
        "own_position",
        "resource_cost",
        "communication_cost",
        "role_cost",
    }
    assert evaluate_parameters == expected_common | {"objective"}
    assert rank_parameters == expected_common | {"objectives"}

    source_path = Path(__file__).parents[1] / "src/eudis_swarm/task_utility.py"
    syntax = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(syntax)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    prohibited = {
        "agent",
        "communication",
        "mission",
        "peer_state",
        "task",
        "task_claims",
    }
    assert imported_modules.isdisjoint(prohibited)
