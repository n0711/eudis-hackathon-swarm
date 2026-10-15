"""Receiver-local quantum-optimised task preference.

One idle UAV formulates a joint assignment over itself and the peers it
currently has HEARD snapshots of, restricted to its own nearest claimable
objectives, solves it with a simulated quantum solver, and reorders its own
utility ranking so the objective the plan gives to itself comes first.

The inputs are exactly what that receiver holds: its own identity and pose,
:class:`PeerEstimate` values derived from delivered heartbeats, and its own
ranked claimable objectives. A peer whose heartbeat reported a current task
enters at that task's briefed position, with the distance it still had to fly
as a lead cost: it is predicted to be free there, later. This module imports no world, network, or
store module. The plan is a preference, not an allocation: the caller still
creates a claim before binding work, and claims, leases and reconciliation
decide ownership. Two UAVs whose local plans disagree simply create competing
claims, which reconciliation resolves as it would for any other policy.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .quantum_opt import (
    AssignmentProblem,
    AssignmentSolution,
    QaoaSettings,
    SqaSettings,
    assignment_qubo,
    solve,
)
from .task_utility import LocalTaskUtility, Position, TaskObjective
from .validation import validate_positive_integer


@dataclass(frozen=True, slots=True)
class PeerEstimate:
    """Where one HEARD peer is predicted to become free, and how far away."""

    peer_id: int
    position: Position
    #: Distance the peer still had to fly when its heartbeat was sent.
    lead_cost: float = 0.0


@dataclass(frozen=True, slots=True)
class QuantumProposal:
    """One receiver's solved formulation and resulting preference order."""

    problem: AssignmentProblem
    solution: AssignmentSolution
    #: Task IDs in preference order (every ranked candidate exactly once).
    order: tuple[int, ...]
    #: The first choice of the plain utility ranking, for comparison.
    utility_task_id: int

    @property
    def proposed_task_id(self) -> int:
        return self.order[0]

    @property
    def changed_choice(self) -> bool:
        return self.order[0] != self.utility_task_id


@dataclass(frozen=True, slots=True)
class QuantumAllocationPolicy:
    """Bounded formulation plus solver choice; holds no mutable state."""

    solver: str = "qaoa"
    max_agents: int = 3
    max_objectives: int = 4
    qaoa: QaoaSettings = field(default_factory=QaoaSettings)
    sqa: SqaSettings = field(default_factory=SqaSettings)

    def __post_init__(self) -> None:
        if self.solver not in {"qaoa", "sqa", "brute_force"}:
            raise ValueError("solver must be 'qaoa', 'sqa' or 'brute_force'")
        validate_positive_integer(self.max_agents, name="max_agents")
        validate_positive_integer(self.max_objectives, name="max_objectives")

    def formulate(
        self,
        agent_id: int,
        own_position: Position,
        peers: Sequence[PeerEstimate],
        ranked: Sequence[LocalTaskUtility],
        objectives: Sequence[TaskObjective],
    ) -> AssignmentProblem | None:
        """Build the bounded problem, or ``None`` when it would be trivial.

        Peers: the ``max_agents - 1`` nearest by predicted distance to this
        UAV. Objectives: chosen round-robin -- this UAV, then each peer in
        that order, takes its nearest not-yet-chosen candidate -- until
        ``max_objectives``, so every included agent has a nearby option and
        no peer is forced onto objectives that only suit this UAV.

        A formulation with one agent or one objective has the receiver's own
        nearest objective as its exact optimum, so it is not solved.
        """

        by_id = {objective.task_id: objective for objective in objectives}
        candidates = [by_id[utility.task_id] for utility in ranked]
        nearest_peers = sorted(
            (
                (
                    peer.lead_cost
                    + math.hypot(
                        peer.position[0] - own_position[0],
                        peer.position[1] - own_position[1],
                    ),
                    peer.peer_id,
                    peer,
                )
                for peer in peers
                if peer.peer_id != agent_id
            ),
            key=lambda item: (item[0], item[1]),
        )[: self.max_agents - 1]
        if not nearest_peers or len(candidates) < 2:
            return None
        pickers = [own_position] + [item[2].position for item in nearest_peers]
        chosen: list[TaskObjective] = []
        while len(chosen) < min(self.max_objectives, len(candidates)):
            for position in pickers:
                if len(chosen) == min(self.max_objectives, len(candidates)):
                    break
                remaining = [item for item in candidates if item not in chosen]
                chosen.append(
                    min(
                        remaining,
                        key=lambda objective: (
                            math.hypot(
                                objective.position[0] - position[0],
                                objective.position[1] - position[1],
                            ),
                            objective.task_id,
                        ),
                    )
                )
        chosen_objectives = sorted(chosen, key=lambda objective: objective.task_id)
        selected = sorted((item[2] for item in nearest_peers), key=lambda p: p.peer_id)
        agents = [(agent_id, own_position)] + [
            (peer.peer_id, peer.position) for peer in selected
        ]
        return assignment_qubo(
            agents,
            chosen_objectives,
            lead_costs=[0.0] + [peer.lead_cost for peer in selected],
        )

    def propose(
        self,
        agent_id: int,
        own_position: Position,
        peers: Sequence[PeerEstimate],
        ranked: Sequence[LocalTaskUtility],
        objectives: Sequence[TaskObjective],
    ) -> QuantumProposal | None:
        """Solve the local formulation and reorder ``ranked`` accordingly.

        Order: the objective assigned to this UAV; then the remaining
        candidates in utility order, except those the plan gives to a peer;
        then those, in utility order. An infeasible solver answer leaves the
        utility order unchanged.
        """

        problem = self.formulate(agent_id, own_position, peers, ranked, objectives)
        if problem is None:
            return None
        solution = solve(problem, self.solver, qaoa=self.qaoa, sqa=self.sqa)
        utility_order = tuple(utility.task_id for utility in ranked)
        if not solution.feasible:
            return QuantumProposal(problem, solution, utility_order, utility_order[0])
        own_task = solution.task_for(agent_id)
        given_to_peers = {
            task_id
            for assigned_agent_id, task_id in solution.assignment
            if assigned_agent_id != agent_id
        }
        first = () if own_task is None else (own_task,)
        rest = tuple(
            task_id
            for task_id in utility_order
            if task_id != own_task and task_id not in given_to_peers
        )
        last = tuple(task_id for task_id in utility_order if task_id in given_to_peers)
        return QuantumProposal(problem, solution, first + rest + last, utility_order[0])


__all__ = ["PeerEstimate", "QuantumAllocationPolicy", "QuantumProposal"]
