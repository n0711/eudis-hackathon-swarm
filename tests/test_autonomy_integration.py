"""Cross-layer contracts between the autonomy EFSMs and the simulator."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from eudis_swarm.agent import Agent
from eudis_swarm.autonomy import ContactState, CoordinationMode, MachineName
from eudis_swarm.config import SimulationConfig
from eudis_swarm.peer_state import PeerStatus
from eudis_swarm.simulation import Simulation, SimulationResult
from eudis_swarm.task import Task
from eudis_swarm.trace import SimulationTrace


@pytest.fixture(scope="module")
def outage_result() -> Iterator[SimulationResult]:
    """Run one long-lived, physically healthy three-UAV outage scenario."""

    config = SimulationConfig(
        agent_count=3,
        task_count=1,
        area_width=100.0,
        area_height=100.0,
        agent_speed=0.1,
        time_step=0.25,
        completion_tolerance=0.01,
        heartbeat_interval=1.0,
        failure_timeout=2.0,
        peer_state_stale_after=1.0,
        claim_lease_duration=3.0,
        failure_agent_id=3,
        failure_time=100.0,
        max_simulation_time=11.0,
        communication_range=1_000.0,
        comm_fault_agent_id=2,
        comm_fault_start=1.0,
        comm_fault_end=7.0,
    )
    agents = [
        Agent(agent_id, (float(agent_id), 0.0), speed=0.1) for agent_id in range(1, 4)
    ]
    tasks = [Task(1, (100.0, 100.0))]

    yield Simulation(
        config,
        agents=agents,
        tasks=tasks,
        capture_trace=True,
    ).run()


def _agent_states(frame: object) -> dict[int, object]:
    return {agent.agent_id: agent for agent in frame.agents}  # type: ignore[attr-defined]


def test_healthy_outage_produces_receiver_local_contact_and_mode_divergence(
    outage_result: SimulationResult,
) -> None:
    kernels = outage_result.autonomy_kernels
    trace = outage_result.trace
    assert kernels is not None
    assert trace is not None
    assert set(kernels) == {1, 2, 3}
    assert all(kernel.owner_agent_id == owner for owner, kernel in kernels.items())

    divergent_frame = next(
        frame
        for frame in trace.frames
        if any(
            agent.agent_id == 2
            and agent.coordination_mode == CoordinationMode.LOCAL_AUTONOMY.value
            for agent in frame.agents
        )
    )
    agents = _agent_states(divergent_frame)

    # A communications inference is not a world-level crash declaration.
    assert all(agent.responsive for agent in agents.values())  # type: ignore[attr-defined]
    assert outage_result.mission.agents[2].responsive is True
    assert {
        agent_id: agent.coordination_mode  # type: ignore[attr-defined]
        for agent_id, agent in agents.items()
    } == {
        1: CoordinationMode.DEGRADED.value,
        2: CoordinationMode.LOCAL_AUTONOMY.value,
        3: CoordinationMode.DEGRADED.value,
    }

    contacts = {
        agent_id: {
            contact.peer_agent_id: contact.state
            for contact in agent.contact_states  # type: ignore[attr-defined]
        }
        for agent_id, agent in agents.items()
    }
    assert contacts[2] == {1: ContactState.LOST.value, 3: ContactState.LOST.value}
    assert contacts[1] == {2: ContactState.LOST.value, 3: ContactState.ACTIVE.value}
    assert contacts[3] == {1: ContactState.ACTIVE.value, 2: ContactState.LOST.value}


def test_restoration_records_lost_recovering_active_before_cooperation(
    outage_result: SimulationResult,
) -> None:
    kernels = outage_result.autonomy_kernels
    assert kernels is not None

    contact_changes = [
        record
        for record in kernels[1].transition_records
        if record.machine is MachineName.CONTACT and record.peer_agent_id == 2
    ]
    states = [record.next_state for record in contact_changes]
    lost_index = states.index(ContactState.LOST.value)
    assert states[lost_index : lost_index + 3] == [
        ContactState.LOST.value,
        ContactState.RECOVERING.value,
        ContactState.ACTIVE.value,
    ]
    assert [
        record.timestamp for record in contact_changes[lost_index : lost_index + 3]
    ] == pytest.approx([2.25, 7.0, 8.0])

    coordination_changes = [
        record.next_state
        for record in kernels[2].transition_records
        if record.machine is MachineName.COORDINATION_MODE
    ]
    assert CoordinationMode.LOCAL_AUTONOMY.value in coordination_changes
    local_index = coordination_changes.index(CoordinationMode.LOCAL_AUTONOMY.value)
    assert coordination_changes[local_index : local_index + 3] == [
        CoordinationMode.LOCAL_AUTONOMY.value,
        CoordinationMode.RECONCILING.value,
        CoordinationMode.COOPERATIVE.value,
    ]


def test_multihop_protocol_receipt_attributes_contact_to_immediate_forwarder() -> None:
    config = SimulationConfig(
        agent_count=3,
        task_count=1,
        area_width=10.0,
        area_height=10.0,
        agent_speed=1.0,
        time_step=1.0,
        completion_tolerance=0.0,
        heartbeat_interval=1.0,
        failure_timeout=2.0,
        peer_state_stale_after=1.0,
        claim_lease_duration=3.0,
        failure_agent_id=3,
        failure_time=100.0,
        max_simulation_time=1.0,
        communication_range=1.1,
    )
    simulation = Simulation(
        config,
        agents=[
            Agent(1, (0.0, 0.0), speed=1.0),
            Agent(2, (1.0, 0.0), speed=1.0),
            Agent(3, (2.0, 0.0), speed=1.0),
        ],
        tasks=[Task(1, (9.0, 9.0))],
    )
    simulation.communication_graph.update(
        {agent.agent_id: agent.position for agent in simulation.mission.ordered_agents}
    )
    claim = simulation.task_claim_stores[1].create_claim(1, 0.0)
    batch = simulation.task_claim_transport.deliver_claims((claim,), 0.0)
    final_hop = next(
        receipt for receipt in batch.receipts if receipt.receiver_agent_id == 3
    )
    assert (
        final_hop.source_agent_id,
        final_hop.forwarder_agent_id,
        final_hop.hop_count,
    ) == (1, 2, 2)

    simulation._record_protocol_delivery(0.0, batch)
    receiver = simulation.autonomy_kernels[3]
    assert receiver.contact_configuration_for(2).state is ContactState.ACTIVE
    assert receiver.contact_configuration_for(1).state is ContactState.UNKNOWN
    # Task gossip is contact evidence for the physical hop, not a heartbeat.
    assert receiver.peer_availability_for(2) is PeerStatus.SILENT
    assert receiver.peer_availability_for(1) is PeerStatus.SILENT


def test_normal_claim_flow_records_transient_contest_then_reconciliation(
    outage_result: SimulationResult,
) -> None:
    kernels = outage_result.autonomy_kernels
    trace = outage_result.trace
    assert kernels is not None
    assert trace is not None

    expected_reconciled_states = {
        1: "OWNED_BY_SELF",
        2: "CLAIMED_BY_PEER_FRESH",
        3: "CLAIMED_BY_PEER_FRESH",
    }
    for observer_agent_id, kernel in kernels.items():
        task_changes = [
            record
            for record in kernel.transition_records
            if record.machine is MachineName.TASK_OWNERSHIP and record.task_id == 1
        ]
        assert [
            (record.previous_state, record.next_state) for record in task_changes[:2]
        ] == [
            ("UNCLAIMED", "CONTESTED"),
            ("CONTESTED", expected_reconciled_states[observer_agent_id]),
        ]
        assert task_changes[0].effects == ("REQUEST_RECONCILIATION",)
        assert task_changes[0].timestamp == task_changes[1].timestamp == 0.0

    initial_frame = trace.frames[0]
    task_transitions = [
        transition
        for transition in initial_frame.transitions
        if transition.machine == MachineName.TASK_OWNERSHIP.value
    ]
    assert len(task_transitions) == 6
    assert (
        sum(transition.next_state == "CONTESTED" for transition in task_transitions)
        == 3
    )


def test_trace_json_round_trip_preserves_kernels_projection_and_transitions(
    outage_result: SimulationResult,
    tmp_path: Path,
) -> None:
    original = outage_result.trace
    assert original is not None

    destination = tmp_path / "autonomy-integration.trace.json"
    original.write_json(destination)
    restored = SimulationTrace.read_json(destination)

    assert restored == original
    assert any(frame.transitions for frame in restored.frames)
    assert all(
        len(agent.contact_states) == 2
        for frame in restored.frames
        for agent in frame.agents
    )
