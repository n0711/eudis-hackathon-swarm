"""Focused tests for receiver-local task ownership classification.

They prove the vocabulary and its dependence on delivered peer evidence.
"""

from eudis_swarm.agent import Agent
from eudis_swarm.config import SimulationConfig
from eudis_swarm.mission import MissionEventKind
from eudis_swarm.peer_state import PeerStateStore
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import TaskOwnershipState, classify_task_ownership


def _store(owner_agent_id: int, peer_agent_ids: tuple[int, ...]) -> PeerStateStore:
    return PeerStateStore(owner_agent_id, peer_agent_ids, stale_after=2.5)


def test_task_ownership_vocabulary_is_exact() -> None:
    assert [state.value for state in TaskOwnershipState] == [
        "UNCLAIMED",
        "OWNED_BY_SELF",
        "CLAIMED_BY_PEER_FRESH",
        "CLAIMED_BY_PEER_STALE",
        "CONTESTED",
        "COMPLETE",
    ]


def test_local_self_and_completion_evidence_are_classified() -> None:
    store = _store(1, (2,))

    assert (
        classify_task_ownership(
            7,
            own_current_task=None,
            peer_state_store=store,
        )
        is TaskOwnershipState.UNCLAIMED
    )
    assert (
        classify_task_ownership(
            7,
            own_current_task=7,
            peer_state_store=store,
        )
        is TaskOwnershipState.OWNED_BY_SELF
    )
    assert (
        classify_task_ownership(
            7,
            own_current_task=7,
            peer_state_store=store,
            known_completed_task_ids={7},
        )
        is TaskOwnershipState.COMPLETE
    )


def test_peer_claim_uses_delivered_snapshot_and_ages_from_fresh_to_stale() -> None:
    source = Agent(agent_id=2, position=(10.0, 0.0), speed=1.0)
    source.assign_task(7)
    snapshot = source.send_heartbeat(0.0)
    assert snapshot is not None
    store = _store(1, (2,))
    store.receive(snapshot, 0.0)

    source.release_task(7)
    assert source.current_task is None
    assert (
        classify_task_ownership(
            7,
            own_current_task=None,
            peer_state_store=store,
        )
        is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    )

    store.advance_time(2.5001)
    assert (
        classify_task_ownership(
            7,
            own_current_task=None,
            peer_state_store=store,
        )
        is TaskOwnershipState.CLAIMED_BY_PEER_STALE
    )


def test_multiple_local_claimants_are_contested() -> None:
    peer_two = Agent(agent_id=2, position=(10.0, 0.0), speed=1.0)
    peer_three = Agent(agent_id=3, position=(20.0, 0.0), speed=1.0)
    peer_two.assign_task(7)
    peer_three.assign_task(7)
    snapshot_two = peer_two.send_heartbeat(0.0)
    snapshot_three = peer_three.send_heartbeat(0.0)
    assert snapshot_two is not None
    assert snapshot_three is not None
    store = _store(1, (2, 3))
    store.receive(snapshot_two, 0.0)
    store.receive(snapshot_three, 0.0)

    assert (
        classify_task_ownership(
            7,
            own_current_task=None,
            peer_state_store=store,
        )
        is TaskOwnershipState.CONTESTED
    )

    single_peer_store = _store(1, (2,))
    single_peer_store.receive(snapshot_two, 0.0)
    assert (
        classify_task_ownership(
            7,
            own_current_task=7,
            peer_state_store=single_peer_store,
        )
        is TaskOwnershipState.CONTESTED
    )


def test_leases_stop_the_coordinator_handing_out_work_it_does_not_own() -> None:
    """Mission is no longer the single writer of task ownership.

    A partitioned UAV keeps a live lease on its task.  The coordinator cannot
    see that claim, wants to reassign the work, and is refused until the lease
    lapses.  When the partition heals the competing claims meet and the loser
    yields, so ownership converges without anybody being overruled centrally.
    """

    result = Simulation(
        SimulationConfig(
            failure_time=100.0,
            communication_range=130.0,
            comm_fault_agent_id=2,
            comm_fault_start=4.0,
            comm_fault_end=8.0,
        )
    ).run()
    metrics = result.metrics

    # allocation proposals were actually blocked by leases held elsewhere.
    assert metrics.claim_refused_allocation_count > 0

    # reconnection surfaced a genuine contest, and it was resolved by release.
    assert metrics.contested_task_yield_count == 1
    yielded = [
        event
        for event in result.mission.events
        if event.kind is MissionEventKind.TASK_YIELDED
    ]
    assert len(yielded) == 1

    # no contest survives reconciliation anywhere in the swarm.  (Evidence for
    # the very last completion has not been broadcast when the mission ends,
    # so replicas may still lag on that one task -- but none of them disagree
    # about who owns anything.)
    assert result.task_claim_stores is not None
    end_time = metrics.end_time
    assert end_time is not None
    for task_id in result.mission.tasks:
        for store in result.task_claim_stores.values():
            view = store.view(task_id, end_time)
            assert not view.contested, (
                f"UAV {store.owner_agent_id} still contests Task {task_id}"
            )

    assert metrics.mission_completed is True
    assert metrics.completed_task_count == 20
