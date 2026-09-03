"""Run a deterministic partition and reconciliation demonstration for task claims.

The scenario uses only receiver-local stores and graph-delivered immutable evidence,
so no authoritative mission object can arbitrate the ownership conflict.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .communication import CommunicationGraph
from .messaging import (
    TaskClaimTransport,
    TaskProtocolDeliveryBatch,
    TaskProtocolMessageKind,
)
from .simulation_events import TaskClaimEvent, TaskClaimEventKind
from .task import TaskOwnershipState
from .task_claim_trace import (
    TaskClaimTrace,
    TaskClaimTraceRecorder,
    canonical_claim_id,
)
from .task_claims import (
    TaskClaim,
    TaskClaimRelease,
    TaskClaimStore,
    TaskOwnershipView,
)

DEMO_AGENT_IDS = (1, 2, 3, 4)
DEMO_TASK_IDS = (7, 11, 19, 23, 29)
DEMO_CONTESTED_TASK_ID = 19
DEMO_CONTINUATION_TASK_ID = 29
DEMO_WINNER_AGENT_ID = 1
DEMO_LOSER_AGENT_ID = 4
DEMO_FRESHNESS_TIMEOUT = 1.0
DEMO_LEASE_TIMEOUT = 3.0
DEMO_POSITIONS = {
    1: (0.0, 0.0),
    2: (0.0, 1.0),
    3: (1.0, 0.0),
    4: (1.0, 1.0),
}
DEMO_CROSS_COMPONENT_LINKS = frozenset(
    {
        (1, 3),
        (1, 4),
        (2, 3),
        (2, 4),
    }
)


@dataclass(frozen=True, slots=True)
class TaskClaimDemoResult:
    """Expose final local replicas and the complete observer-only demonstration trace."""

    stores: dict[int, TaskClaimStore]
    graph: CommunicationGraph
    events: tuple[TaskClaimEvent, ...]
    trace: TaskClaimTrace
    contested_task_id: int
    winner_agent_id: int
    loser_agent_id: int
    continuation_task_id: int


def _active_link_keys(graph: CommunicationGraph) -> tuple[tuple[int, int], ...]:
    return tuple(
        (link.source_agent_id, link.destination_agent_id) for link in graph.active_links
    )


def _capture(
    recorder: TaskClaimTraceRecorder,
    graph: CommunicationGraph,
    events: list[TaskClaimEvent],
    timestamp: float,
    stage: str,
) -> None:
    recorder.capture(
        timestamp,
        stage,
        active_links=_active_link_keys(graph),
        components=graph.connected_components,
        events=events,
    )


def _claim_event(
    kind: TaskClaimEventKind,
    timestamp: float,
    observer_agent_id: int,
    claim: TaskClaim,
    *,
    source_agent_id: int | None = None,
    winner: TaskClaim | None = None,
) -> TaskClaimEvent:
    return TaskClaimEvent(
        kind=kind,
        timestamp=timestamp,
        observer_agent_id=observer_agent_id,
        task_id=claim.task_id,
        owner_agent_id=claim.owner_agent_id,
        source_agent_id=source_agent_id,
        claim_id=canonical_claim_id(claim.claim_id),
        epoch=claim.epoch,
        winner_agent_id=None if winner is None else winner.owner_agent_id,
        winner_claim_id=(
            None if winner is None else canonical_claim_id(winner.claim_id)
        ),
    )


def _append_receipt_events(
    events: list[TaskClaimEvent],
    batch: TaskProtocolDeliveryBatch,
    timestamp: float,
    *,
    release: TaskClaimRelease | None = None,
) -> None:
    kind_by_message = {
        TaskProtocolMessageKind.CLAIM: TaskClaimEventKind.CLAIM_RECEIVED,
        TaskProtocolMessageKind.RELEASE: TaskClaimEventKind.RELEASE_RECEIVED,
        TaskProtocolMessageKind.COMPLETION: TaskClaimEventKind.COMPLETION_RECEIVED,
    }
    for receipt in batch.receipts:
        winner = None if release is None else release.winning_claim
        events.append(
            TaskClaimEvent(
                kind=kind_by_message[receipt.kind],
                timestamp=timestamp,
                observer_agent_id=receipt.receiver_agent_id,
                task_id=receipt.task_id,
                owner_agent_id=receipt.claim_id[1],
                source_agent_id=receipt.source_agent_id,
                claim_id=canonical_claim_id(receipt.claim_id),
                epoch=receipt.epoch,
                winner_agent_id=(None if winner is None else winner.owner_agent_id),
                winner_claim_id=(
                    None if winner is None else canonical_claim_id(winner.claim_id)
                ),
            )
        )


def _selected_claim(view: TaskOwnershipView) -> TaskClaim | None:
    if view.claim_id is None:
        return None
    return next(
        observation.claim
        for observation in view.known_claim_observations
        if observation.claim.claim_id == view.claim_id
    )


def _advance_and_record_lease_transitions(
    stores: dict[int, TaskClaimStore],
    events: list[TaskClaimEvent],
    timestamp: float,
) -> None:
    for observer_agent_id, store in sorted(stores.items()):
        previous_timestamp = store.last_timestamp
        previous_views = {
            task_id: store.view(
                task_id,
                timestamp if previous_timestamp is None else previous_timestamp,
            )
            for task_id in store.task_ids
        }
        for task_id in store.advance_time(timestamp):
            previous_view = previous_views[task_id]
            current_view = store.view(task_id, timestamp)
            reference_claim = _selected_claim(current_view) or _selected_claim(
                previous_view
            )
            if reference_claim is None:
                continue
            kind = (
                TaskClaimEventKind.LEASE_EXPIRED
                if current_view.state is TaskOwnershipState.UNCLAIMED
                else TaskClaimEventKind.CLAIM_STALE
            )
            events.append(
                _claim_event(
                    kind,
                    timestamp,
                    observer_agent_id,
                    reference_claim,
                    source_agent_id=reference_claim.owner_agent_id,
                )
            )


def _publish_claims(
    transport: TaskClaimTransport,
    events: list[TaskClaimEvent],
    claims: Iterable[TaskClaim],
    timestamp: float,
) -> TaskProtocolDeliveryBatch:
    batch = transport.deliver_claims(tuple(claims), timestamp)
    _append_receipt_events(events, batch, timestamp)
    return batch


def run_task_claim_partition_demo() -> TaskClaimDemoResult:
    """Run the fixed 2+2 split-brain scenario and return its replayable evidence."""

    stores = {
        owner_agent_id: TaskClaimStore(
            owner_agent_id,
            DEMO_AGENT_IDS,
            DEMO_TASK_IDS,
            DEMO_LEASE_TIMEOUT,
            freshness_timeout=DEMO_FRESHNESS_TIMEOUT,
        )
        for owner_agent_id in DEMO_AGENT_IDS
    }
    graph = CommunicationGraph(DEMO_AGENT_IDS, communication_range=10.0)
    graph.update(DEMO_POSITIONS)
    transport = TaskClaimTransport(graph, stores)
    events: list[TaskClaimEvent] = []
    recorder = TaskClaimTraceRecorder("deterministic-2-plus-2-task-claims", stores)

    # the connected baseline publishes three independent local allocations.
    initial_claims = (
        stores[1].create_claim(DEMO_CONTESTED_TASK_ID, 0.0),
        stores[2].create_claim(7, 0.0),
        stores[3].create_claim(11, 0.0),
    )
    events.extend(
        _claim_event(
            TaskClaimEventKind.CLAIM_CREATED,
            0.0,
            claim.owner_agent_id,
            claim,
            source_agent_id=claim.owner_agent_id,
        )
        for claim in initial_claims
    )
    _publish_claims(transport, events, initial_claims, 0.1)
    _capture(recorder, graph, events, 0.2, "connected_initial_claims")

    # cutting only cross-component links preserves useful delivery inside both halves.
    graph.update(
        DEMO_POSITIONS,
        blocked_links=DEMO_CROSS_COMPONENT_LINKS,
    )
    _capture(recorder, graph, events, 0.6, "partitioned_two_plus_two")

    component_claims = (
        stores[1].renew_claim(DEMO_CONTESTED_TASK_ID, 0.8),
        stores[2].renew_claim(7, 0.8),
        stores[3].renew_claim(11, 0.8),
        stores[4].create_claim(23, 0.8),
    )
    for claim in component_claims:
        events.append(
            _claim_event(
                (
                    TaskClaimEventKind.CLAIM_CREATED
                    if claim.owner_agent_id == 4
                    else TaskClaimEventKind.CLAIM_RENEWED
                ),
                0.8,
                claim.owner_agent_id,
                claim,
                source_agent_id=claim.owner_agent_id,
            )
        )
    _publish_claims(transport, events, component_claims, 0.9)
    _advance_and_record_lease_transitions(stores, events, 1.2)
    for observer_agent_id in (3, 4):
        assert (
            stores[observer_agent_id].view(DEMO_CONTESTED_TASK_ID, 1.2).state
            is TaskOwnershipState.CLAIMED_BY_PEER_STALE
        )
    _capture(recorder, graph, events, 1.3, "right_view_stale_but_not_free")

    # explicit new epochs keep every live component owner current without shortcuts.
    first_component_renewals = (
        stores[1].renew_claim(DEMO_CONTESTED_TASK_ID, 2.0),
        stores[2].renew_claim(7, 2.0),
        stores[3].renew_claim(11, 2.0),
        stores[4].renew_claim(23, 2.0),
    )
    events.extend(
        _claim_event(
            TaskClaimEventKind.CLAIM_RENEWED,
            2.0,
            claim.owner_agent_id,
            claim,
            source_agent_id=claim.owner_agent_id,
        )
        for claim in first_component_renewals
    )
    _publish_claims(transport, events, first_component_renewals, 2.1)

    second_component_renewals = (
        stores[1].renew_claim(DEMO_CONTESTED_TASK_ID, 3.0),
        stores[2].renew_claim(7, 3.0),
        stores[3].renew_claim(11, 3.0),
        stores[4].renew_claim(23, 3.0),
    )
    events.extend(
        _claim_event(
            TaskClaimEventKind.CLAIM_RENEWED,
            3.0,
            claim.owner_agent_id,
            claim,
            source_agent_id=claim.owner_agent_id,
        )
        for claim in second_component_renewals
    )
    _publish_claims(transport, events, second_component_renewals, 3.05)
    _advance_and_record_lease_transitions(stores, events, 3.2)
    for observer_agent_id in (3, 4):
        assert (
            stores[observer_agent_id].view(DEMO_CONTESTED_TASK_ID, 3.2).state
            is TaskOwnershipState.UNCLAIMED
        )
        assert stores[observer_agent_id].can_create_claim(
            DEMO_CONTESTED_TASK_ID,
            3.2,
        )
    _capture(recorder, graph, events, 3.25, "right_view_lease_expired")

    # uav 4 now has protocol permission to replace the locally expired remote claim.
    right_claim = stores[4].create_claim(DEMO_CONTESTED_TASK_ID, 3.3)
    events.append(
        _claim_event(
            TaskClaimEventKind.CLAIM_CREATED,
            3.3,
            4,
            right_claim,
            source_agent_id=4,
        )
    )
    _publish_claims(transport, events, (right_claim,), 3.35)

    left_claim = stores[1].renew_claim(DEMO_CONTESTED_TASK_ID, 3.4)
    right_claim = stores[4].renew_claim(DEMO_CONTESTED_TASK_ID, 3.4)
    for claim in (left_claim, right_claim):
        events.append(
            _claim_event(
                TaskClaimEventKind.CLAIM_RENEWED,
                3.4,
                claim.owner_agent_id,
                claim,
                source_agent_id=claim.owner_agent_id,
            )
        )
    _publish_claims(transport, events, (left_claim, right_claim), 3.45)
    assert stores[1].owns_task(DEMO_CONTESTED_TASK_ID, 3.45)
    assert stores[4].owns_task(DEMO_CONTESTED_TASK_ID, 3.45)
    _capture(recorder, graph, events, 3.5, "partition_split_brain")

    graph.update(DEMO_POSITIONS)
    _capture(recorder, graph, events, 3.6, "network_reconnected")

    # delivery reveals both still-valid claims before any replica may reconcile.
    reconnect_batch = _publish_claims(
        transport,
        events,
        (left_claim, right_claim),
        3.7,
    )
    # Store-and-forward also drains immutable evidence queued by earlier epochs.
    # The deterministic demo therefore exposes 30 first deliveries on reconnect,
    # including the four missing copies of the two current contested claims.
    assert reconnect_batch.delivered == 30
    for observer_agent_id in DEMO_AGENT_IDS:
        view = stores[observer_agent_id].view(DEMO_CONTESTED_TASK_ID, 3.7)
        assert view.state is TaskOwnershipState.CONTESTED
        events.append(
            TaskClaimEvent(
                kind=TaskClaimEventKind.CONTESTED,
                timestamp=3.7,
                observer_agent_id=observer_agent_id,
                task_id=DEMO_CONTESTED_TASK_ID,
            )
        )
    _capture(recorder, graph, events, 3.8, "reconnected_conflict_visible")

    # every replica independently applies the same pure lower-owner decision.
    decisions = {}
    losing_release: TaskClaimRelease | None = None
    for observer_agent_id, store in sorted(stores.items()):
        decision = store.reconcile(DEMO_CONTESTED_TASK_ID, 4.0)
        assert decision is not None
        assert decision.winner.owner_agent_id == DEMO_WINNER_AGENT_ID
        decisions[observer_agent_id] = decision
        events.append(
            _claim_event(
                TaskClaimEventKind.RECONCILIATION_SELECTED,
                4.0,
                observer_agent_id,
                decision.winner,
                winner=decision.winner,
            )
        )
        if decision.local_release is not None:
            assert observer_agent_id == DEMO_LOSER_AGENT_ID
            losing_release = decision.local_release
            events.append(
                _claim_event(
                    TaskClaimEventKind.CLAIM_RELEASED,
                    4.0,
                    observer_agent_id,
                    losing_release.losing_claim,
                    source_agent_id=observer_agent_id,
                    winner=losing_release.winning_claim,
                )
            )
    assert set(decisions) == set(DEMO_AGENT_IDS)
    assert losing_release is not None
    assert not stores[DEMO_LOSER_AGENT_ID].owns_task(
        DEMO_CONTESTED_TASK_ID,
        4.0,
    )
    _capture(recorder, graph, events, 4.1, "deterministic_reconciliation")

    release_batch = transport.deliver_releases((losing_release,), 4.2)
    _append_receipt_events(
        events,
        release_batch,
        4.2,
        release=losing_release,
    )
    assert release_batch.delivered == 3
    for store in stores.values():
        view = store.view(DEMO_CONTESTED_TASK_ID, 4.2)
        assert view.known_owner_agent_id == DEMO_WINNER_AGENT_ID
        assert not view.contested
    _capture(recorder, graph, events, 4.3, "loser_release_propagated")

    # the former loser immediately returns to useful local work without a restart.
    continuation_claim = stores[DEMO_LOSER_AGENT_ID].create_claim(
        DEMO_CONTINUATION_TASK_ID,
        4.5,
    )
    events.append(
        _claim_event(
            TaskClaimEventKind.CLAIM_CREATED,
            4.5,
            DEMO_LOSER_AGENT_ID,
            continuation_claim,
            source_agent_id=DEMO_LOSER_AGENT_ID,
        )
    )
    continuation_batch = _publish_claims(
        transport,
        events,
        (continuation_claim,),
        4.55,
    )
    assert continuation_batch.delivered == 3
    assert stores[DEMO_LOSER_AGENT_ID].owns_task(
        DEMO_CONTINUATION_TASK_ID,
        4.55,
    )
    _capture(recorder, graph, events, 4.6, "mission_work_continues_after_release")

    return TaskClaimDemoResult(
        stores=stores,
        graph=graph,
        events=tuple(events),
        trace=recorder.finish(),
        contested_task_id=DEMO_CONTESTED_TASK_ID,
        winner_agent_id=DEMO_WINNER_AGENT_ID,
        loser_agent_id=DEMO_LOSER_AGENT_ID,
        continuation_task_id=DEMO_CONTINUATION_TASK_ID,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="run the deterministic 2+2 task-claim partition demo",
    )
    parser.add_argument(
        "--record-trace",
        type=Path,
        metavar="PATH",
        help="write the receiver-local claim trace as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the demonstration CLI and optionally persist its trace."""

    arguments = _parser().parse_args(argv)
    result = run_task_claim_partition_demo()
    if arguments.record_trace is not None:
        result.trace.write_json(arguments.record_trace)
    print(
        "Task-claim demo: 2+2 partition, "
        f"Task {result.contested_task_id} -> UAV {result.winner_agent_id}, "
        f"UAV {result.loser_agent_id} released and claimed "
        f"Task {result.continuation_task_id}; {len(result.trace.frames)} trace frames."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``.
    raise SystemExit(main())


__all__ = [
    "DEMO_AGENT_IDS",
    "DEMO_CONTESTED_TASK_ID",
    "DEMO_CONTINUATION_TASK_ID",
    "DEMO_LOSER_AGENT_ID",
    "DEMO_TASK_IDS",
    "DEMO_WINNER_AGENT_ID",
    "TaskClaimDemoResult",
    "main",
    "run_task_claim_partition_demo",
]
