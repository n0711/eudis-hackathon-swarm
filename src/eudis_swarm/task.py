"""Define observer-facing mission tasks and the local ownership vocabulary.

The legacy heartbeat classifier remains a compatibility helper, while the formal
lease and reconciliation state machine lives in the pointer-free claim protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from numbers import Real
from typing import Collection

from .agent import Position
from .peer_state import PeerKnowledgeState, PeerStateStore
from .validation import validate_positive_integer


class TaskStatus(str, Enum):
    UNASSIGNED = "UNASSIGNED"
    ASSIGNED = "ASSIGNED"
    COMPLETED = "COMPLETED"


class TaskOwnershipState(str, Enum):
    """One UAV's evidence-based view of ownership for a task."""

    UNCLAIMED = "UNCLAIMED"
    OWNED_BY_SELF = "OWNED_BY_SELF"
    CLAIMED_BY_PEER_FRESH = "CLAIMED_BY_PEER_FRESH"
    CLAIMED_BY_PEER_STALE = "CLAIMED_BY_PEER_STALE"
    CONTESTED = "CONTESTED"
    COMPLETE = "COMPLETE"


def classify_task_ownership(
    task_id: int,
    *,
    own_current_task: int | None,
    peer_state_store: PeerStateStore,
    known_completed_task_ids: Collection[int] = (),
) -> TaskOwnershipState:
    """Classify legacy heartbeat evidence without consulting authoritative state.

    Completion evidence is terminal, and multiple observed claimant IDs are
    contested. New distributed decisions should use ``TaskClaimStore`` because
    heartbeats do not carry formal claim epochs, leases, or release evidence.
    """

    task_id = validate_positive_integer(task_id, name="task_id")
    if own_current_task is not None:
        validate_positive_integer(own_current_task, name="own_current_task")
    completed_task_ids = frozenset(known_completed_task_ids)
    for completed_task_id in completed_task_ids:
        validate_positive_integer(
            completed_task_id,
            name="known completed task ID",
        )
    if task_id in completed_task_ids:
        return TaskOwnershipState.COMPLETE

    peer_claim_states: list[PeerKnowledgeState] = []
    for peer_agent_id in peer_state_store.peer_agent_ids:
        observation = peer_state_store.observation_for(peer_agent_id)
        if observation is None or observation.snapshot.current_task != task_id:
            continue
        peer_claim_states.append(peer_state_store.state_for(peer_agent_id))

    claimant_count = int(own_current_task == task_id) + len(peer_claim_states)
    if claimant_count > 1:
        return TaskOwnershipState.CONTESTED
    if own_current_task == task_id:
        return TaskOwnershipState.OWNED_BY_SELF
    if not peer_claim_states:
        return TaskOwnershipState.UNCLAIMED
    if peer_claim_states[0] is PeerKnowledgeState.FRESH:
        return TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    if peer_claim_states[0] is PeerKnowledgeState.STALE:
        return TaskOwnershipState.CLAIMED_BY_PEER_STALE
    raise RuntimeError("a stored peer claim must have fresh or stale evidence")


@dataclass(slots=True)
class Task:
    """A point objective with observer-facing progress and owner projection.

    In distributed-control runs, ``assigned_agent`` is not authorization and
    cannot represent concurrent partition-local owners. ``TaskClaimStore`` is
    the only operational ownership source.
    """

    task_id: int
    position: Position
    status: TaskStatus = TaskStatus.UNASSIGNED
    assigned_agent: int | None = None

    def __post_init__(self) -> None:
        validate_positive_integer(self.task_id, name="task_id")
        if len(self.position) != 2 or not all(
            isinstance(value, Real) and not isinstance(value, bool) and isfinite(value)
            for value in self.position
        ):
            raise ValueError("position must contain two finite coordinates")

    def assign(self, agent_id: int) -> None:
        validate_positive_integer(agent_id, name="agent_id")
        if self.status is not TaskStatus.UNASSIGNED or self.assigned_agent is not None:
            raise ValueError(f"Task {self.task_id} is not unassigned")
        self.status = TaskStatus.ASSIGNED
        self.assigned_agent = agent_id

    def release(self) -> None:
        if self.status is not TaskStatus.ASSIGNED:
            raise ValueError(f"Task {self.task_id} is not assigned")
        self.status = TaskStatus.UNASSIGNED
        self.assigned_agent = None

    def complete(self, agent_id: int) -> None:
        validate_positive_integer(agent_id, name="agent_id")
        if self.status is not TaskStatus.ASSIGNED or self.assigned_agent != agent_id:
            raise ValueError(f"Task {self.task_id} is not owned by UAV {agent_id}")
        self.status = TaskStatus.COMPLETED


__all__ = [
    "Task",
    "TaskOwnershipState",
    "TaskStatus",
    "classify_task_ownership",
]
