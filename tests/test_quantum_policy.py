"""The receiver-local quantum allocation policy and its simulator wiring."""

from __future__ import annotations

from dataclasses import replace

import pytest

from eudis_swarm import quantum_policy
from eudis_swarm.config import SimulationConfig
from eudis_swarm.quantum_opt import AssignmentProblem, AssignmentSolution
from eudis_swarm.quantum_policy import PeerEstimate, QuantumAllocationPolicy
from eudis_swarm.simulation import Simulation, main
from eudis_swarm.task_allocator import Allocation
from eudis_swarm.task_utility import ReceiverLocalTaskUtility, TaskObjective

#: Chain 1-2-3-4: every other link permanently down.
CHAIN = frozenset({(1, 3), (1, 4), (2, 4)})
#: UAV 4 can reach nobody, and nobody can reach it.
ISOLATE_4 = frozenset({(1, 4), (2, 4), (3, 4)})


def _ranked(position: tuple[float, float], objectives: list[TaskObjective]):
    return ReceiverLocalTaskUtility().rank(1, position, objectives)


def test_policy_yields_the_nearest_objective_to_a_better_placed_peer() -> None:
    objectives = [
        TaskObjective(1, (10.0, 0.0)),
        TaskObjective(2, (-30.0, 0.0)),
        TaskObjective(3, (50.0, 50.0)),
    ]
    ranked = _ranked((0.0, 0.0), objectives)
    assert [utility.task_id for utility in ranked] == [1, 2, 3]
    policy = QuantumAllocationPolicy(solver="brute_force", max_agents=2)
    proposal = policy.propose(
        1, (0.0, 0.0), [PeerEstimate(2, (11.0, 0.0))], ranked, objectives
    )
    assert proposal is not None
    # Peer 2 sits on objective 1; the joint optimum sends this UAV to 2.
    assert proposal.solution.assignment == ((1, 2), (2, 1))
    assert proposal.order == (2, 3, 1)
    assert proposal.proposed_task_id == 2
    assert proposal.utility_task_id == 1
    assert proposal.changed_choice


def test_busy_peer_lead_cost_can_keep_this_uav_on_its_nearest_objective() -> None:
    objectives = [TaskObjective(1, (10.0, 0.0)), TaskObjective(2, (-30.0, 0.0))]
    ranked = _ranked((0.0, 0.0), objectives)
    policy = QuantumAllocationPolicy(solver="brute_force", max_agents=2)
    # Same final position as above, but 100 m still to fly before it is free.
    proposal = policy.propose(
        1, (0.0, 0.0), [PeerEstimate(2, (11.0, 0.0), 100.0)], ranked, objectives
    )
    assert proposal is not None
    # Lead cost is per agent, so with exactly one objective each it shifts both
    # assignments equally; the optimum is still the parallel one.
    assert proposal.solution.assignment == ((1, 2), (2, 1))
    three = [*objectives, TaskObjective(3, (12.0, 0.0))]
    policy = QuantumAllocationPolicy(solver="brute_force", max_agents=3)
    proposal = policy.propose(
        1,
        (0.0, 0.0),
        [PeerEstimate(2, (11.0, 0.0), 100.0), PeerEstimate(3, (13.0, 0.0))],
        _ranked((0.0, 0.0), three),
        three,
    )
    assert proposal is not None
    assert proposal.problem.costs[1][0] == pytest.approx(101.0)


def test_trivial_formulations_are_not_solved() -> None:
    objectives = [TaskObjective(1, (10.0, 0.0)), TaskObjective(2, (-30.0, 0.0))]
    policy = QuantumAllocationPolicy()
    ranked = _ranked((0.0, 0.0), objectives)
    assert policy.propose(1, (0.0, 0.0), [], ranked, objectives) is None
    # A peer list holding only this UAV's own ID is no peer at all.
    assert (
        policy.propose(1, (0.0, 0.0), [PeerEstimate(1, (5.0, 0.0))], ranked, objectives)
        is None
    )
    single = objectives[:1]
    assert (
        policy.propose(
            1,
            (0.0, 0.0),
            [PeerEstimate(2, (5.0, 0.0))],
            _ranked((0.0, 0.0), single),
            single,
        )
        is None
    )


def test_formulation_is_bounded_and_round_robin() -> None:
    objectives = [TaskObjective(t, (10.0 * t, 0.0)) for t in range(1, 7)]
    objectives.append(TaskObjective(7, (0.0, 95.0)))
    ranked = _ranked((0.0, 0.0), objectives)
    policy = QuantumAllocationPolicy(max_agents=2, max_objectives=3)
    problem = policy.formulate(
        1,
        (0.0, 0.0),
        [PeerEstimate(3, (0.0, 100.0)), PeerEstimate(2, (0.0, 90.0))],
        ranked,
        objectives,
    )
    assert problem is not None
    # Nearest peer only; this UAV's nearest, the peer's nearest, then this
    # UAV's second nearest.
    assert problem.agent_ids == (1, 2)
    assert problem.task_ids == (1, 2, 7)
    assert problem.num_variables == 6


def test_infeasible_solver_answer_keeps_utility_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objectives = [TaskObjective(1, (10.0, 0.0)), TaskObjective(2, (-30.0, 0.0))]
    ranked = _ranked((0.0, 0.0), objectives)

    def nothing_feasible(problem: AssignmentProblem, *_: object, **__: object):
        return AssignmentSolution(
            solver="qaoa",
            assignment=(),
            feasible=False,
            cost=None,
            bits=(0,) * problem.num_variables,
            evaluations=1,
        )

    monkeypatch.setattr(quantum_policy, "solve", nothing_feasible)
    proposal = QuantumAllocationPolicy().propose(
        1, (0.0, 0.0), [PeerEstimate(2, (11.0, 0.0))], ranked, objectives
    )
    assert proposal is not None
    assert not proposal.solution.feasible
    assert proposal.order == (1, 2)
    assert not proposal.changed_choice


def test_policy_settings_validate() -> None:
    with pytest.raises(ValueError):
        QuantumAllocationPolicy(solver="annealer")
    with pytest.raises(ValueError):
        QuantumAllocationPolicy(max_agents=0)
    with pytest.raises(ValueError):
        QuantumAllocationPolicy(max_objectives=0)


def test_config_validates_quantum_fields() -> None:
    with pytest.raises(ValueError):
        SimulationConfig(allocation_policy="oracle")
    with pytest.raises(ValueError):
        SimulationConfig(quantum_solver="annealer")
    with pytest.raises(ValueError):
        SimulationConfig(quantum_max_agents=0)
    with pytest.raises(ValueError):
        SimulationConfig(qaoa_depth=0)
    with pytest.raises(ValueError):
        SimulationConfig(qaoa_depth=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SimulationConfig(
            allocation_policy="quantum", quantum_max_agents=4, quantum_max_objectives=5
        )
    # SQA accepts more than the statevector limit, up to its own bound.
    SimulationConfig(
        allocation_policy="quantum",
        quantum_solver="sqa",
        quantum_max_agents=4,
        quantum_max_objectives=5,
    )
    with pytest.raises(ValueError):
        SimulationConfig(
            allocation_policy="quantum",
            quantum_solver="sqa",
            quantum_max_agents=17,
            quantum_max_objectives=16,
        )
    with pytest.raises(ValueError):
        SimulationConfig(allocation_policy="quantum", task_control="centralized")


def test_quantum_allocations_carry_no_connectivity_predictions() -> None:
    Allocation(1, 1, 3.0, policy="quantum")
    with pytest.raises(ValueError, match="quantum"):
        Allocation(1, 1, 3.0, policy="quantum", predicted_peer_degree=1)


def test_default_policy_is_distance_and_never_solves() -> None:
    config = SimulationConfig()
    assert config.allocation_policy == "distance"
    result = Simulation(config).run()
    assert result.metrics.simulation_duration == 17.25
    assert result.quantum_solves == ()


@pytest.mark.parametrize("solver", ["qaoa", "sqa"])
def test_default_scenario_with_the_quantum_policy_is_pinned(solver: str) -> None:
    result = Simulation(
        SimulationConfig(allocation_policy="quantum", quantum_solver=solver)
    ).run()
    metrics = result.metrics
    assert metrics.mission_completed
    assert metrics.completed_task_count == 20
    assert metrics.simulation_duration == 16.75
    assert len(result.quantum_solves) == 20
    assert sum(record.proposal.changed_choice for record in result.quantum_solves) == 2
    assert all(record.proposal.solution.feasible for record in result.quantum_solves)
    assert {record.allocation.policy for record in metrics.allocation_decisions} == {
        "quantum"
    }


def test_same_command_line_gives_identical_quantum_numbers() -> None:
    config = SimulationConfig(
        allocation_policy="quantum",
        task_count=8,
        communication_range=50.0,
        failure_time=6.0,
        time_step=0.5,
        max_simulation_time=30.0,
        random_seed=4,
    )
    first = Simulation(config).run()
    second = Simulation(config).run()
    assert first.metrics.simulation_duration == second.metrics.simulation_duration
    assert first.metrics.contested_duration == second.metrics.contested_duration
    assert first.position_history == second.position_history
    assert [
        (record.allocation.agent_id, record.allocation.task_id, record.timestamp)
        for record in first.metrics.allocation_decisions
    ] == [
        (record.allocation.agent_id, record.allocation.task_id, record.timestamp)
        for record in second.metrics.allocation_decisions
    ]
    assert first.quantum_solves == second.quantum_solves


@pytest.mark.parametrize("solver", ["qaoa", "sqa"])
def test_chain_formulations_contain_only_heard_neighbours(solver: str) -> None:
    config = SimulationConfig(
        allocation_policy="quantum",
        quantum_solver=solver,
        blocked_links=CHAIN,
        task_count=8,
        failure_time=1000.0,
    )
    result = Simulation(config).run()
    assert result.metrics.completed_task_count == 8
    neighbours = {1: {2}, 2: {1, 3}, 3: {2, 4}, 4: {3}}
    seen: set[int] = set()
    for record in result.quantum_solves:
        members = set(record.proposal.problem.agent_ids)
        seen.add(record.agent_id)
        assert record.proposal.problem.agent_ids[0] == record.agent_id
        # Negative: a UAV two hops away is never in the formulation, although
        # it is idle at t=0 and well inside the world's play area.
        assert members <= {record.agent_id} | neighbours[record.agent_id]
    assert seen == {1, 2, 3, 4}
    assert any(
        set(record.proposal.problem.agent_ids) == {2, 1, 3}
        for record in result.quantum_solves
    )


def test_a_never_heard_peer_is_never_formulated() -> None:
    config = SimulationConfig(
        allocation_policy="quantum",
        blocked_links=ISOLATE_4,
        task_count=8,
        failure_time=1000.0,
    )
    result = Simulation(config).run()
    assert result.quantum_solves
    for record in result.quantum_solves:
        assert 4 not in record.proposal.problem.agent_ids
        assert record.agent_id != 4


def test_partitioned_peer_leaves_the_formulation_while_jammed() -> None:
    # UAV 2 is comms-isolated for 1 s <= t < 30 s. Once its last heartbeat ages
    # past HEARD, no other UAV may include it.
    config = SimulationConfig(
        allocation_policy="quantum",
        quantum_solver="sqa",
        task_count=12,
        failure_time=1000.0,
        comm_fault_agent_id=2,
        comm_fault_start=1.0,
        comm_fault_end=30.0,
    )
    result = Simulation(config).run()
    late = [record for record in result.quantum_solves if record.timestamp >= 4.0]
    assert late
    for record in late:
        assert 2 not in record.proposal.problem.agent_ids
        assert record.agent_id != 2


def test_with_nobody_heard_quantum_equals_distance() -> None:
    blocked = frozenset(
        (left, right) for left in range(1, 5) for right in range(left + 1, 5)
    )
    base = SimulationConfig(blocked_links=blocked, task_count=8, failure_time=1000.0)
    distance = Simulation(base).run()
    quantum = Simulation(replace(base, allocation_policy="quantum")).run()
    assert quantum.quantum_solves == ()
    assert quantum.position_history == distance.position_history
    assert quantum.metrics.simulation_duration == distance.metrics.simulation_duration


def test_cli_accepts_the_quantum_policy() -> None:
    assert (
        main(
            [
                "--allocation-policy",
                "quantum",
                "--quantum-solver",
                "sqa",
                "--quantum-max-agents",
                "2",
                "--quantum-max-objectives",
                "2",
                "--qaoa-depth",
                "1",
                "--tasks",
                "6",
                "--log-level",
                "ERROR",
            ]
        )
        == 0
    )
