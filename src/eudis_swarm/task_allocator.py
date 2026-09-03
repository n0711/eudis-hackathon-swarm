"""Legacy deterministic assignment proposals and comparison scoring.

The normal simulation does not invoke this centralized allocator: receiver-local
utility, claims, gossip, and ``TaskClaimStore.owns_task`` govern execution there.
These helpers remain for compatibility, focused unit tests, and policy comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot
from typing import Iterable, Mapping, Protocol

from .agent import Agent, Position
from .peer_state import PeerStateStore
from .task import Task, TaskStatus
from .validation import (
    validate_nonnegative_real,
    validate_positive_integer,
    validate_positive_real,
)


def _available_agents(agents: Iterable[Agent]) -> dict[int, Agent]:
    return {agent.agent_id: agent for agent in agents if agent.available}


def _unassigned_tasks(tasks: Iterable[Task]) -> dict[int, Task]:
    return {
        task.task_id: task
        for task in tasks
        if task.status is TaskStatus.UNASSIGNED and task.assigned_agent is None
    }


@dataclass(frozen=True, slots=True)
class Allocation:
    agent_id: int
    task_id: int
    distance: float
    policy: str = "distance"
    predicted_peer_degree: int | None = None
    predicted_isolation: bool | None = None

    def __post_init__(self) -> None:
        validate_positive_integer(self.agent_id, name="allocation agent_id")
        validate_positive_integer(self.task_id, name="allocation task_id")
        validate_nonnegative_real(self.distance, name="allocation distance")
        if self.policy not in {"distance", "connectivity"}:
            raise ValueError("unknown allocation policy")
        if self.policy == "distance":
            if (
                self.predicted_peer_degree is not None
                or self.predicted_isolation is not None
            ):
                raise ValueError("distance allocations must not contain predictions")
            return
        if (
            not isinstance(self.predicted_peer_degree, int)
            or isinstance(self.predicted_peer_degree, bool)
            or self.predicted_peer_degree < 0
            or not isinstance(self.predicted_isolation, bool)
        ):
            raise ValueError("connectivity allocations require valid predictions")
        if self.predicted_isolation != (self.predicted_peer_degree == 0):
            raise ValueError("predicted isolation must match predicted peer degree")


class AllocationPolicy(Protocol):
    """Minimal proposal interface consumed by the authoritative Mission."""

    def allocate(
        self, agents: Iterable[Agent], tasks: Iterable[Task]
    ) -> list[Allocation]: ...


class TaskAllocator:
    """Greedily select globally nearest available UAV/task pairs."""

    def allocate(
        self, agents: Iterable[Agent], tasks: Iterable[Task]
    ) -> list[Allocation]:
        """Propose unique assignments without mutating mission state."""

        candidate_agents = _available_agents(agents)
        candidate_tasks = _unassigned_tasks(tasks)
        allocations: list[Allocation] = []

        ranked_pairs = sorted(
            (
                (
                    agent.distance_to(task.position),
                    agent.agent_id,
                    task.task_id,
                )
                for agent in candidate_agents.values()
                for task in candidate_tasks.values()
            )
        )
        remaining_agent_ids = set(candidate_agents)
        remaining_task_ids = set(candidate_tasks)
        for distance, agent_id, task_id in ranked_pairs:
            if agent_id not in remaining_agent_ids or task_id not in remaining_task_ids:
                continue
            allocations.append(
                Allocation(agent_id=agent_id, task_id=task_id, distance=distance)
            )
            remaining_agent_ids.remove(agent_id)
            remaining_task_ids.remove(task_id)
            if not remaining_agent_ids or not remaining_task_ids:
                break

        return allocations


class CommunicationAwareTaskAllocator:
    """Prefer assignments that local ``HEARD`` peer evidence predicts stay linked."""

    def __init__(
        self,
        peer_state_stores: Mapping[int, PeerStateStore],
        communication_range: float,
    ) -> None:
        communication_range = validate_positive_real(
            communication_range, name="communication_range"
        )
        for owner_agent_id, store in peer_state_stores.items():
            if owner_agent_id != store.owner_agent_id:
                raise ValueError("peer-state store key must match its owner")
        self._peer_state_stores = dict(peer_state_stores)
        self._communication_range = communication_range

    def _heard_peer_positions(self, agent_id: int) -> tuple[Position, ...]:
        try:
            store = self._peer_state_stores[agent_id]
        except KeyError as error:
            raise ValueError(f"missing peer-state store for UAV {agent_id}") from error
        return tuple(
            observation.snapshot.position for observation in store.heard_observations
        )

    def _predicted_degree_from_positions(
        self,
        task: Task,
        fresh_peer_positions: tuple[Position, ...],
    ) -> int:
        task_x, task_y = task.position
        predicted_degree = 0
        for peer_x, peer_y in fresh_peer_positions:
            if hypot(task_x - peer_x, task_y - peer_y) <= self._communication_range:
                predicted_degree += 1
        return predicted_degree

    def _predicted_degree(self, agent_id: int, task: Task) -> int:
        return self._predicted_degree_from_positions(
            task, self._heard_peer_positions(agent_id)
        )

    def evaluate_candidate(self, agent: Agent, task: Task) -> Allocation:
        """Explain one candidate using only the UAV's local peer observations."""

        predicted_degree = self._predicted_degree(agent.agent_id, task)
        return Allocation(
            agent_id=agent.agent_id,
            task_id=task.task_id,
            distance=agent.distance_to(task.position),
            policy="connectivity",
            predicted_peer_degree=predicted_degree,
            predicted_isolation=predicted_degree == 0,
        )

    def allocate(
        self, agents: Iterable[Agent], tasks: Iterable[Task]
    ) -> list[Allocation]:
        """Propose unique pairs using local-knowledge connectivity first."""

        candidate_agents = _available_agents(agents)
        candidate_tasks = _unassigned_tasks(tasks)
        allocations: list[Allocation] = []

        heard_positions_by_agent = {
            agent_id: self._heard_peer_positions(agent_id)
            for agent_id in candidate_agents
        }
        ranked_pairs: list[tuple[int, int, float, int, int]] = []
        for agent in candidate_agents.values():
            heard_peer_positions = heard_positions_by_agent[agent.agent_id]
            for task in candidate_tasks.values():
                degree = self._predicted_degree_from_positions(
                    task, heard_peer_positions
                )
                ranked_pairs.append(
                    (
                        int(degree == 0),
                        -degree,
                        agent.distance_to(task.position),
                        agent.agent_id,
                        task.task_id,
                    )
                )

        # scores are immutable during one proposal batch, so sorting once is
        # equivalent to repeatedly scanning every remaining UAV/task pair.
        ranked_pairs.sort()
        remaining_agent_ids = set(candidate_agents)
        remaining_task_ids = set(candidate_tasks)
        for isolation, negative_degree, distance, agent_id, task_id in ranked_pairs:
            if agent_id not in remaining_agent_ids or task_id not in remaining_task_ids:
                continue
            allocations.append(
                Allocation(
                    agent_id=agent_id,
                    task_id=task_id,
                    distance=distance,
                    policy="connectivity",
                    predicted_peer_degree=-negative_degree,
                    predicted_isolation=bool(isolation),
                )
            )
            remaining_agent_ids.remove(agent_id)
            remaining_task_ids.remove(task_id)
            if not remaining_agent_ids or not remaining_task_ids:
                break

        return allocations


__all__ = [
    "Allocation",
    "AllocationPolicy",
    "CommunicationAwareTaskAllocator",
    "TaskAllocator",
]
