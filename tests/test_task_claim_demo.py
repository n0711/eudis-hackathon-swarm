"""Verify the deterministic split-brain demonstration and its local-belief trace.

The tests keep authoritative world state absent while proving every replay stage.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

import pytest

from eudis_swarm.simulation_events import TaskClaimEvent, TaskClaimEventKind
from eudis_swarm.task_claim_demo import (
    DEMO_AGENT_IDS,
    DEMO_CONTESTED_TASK_ID,
    DEMO_CONTINUATION_TASK_ID,
    DEMO_TASK_IDS,
    TaskClaimDemoResult,
    main,
    run_task_claim_partition_demo,
)
from eudis_swarm.task_claim_trace import (
    TaskClaimTrace,
    TaskClaimTraceFrame,
    TaskClaimTraceRecorder,
    TaskClaimTraceTaskView,
    canonical_claim_id,
)
from eudis_swarm.task_claims import TaskClaimStore

EXPECTED_STAGES = (
    (0.2, "connected_initial_claims"),
    (0.6, "partitioned_two_plus_two"),
    (1.3, "right_view_stale_but_not_free"),
    (3.25, "right_view_lease_expired"),
    (3.5, "partition_split_brain"),
    (3.6, "network_reconnected"),
    (3.8, "reconnected_conflict_visible"),
    (4.1, "deterministic_reconciliation"),
    (4.3, "loser_release_propagated"),
    (4.6, "mission_work_continues_after_release"),
)


@pytest.fixture(scope="module")
def demo() -> TaskClaimDemoResult:
    return run_task_claim_partition_demo()


def _frame(demo: TaskClaimDemoResult, stage: str) -> TaskClaimTraceFrame:
    return next(frame for frame in demo.trace.frames if frame.stage == stage)


def _view(
    frame: TaskClaimTraceFrame,
    agent_id: int,
    task_id: int = DEMO_CONTESTED_TASK_ID,
) -> TaskClaimTraceTaskView:
    agent = next(item for item in frame.agents if item.agent_id == agent_id)
    return next(item for item in agent.task_views if item.task_id == task_id)


def _claim(view: TaskClaimTraceTaskView, claim_id: str):
    return next(item for item in view.known_claims if item.claim_id == claim_id)


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key for nested in value.values() for key in _all_keys(nested)
        }
    if isinstance(value, (list, tuple)):
        return {key for nested in value for key in _all_keys(nested)}
    return set()


def test_demo_trace_has_exact_stages_and_complete_local_matrix(
    demo: TaskClaimDemoResult,
) -> None:
    trace = demo.trace

    assert tuple((frame.timestamp, frame.stage) for frame in trace.frames) == (
        EXPECTED_STAGES
    )
    assert all(
        later.timestamp > earlier.timestamp
        for earlier, later in zip(trace.frames, trace.frames[1:], strict=False)
    )
    assert trace.metadata.scenario == "deterministic-2-plus-2-task-claims"
    assert trace.metadata.agent_ids == DEMO_AGENT_IDS
    assert trace.metadata.task_ids == DEMO_TASK_IDS
    assert trace.metadata.freshness_timeout == 1.0
    assert trace.metadata.lease_timeout == 3.0

    for frame in trace.frames:
        assert tuple(agent.agent_id for agent in frame.agents) == DEMO_AGENT_IDS
        assert all(
            tuple(view.task_id for view in agent.task_views) == DEMO_TASK_IDS
            for agent in frame.agents
        )

    # this standalone artifact contains local evidence and topology, never world truth.
    keys = _all_keys(trace.to_dict())
    assert keys.isdisjoint(
        {
            "assigned_agent_id",
            "current_task",
            "physical_state",
            "position",
            "responsive",
            "world_state",
        }
    )


def test_connected_partition_stale_and_expiry_stages(
    demo: TaskClaimDemoResult,
) -> None:
    connected = _frame(demo, "connected_initial_claims")
    assert connected.components == ((1, 2, 3, 4),)
    assert len(connected.active_links) == 6
    assert [_view(connected, agent_id).state for agent_id in DEMO_AGENT_IDS] == [
        "OWNED_BY_SELF",
        "CLAIMED_BY_PEER_FRESH",
        "CLAIMED_BY_PEER_FRESH",
        "CLAIMED_BY_PEER_FRESH",
    ]
    assert _view(connected, 1).claim_id == "19:1:1"

    partitioned = _frame(demo, "partitioned_two_plus_two")
    assert partitioned.components == ((1, 2), (3, 4))
    assert {
        (link.source_agent_id, link.destination_agent_id)
        for link in partitioned.active_links
    } == {(1, 2), (3, 4)}
    assert partitioned.events == ()

    stale = _frame(demo, "right_view_stale_but_not_free")
    for agent_id in (3, 4):
        view = _view(stale, agent_id)
        evidence = _claim(view, "19:1:1")
        assert view.state == "CLAIMED_BY_PEER_STALE"
        assert view.known_owner_agent_id == 1
        assert view.claim_freshness == "STALE"
        assert evidence.current is True
        assert evidence.valid is True
        assert evidence.released is False

    expired = _frame(demo, "right_view_lease_expired")
    for agent_id in (3, 4):
        view = _view(expired, agent_id)
        evidence = _claim(view, "19:1:1")
        assert view.state == "UNCLAIMED"
        assert view.known_owner_agent_id is None
        assert view.claim_id is None
        assert evidence.freshness == "EXPIRED"
        assert evidence.age > demo.trace.metadata.lease_timeout
        assert evidence.current is True
        assert evidence.valid is False


def test_split_brain_reconnection_and_visible_contest(
    demo: TaskClaimDemoResult,
) -> None:
    split = _frame(demo, "partition_split_brain")
    assert split.components == ((1, 2), (3, 4))
    assert [
        _view(split, agent_id).known_owner_agent_id for agent_id in DEMO_AGENT_IDS
    ] == [
        1,
        1,
        4,
        4,
    ]
    assert _view(split, 1).state == "OWNED_BY_SELF"
    assert _view(split, 4).state == "OWNED_BY_SELF"
    assert _view(split, 1).claim_id == "19:1:5"
    assert _view(split, 4).claim_id == "19:4:2"

    reconnected = _frame(demo, "network_reconnected")
    assert reconnected.components == ((1, 2, 3, 4),)
    assert len(reconnected.active_links) == 6
    assert [_view(reconnected, agent_id).state for agent_id in DEMO_AGENT_IDS] == [
        "OWNED_BY_SELF",
        "CLAIMED_BY_PEER_FRESH",
        "CLAIMED_BY_PEER_FRESH",
        "OWNED_BY_SELF",
    ]
    assert [
        _view(reconnected, agent_id).known_owner_agent_id for agent_id in DEMO_AGENT_IDS
    ] == [
        1,
        1,
        4,
        4,
    ]

    contested = _frame(demo, "reconnected_conflict_visible")
    assert all(
        _view(contested, agent_id).state == "CONTESTED" for agent_id in DEMO_AGENT_IDS
    )
    assert all(_view(contested, agent_id).contested for agent_id in DEMO_AGENT_IDS)
    for agent_id in DEMO_AGENT_IDS:
        view = _view(contested, agent_id)
        assert view.known_owner_agent_id is None
        assert view.claim_id is None
        assert {
            claim.owner_agent_id
            for claim in view.known_claims
            if claim.current and claim.valid
        } == {1, 4}
    assert [
        event.observer_agent_id
        for event in contested.events
        if event.kind is TaskClaimEventKind.CONTESTED
    ] == [1, 2, 3, 4]


def test_reconciliation_release_and_continuation(
    demo: TaskClaimDemoResult,
) -> None:
    reconciled = _frame(demo, "deterministic_reconciliation")
    assert [_view(reconciled, agent_id).state for agent_id in DEMO_AGENT_IDS] == [
        "OWNED_BY_SELF",
        "CLAIMED_BY_PEER_FRESH",
        "CLAIMED_BY_PEER_FRESH",
        "CLAIMED_BY_PEER_FRESH",
    ]
    for agent_id in DEMO_AGENT_IDS:
        view = _view(reconciled, agent_id)
        assert view.known_owner_agent_id == 1
        assert view.claim_id == "19:1:5"
        assert view.epoch == 5
        assert view.reconciliation_winner_agent_id == 1
        assert view.reconciliation_winner_claim_id == "19:1:5"
        assert view.contested is False
    loser_view = _view(reconciled, 4)
    assert loser_view.released_claim_ids == ("19:4:2",)
    assert _claim(loser_view, "19:4:2").released is True
    assert not demo.stores[4].owns_task(DEMO_CONTESTED_TASK_ID, 4.6)
    assert (
        sum(
            event.kind is TaskClaimEventKind.CLAIM_RELEASED
            and event.observer_agent_id == 4
            and event.claim_id == "19:4:2"
            and event.winner_claim_id == "19:1:5"
            for event in reconciled.events
        )
        == 1
    )

    propagated = _frame(demo, "loser_release_propagated")
    for agent_id in DEMO_AGENT_IDS:
        view = _view(propagated, agent_id)
        tombstone = _claim(view, "19:4:2")
        assert view.released is True
        assert view.released_claim_ids == ("19:4:2",)
        assert view.known_owner_agent_id == 1
        assert tombstone.current is True
        assert tombstone.released is True
        assert tombstone.valid is False
    assert {
        event.observer_agent_id
        for event in propagated.events
        if event.kind is TaskClaimEventKind.RELEASE_RECEIVED
    } == {1, 2, 3}

    continued = _frame(demo, "mission_work_continues_after_release")
    continuation_views = {
        agent_id: _view(continued, agent_id, DEMO_CONTINUATION_TASK_ID)
        for agent_id in DEMO_AGENT_IDS
    }
    assert continuation_views[4].state == "OWNED_BY_SELF"
    assert all(
        continuation_views[agent_id].state == "CLAIMED_BY_PEER_FRESH"
        for agent_id in (1, 2, 3)
    )
    assert all(view.known_owner_agent_id == 4 for view in continuation_views.values())
    assert all(view.claim_id == "29:4:1" for view in continuation_views.values())


def test_claim_ages_freshness_and_event_identities_are_replayable(
    demo: TaskClaimDemoResult,
) -> None:
    all_trace_events = []
    previous_timestamp: float | None = None
    for frame in demo.trace.frames:
        for agent in frame.agents:
            for view in agent.task_views:
                for claim in view.known_claims:
                    assert claim.age == pytest.approx(
                        frame.timestamp - claim.received_at
                    )
                    expected_freshness = (
                        "FRESH"
                        if claim.age <= demo.trace.metadata.freshness_timeout
                        else "STALE"
                        if claim.age <= demo.trace.metadata.lease_timeout
                        else "EXPIRED"
                    )
                    assert claim.freshness == expected_freshness
                    assert claim.valid is (
                        claim.current
                        and not claim.released
                        and not claim.reconciled_loser
                        and claim.freshness != "EXPIRED"
                    )
        for event in frame.events:
            assert event.timestamp <= frame.timestamp
            if previous_timestamp is not None:
                assert event.timestamp > previous_timestamp
            if event.claim_id is not None:
                task_id, owner_id, epoch = map(int, event.claim_id.split(":"))
                assert task_id == event.task_id
                if event.owner_agent_id is not None:
                    assert owner_id == event.owner_agent_id
                if event.epoch is not None:
                    assert epoch == event.epoch
            if event.winner_claim_id is not None:
                task_id, owner_id, _ = map(int, event.winner_claim_id.split(":"))
                assert task_id == event.task_id
                assert owner_id == event.winner_agent_id
        all_trace_events.extend(frame.events)
        previous_timestamp = frame.timestamp

    assert Counter(all_trace_events) == Counter(demo.events)


def test_trace_round_trip_and_cli_artifact(
    demo: TaskClaimDemoResult,
    tmp_path,
    capsys,
) -> None:
    destination = tmp_path / "claim-demo.trace.json"
    demo.trace.write_json(destination)
    assert TaskClaimTrace.read_json(destination) == demo.trace
    assert TaskClaimTrace.from_dict(demo.trace.to_dict()) == demo.trace

    cli_destination = tmp_path / "claim-demo.cli.json"
    assert main(["--record-trace", str(cli_destination)]) == 0
    cli_trace = TaskClaimTrace.read_json(cli_destination)
    assert tuple((frame.timestamp, frame.stage) for frame in cli_trace.frames) == (
        EXPECTED_STAGES
    )
    assert "Task 19 -> UAV 1" in capsys.readouterr().out


def test_trace_recording_is_observer_pure(demo: TaskClaimDemoResult) -> None:
    stores = demo.stores
    before_timestamps = {
        agent_id: store.last_timestamp for agent_id, store in stores.items()
    }
    before_snapshots = {
        agent_id: store.snapshot(4.6) for agent_id, store in stores.items()
    }
    recorder = TaskClaimTraceRecorder("observer-purity", stores)
    recorder.capture(
        4.7,
        "observer_only",
        active_links=tuple(
            (link.source_agent_id, link.destination_agent_id)
            for link in demo.graph.active_links
        ),
        components=demo.graph.connected_components,
        events=demo.events,
    )

    assert recorder.finish().frames[0].stage == "observer_only"
    assert {agent_id: store.last_timestamp for agent_id, store in stores.items()} == (
        before_timestamps
    )
    assert {
        agent_id: store.snapshot(4.6) for agent_id, store in stores.items()
    } == before_snapshots


def test_completion_trace_is_explicit_and_terminal() -> None:
    stores = {
        agent_id: TaskClaimStore(
            agent_id,
            (1, 2),
            (5,),
            3.0,
            freshness_timeout=1.0,
        )
        for agent_id in (1, 2)
    }
    claim = stores[1].create_claim(5, 0.0)
    stores[2].receive_claim(claim, 0.1)
    completion = stores[1].create_completion(5, 0.2)
    stores[2].receive_completion(completion, 0.3)
    claim_id = canonical_claim_id(claim.claim_id)
    events = [
        TaskClaimEvent(
            TaskClaimEventKind.CLAIM_CREATED,
            0.0,
            1,
            5,
            owner_agent_id=1,
            source_agent_id=1,
            claim_id=claim_id,
            epoch=1,
        ),
        TaskClaimEvent(
            TaskClaimEventKind.COMPLETION_CREATED,
            0.2,
            1,
            5,
            owner_agent_id=1,
            source_agent_id=1,
            claim_id=claim_id,
            epoch=1,
        ),
        TaskClaimEvent(
            TaskClaimEventKind.COMPLETION_RECEIVED,
            0.3,
            2,
            5,
            owner_agent_id=1,
            source_agent_id=1,
            claim_id=claim_id,
            epoch=1,
        ),
    ]
    recorder = TaskClaimTraceRecorder("completion", stores)
    recorder.capture(
        0.4,
        "completion_propagated",
        active_links=((2, 1),),
        components=((2, 1),),
        events=events,
    )
    trace = recorder.finish()

    assert trace.frames[0].active_links[0].source_agent_id == 1
    assert trace.frames[0].components == ((1, 2),)
    for agent_id in (1, 2):
        view = _view(trace.frames[0], agent_id, 5)
        assert view.state == "COMPLETE"
        assert view.complete is True
        assert view.completion is not None
        assert view.completion.claim_id == claim_id
        assert view.completion.owner_agent_id == 1
        assert view.completion.epoch == 1
        assert view.completion.created_at == 0.2


def test_trace_rejects_meaningful_structural_corruption(
    demo: TaskClaimDemoResult,
    tmp_path,
) -> None:
    document = demo.trace.to_dict()

    bad_version = deepcopy(document)
    bad_version["schema_version"] = 99
    with pytest.raises(ValueError, match="unsupported.*schema"):
        TaskClaimTrace.from_dict(bad_version)

    wrong_version_type = deepcopy(document)
    wrong_version_type["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version must be an integer"):
        TaskClaimTrace.from_dict(wrong_version_type)

    empty = deepcopy(document)
    empty["frames"] = []
    with pytest.raises(ValueError, match="at least one frame"):
        TaskClaimTrace.from_dict(empty)

    repeated_time = deepcopy(document)
    repeated_time["frames"][1]["timestamp"] = repeated_time["frames"][0]["timestamp"]
    with pytest.raises(ValueError, match="strictly increasing"):
        TaskClaimTrace.from_dict(repeated_time)

    missing_receiver = deepcopy(document)
    missing_receiver["frames"][0]["agents"] = missing_receiver["frames"][0]["agents"][
        :-1
    ]
    with pytest.raises(ValueError, match="agent views must match"):
        TaskClaimTrace.from_dict(missing_receiver)

    wrong_age = deepcopy(document)
    wrong_age["frames"][0]["agents"][0]["task_views"][2]["known_claims"][0]["age"] += (
        1.0
    )
    with pytest.raises(ValueError, match="age is invalid"):
        TaskClaimTrace.from_dict(wrong_age)

    invalid_state = deepcopy(document)
    invalid_state["frames"][0]["agents"][0]["task_views"][0]["state"] = "ASSIGNED"
    with pytest.raises(ValueError, match="six-state vocabulary"):
        TaskClaimTrace.from_dict(invalid_state)

    unsorted_events = deepcopy(document)
    unsorted_events["frames"][0]["events"] = tuple(
        reversed(unsorted_events["frames"][0]["events"])
    )
    with pytest.raises(ValueError, match="events must use deterministic ordering"):
        TaskClaimTrace.from_dict(unsorted_events)

    unknown_event_agent = deepcopy(document)
    unknown_event_agent["frames"][8]["events"][-1]["observer_agent_id"] = 99
    with pytest.raises(ValueError, match="unknown membership"):
        TaskClaimTrace.from_dict(unknown_event_agent)

    invalid_root = tmp_path / "invalid-root.json"
    invalid_root.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be an object"):
        TaskClaimTrace.read_json(invalid_root)


def test_recorder_and_claim_id_validation_is_fail_fast() -> None:
    with pytest.raises(ValueError, match="requires local stores"):
        TaskClaimTraceRecorder("empty", {})
    with pytest.raises(ValueError, match="greater than zero"):
        canonical_claim_id((1, 2, 0))

    store = TaskClaimStore(1, (1,), (5,), 3.0, freshness_timeout=1.0)
    with pytest.raises(ValueError, match="homogeneous keyed"):
        TaskClaimTraceRecorder("wrong-key", {2: store})

    recorder = TaskClaimTraceRecorder("validation", {1: store})
    with pytest.raises(ValueError, match="at least one frame"):
        recorder.finish()
    with pytest.raises(ValueError, match="different known UAVs"):
        recorder.capture(
            0.0,
            "bad_link",
            active_links=((1, 1),),
            components=((1,),),
            events=(),
        )

    event = TaskClaimEvent(TaskClaimEventKind.CONTESTED, 0.0, 1, 5)
    recorder.capture(
        0.1,
        "first",
        active_links=(),
        components=((1,),),
        events=(event,),
    )
    with pytest.raises(ValueError, match="strictly increase"):
        recorder.capture(
            0.1,
            "duplicate_time",
            active_links=(),
            components=((1,),),
            events=(event,),
        )
    with pytest.raises(ValueError, match="must not shrink"):
        recorder.capture(
            0.2,
            "shrunk_events",
            active_links=(),
            components=((1,),),
            events=(),
        )
