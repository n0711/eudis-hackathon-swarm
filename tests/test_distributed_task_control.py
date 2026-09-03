"""Architecture-boundary tests for receiver-local task execution authority."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from eudis_swarm.agent import Agent, Position
from eudis_swarm.config import SimulationConfig
from eudis_swarm.mission import MissionEventKind
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import Task, TaskOwnershipState, TaskStatus
from eudis_swarm.task_allocator import Allocation


class _RaisingAllocator:
    def allocate(
        self,
        agents: Iterable[Agent],
        tasks: Iterable[Task],
    ) -> list[Allocation]:
        del agents, tasks
        raise AssertionError("the centralized allocator must not run")


def _simulation(
    agent_positions: tuple[Position, ...],
    task_positions: tuple[Position, ...],
    *,
    lease_duration: float = 4.0,
    completion_tolerance: float = 0.0,
) -> Simulation:
    config = SimulationConfig(
        agent_count=len(agent_positions),
        task_count=len(task_positions),
        agent_speed=1.0,
        time_step=0.5,
        completion_tolerance=completion_tolerance,
        heartbeat_interval=1.0,
        failure_timeout=2.5,
        peer_state_stale_after=2.5,
        claim_lease_duration=lease_duration,
        failure_agent_id=1,
        failure_time=100.0,
        max_simulation_time=10.0,
        communication_range=1_000.0,
    )
    agents = [
        Agent(agent_id=index, position=position, speed=1.0)
        for index, position in enumerate(agent_positions, start=1)
    ]
    tasks = [
        Task(task_id=index, position=position)
        for index, position in enumerate(task_positions, start=1)
    ]
    return Simulation(config, agents=agents, tasks=tasks)


def _start_network(
    simulation: Simulation,
    *,
    blocked_links: Iterable[tuple[int, int]] = (),
) -> None:
    simulation.mission.start(0.0)
    simulation.communication_graph.update(
        {agent.agent_id: agent.position for agent in simulation.mission.ordered_agents},
        blocked_links=blocked_links,
    )


def _create_and_activate(
    simulation: Simulation,
    agent_id: int,
    task_id: int,
    timestamp: float,
) -> None:
    simulation.task_claim_stores[agent_id].create_claim(task_id, timestamp)
    simulation.mission.record_task_claim(agent_id, task_id, timestamp)
    assert simulation.mission.activate_claimed_task(
        agent_id,
        task_id,
        timestamp,
    )


def test_normal_acquisition_never_calls_the_centralized_allocator() -> None:
    simulation = _simulation(
        ((0.0, 0.0), (10.0, 0.0)),
        ((1.0, 0.0), (9.0, 0.0)),
    )
    simulation.mission.allocator = _RaisingAllocator()

    result = simulation.run()

    assert result.metrics.mission_completed is True
    assert result.metrics.completed_task_count == 2
    assert result.metrics.allocation_decisions
    assert {event.kind for event in result.mission.events} >= {
        MissionEventKind.TASK_CLAIMED,
        MissionEventKind.TASK_COMPLETED,
    }


def test_current_task_alone_cannot_move_project_complete_or_renew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulation = _simulation(
        ((0.0, 0.0),),
        ((1.0, 0.0),),
        completion_tolerance=2.0,
    )
    _start_network(simulation)
    agent = simulation.mission.agents[1]
    task = simulation.mission.tasks[1]
    store = simulation.task_claim_stores[1]
    agent.assign_task(1)

    assert simulation._project_positions(1.0, 0.0)[1] == (0.0, 0.0)
    simulation._advance_agents(1.0, 0.0)
    assert agent.position == (0.0, 0.0)
    assert simulation._complete_arrivals(0.0) == 0
    assert task.status is TaskStatus.UNASSIGNED
    with pytest.raises(RuntimeError, match="actionable local claim"):
        simulation.mission.complete_task(1, 1, 0.0)

    renewal_calls: list[tuple[int, float]] = []

    def record_forbidden_renewal(task_id: int, timestamp: float) -> object:
        renewal_calls.append((task_id, timestamp))
        raise AssertionError("a bare current_task pointer must not renew a claim")

    monkeypatch.setattr(store, "renew_claim", record_forbidden_renewal)
    simulation._exchange_claim_evidence(0.0)

    assert renewal_calls == []
    assert any(
        event.kind is MissionEventKind.TASK_STOOD_DOWN
        for event in simulation.mission.events
    )
    # Local planning may subsequently acquire the task legitimately, but that is
    # a first claim rather than a renewal of the unauthorized pointer.
    assert store.view(1, 0.0).epoch == 1


def test_simultaneous_claims_converge_and_loser_stops_before_more_motion() -> None:
    simulation = _simulation(
        ((0.0, 0.0), (0.0, 0.0)),
        ((10.0, 0.0),),
    )
    _start_network(simulation)
    participants = (1, 2)

    assert simulation._create_local_claim_intents(0.0) == 2
    assert simulation.mission.activate_claimed_task(1, 1, 0.0)
    assert simulation.mission.activate_claimed_task(2, 1, 0.0)
    assert all(
        simulation.task_claim_stores[agent_id].owns_task(1, 0.0)
        for agent_id in participants
    )

    simulation._gossip_task_evidence(0.0, participants)
    assert simulation._resolve_claims_and_stand_down(0.0, participants) == 1

    winner = simulation.mission.agents[1]
    loser = simulation.mission.agents[2]
    assert winner.current_task == 1
    assert loser.current_task is None
    assert all(
        simulation.task_claim_stores[agent_id].view(1, 0.0).known_owner_agent_id == 1
        for agent_id in participants
    )

    simulation._advance_agents(1.0, 0.0)
    assert winner.position == (1.0, 0.0)
    assert loser.position == (0.0, 0.0)


def test_partition_allows_split_ownership_then_reconnect_converges() -> None:
    simulation = _simulation(
        ((0.0, 0.0), (20.0, 0.0)),
        ((10.0, 0.0),),
    )
    participants = (1, 2)
    _start_network(simulation, blocked_links=((1, 2),))

    assert simulation._create_local_claim_intents(0.0) == 2
    assert simulation.mission.activate_claimed_task(1, 1, 0.0)
    assert simulation.mission.activate_claimed_task(2, 1, 0.0)
    simulation._gossip_task_evidence(0.0, participants)
    assert simulation._resolve_claims_and_stand_down(0.0, participants) == 0
    assert all(
        simulation.task_claim_stores[agent_id].owns_task(1, 0.0)
        for agent_id in participants
    )

    simulation._advance_agents(1.0, 0.0)
    assert simulation.mission.agents[1].position == (1.0, 0.0)
    assert simulation.mission.agents[2].position == (19.0, 0.0)

    simulation.communication_graph.update(
        {agent.agent_id: agent.position for agent in simulation.mission.ordered_agents}
    )
    simulation._gossip_task_evidence(1.0, participants)
    assert simulation._resolve_claims_and_stand_down(1.0, participants) == 1
    assert simulation.mission.agents[2].current_task is None
    assert all(
        simulation.task_claim_stores[agent_id].view(1, 1.0).known_owner_agent_id == 1
        for agent_id in participants
    )

    simulation._advance_agents(1.0, 1.0)
    assert simulation.mission.agents[1].position == (2.0, 0.0)
    assert simulation.mission.agents[2].position == (19.0, 0.0)


def test_received_completion_stands_down_duplicate_and_blocks_reclaim() -> None:
    simulation = _simulation(
        ((0.0, 0.0), (20.0, 0.0)),
        ((10.0, 0.0),),
    )
    participants = (1, 2)
    _start_network(simulation, blocked_links=((1, 2),))
    _create_and_activate(simulation, 1, 1, 0.0)
    _create_and_activate(simulation, 2, 1, 0.0)
    simulation._gossip_task_evidence(0.0, participants)

    simulation.mission.complete_task(1, 1, 0.5)
    simulation._gossip_task_evidence(0.5, participants)
    assert simulation.mission.agents[2].current_task == 1
    assert simulation.task_claim_stores[2].owns_task(1, 0.5)

    simulation.communication_graph.update(
        {agent.agent_id: agent.position for agent in simulation.mission.ordered_agents}
    )
    simulation._gossip_task_evidence(1.0, participants)
    assert simulation._resolve_claims_and_stand_down(1.0, participants) == 1

    duplicate_store = simulation.task_claim_stores[2]
    assert simulation.mission.agents[2].current_task is None
    assert duplicate_store.view(1, 1.0).state is TaskOwnershipState.COMPLETE
    assert duplicate_store.can_create_claim(1, 100.0) is False

    # Observer projection is not an input: even corrupting it cannot defeat
    # receiver-local terminal evidence.
    simulation.mission.tasks[1].status = TaskStatus.UNASSIGNED
    simulation.mission.tasks[1].assigned_agent = None
    assert simulation._create_local_claim_intents(100.0) == 0


def test_voluntary_release_is_gossiped_before_peer_reassignment() -> None:
    simulation = _simulation(
        ((0.0, 0.0), (20.0, 0.0)),
        ((10.0, 0.0),),
    )
    participants = (1, 2)
    _start_network(simulation)
    _create_and_activate(simulation, 1, 1, 0.0)
    simulation._gossip_task_evidence(0.0, participants)
    simulation._resolve_claims_and_stand_down(0.0, participants)

    assert simulation.mission.release_owned_task(1, 1, 0.5)
    simulation._gossip_task_evidence(0.5, participants)
    assert simulation.task_claim_stores[2].can_create_claim(1, 0.5)

    _create_and_activate(simulation, 2, 1, 0.5)
    simulation._gossip_task_evidence(0.5, participants)
    simulation._resolve_claims_and_stand_down(0.5, participants)

    assert simulation.mission.agents[1].current_task is None
    assert simulation.mission.agents[2].current_task == 1
    assert simulation.task_claim_stores[2].owns_task(1, 0.5)


def test_physical_owner_failure_reassigns_only_after_local_lease_expiry() -> None:
    simulation = _simulation(
        ((0.0, 0.0), (20.0, 0.0)),
        ((10.0, 0.0),),
        lease_duration=2.0,
    )
    _start_network(simulation)
    _create_and_activate(simulation, 1, 1, 0.0)
    simulation._gossip_task_evidence(0.0, (1, 2))
    assert simulation.mission.inject_failure(1, 0.5)

    simulation._exchange_claim_evidence(2.0)
    peer_store = simulation.task_claim_stores[2]
    assert peer_store.view(1, 2.0).state is TaskOwnershipState.CLAIMED_BY_PEER_STALE
    assert simulation.mission.agents[2].current_task is None

    simulation._exchange_claim_evidence(2.000001)
    assert peer_store.owns_task(1, 2.000001)
    assert simulation.mission.agents[2].current_task == 1
    assert simulation.mission.task_is_executable(2, 1, 2.000001)


def test_mutable_task_projection_cannot_change_local_intent_selection() -> None:
    baseline = _simulation(
        ((0.0, 0.0),),
        ((1.0, 0.0), (5.0, 0.0)),
    )
    altered = _simulation(
        ((0.0, 0.0),),
        ((1.0, 0.0), (5.0, 0.0)),
    )
    baseline.mission.start(0.0)
    altered.mission.start(0.0)
    altered_task = altered.mission.tasks[1]
    altered_task.assign(1)
    altered_task.complete(1)

    assert baseline._create_local_claim_intents(0.0) == 1
    assert altered._create_local_claim_intents(0.0) == 1
    baseline_claim = baseline.task_claim_stores[1].claims_for_broadcast(0.0)
    altered_claim = altered.task_claim_stores[1].claims_for_broadcast(0.0)

    assert [claim.task_id for claim in baseline_claim] == [1]
    assert [claim.task_id for claim in altered_claim] == [1]


def test_default_protocol_metrics_separate_inactive_work_from_link_attempts() -> None:
    result = Simulation(SimulationConfig()).run()
    metrics = result.metrics

    assert metrics.protocol_messages_attempted == 178
    assert metrics.protocol_messages_delivered == 178
    assert metrics.protocol_messages_undelivered == 0
    assert metrics.protocol_useful_first_deliveries == 178
    assert metrics.protocol_messages_forwarded == 0
    assert metrics.protocol_duplicate_source_publications == 1_552
    assert metrics.protocol_duplicate_route_suppressions == 428
    assert metrics.protocol_inactive_endpoint_deferrals == 53
    end_time = metrics.end_time
    assert end_time is not None
    assert result.task_claim_stores is not None
    for agent_id, store in result.task_claim_stores.items():
        if not result.mission.agents[agent_id].responsive:
            continue
        assert all(store.view(task_id, end_time).complete for task_id in store.task_ids)
