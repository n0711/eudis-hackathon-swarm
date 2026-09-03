"""Exercise the pure, receiver-local autonomy EFSM transition relations."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable, Iterator, Sequence
from itertools import product
from pathlib import Path
from typing import Any

import pytest

import eudis_swarm.autonomy as autonomy
from eudis_swarm.autonomy import (
    AutonomyEffectKind,
    ContactConfiguration,
    ContactEvent,
    ContactEventKind,
    ContactPolicy,
    ContactState,
    CoordinationConfiguration,
    CoordinationEventKind,
    CoordinationEvidenceEvent,
    CoordinationMode,
    CoordinationPolicy,
    MachineName,
    PeerAvailabilityConfiguration,
    PeerAvailabilityEvent,
    PeerAvailabilityEventKind,
    TaskOwnershipConfiguration,
    TaskOwnershipEvent,
    TaskOwnershipEventKind,
    initial_contact_configuration,
    initial_coordination_configuration,
    initial_peer_availability_configuration,
    initial_task_ownership_configuration,
    machine_definitions,
    render_transition_tables,
    task_ownership_event_from_view,
    transition_contact,
    transition_coordination_mode,
    transition_peer_availability,
    transition_task_ownership,
)
from eudis_swarm.peer_state import PeerStatus
from eudis_swarm.task import TaskOwnershipState
from eudis_swarm.task_claims import ClaimFreshness, TaskOwnershipView

CONTACT_POLICY = ContactPolicy(
    expected_interval=1.0,
    degraded_after=2.0,
    lost_after=4.0,
    recovery_receipts=2,
)
COORDINATION_POLICY = CoordinationPolicy(
    degradation_grace=1.0,
    local_autonomy_grace=1.0,
    recovery_stable_for=1.0,
)
OBSERVER_AGENT_ID = 7
PEER_AGENT_ID = 2
TASK_ID = 11


def _contact_step(
    configuration: ContactConfiguration,
    kind: ContactEventKind,
    timestamp: float,
) -> ContactConfiguration:
    result = transition_contact(
        configuration,
        ContactEvent(kind, timestamp, PEER_AGENT_ID),
        CONTACT_POLICY,
    )
    return ContactConfiguration(result.next_state, result.next_variables)


def _peer_step(
    configuration: PeerAvailabilityConfiguration,
    kind: PeerAvailabilityEventKind,
    timestamp: float,
) -> PeerAvailabilityConfiguration:
    result = transition_peer_availability(
        configuration,
        PeerAvailabilityEvent(kind, timestamp, PEER_AGENT_ID),
    )
    return PeerAvailabilityConfiguration(result.next_state, result.next_variables)


def _task_event(kind: TaskOwnershipEventKind, timestamp: float) -> TaskOwnershipEvent:
    if kind is TaskOwnershipEventKind.LOCAL_VALID_CLAIM:
        return TaskOwnershipEvent(
            kind,
            timestamp,
            TASK_ID,
            known_owner_agent_id=OBSERVER_AGENT_ID,
            claim_id=(TASK_ID, OBSERVER_AGENT_ID, 1),
            claim_freshness=ClaimFreshness.FRESH,
        )
    if kind in {
        TaskOwnershipEventKind.FRESH_PEER_VALID_CLAIM,
        TaskOwnershipEventKind.STALE_PEER_VALID_CLAIM,
    }:
        freshness = (
            ClaimFreshness.FRESH
            if kind is TaskOwnershipEventKind.FRESH_PEER_VALID_CLAIM
            else ClaimFreshness.STALE
        )
        return TaskOwnershipEvent(
            kind,
            timestamp,
            TASK_ID,
            known_owner_agent_id=PEER_AGENT_ID,
            claim_id=(TASK_ID, PEER_AGENT_ID, 1),
            claim_freshness=freshness,
        )
    if kind is TaskOwnershipEventKind.COMPLETION_EVIDENCE:
        return TaskOwnershipEvent(
            kind,
            timestamp,
            TASK_ID,
            known_owner_agent_id=PEER_AGENT_ID,
            claim_id=(TASK_ID, PEER_AGENT_ID, 1),
            claim_freshness=ClaimFreshness.FRESH,
        )
    return TaskOwnershipEvent(kind, timestamp, TASK_ID)


def _task_step(
    configuration: TaskOwnershipConfiguration,
    kind: TaskOwnershipEventKind,
    timestamp: float,
) -> TaskOwnershipConfiguration:
    result = transition_task_ownership(
        configuration,
        _task_event(kind, timestamp),
        OBSERVER_AGENT_ID,
    )
    return TaskOwnershipConfiguration(result.next_state, result.next_variables)


def _task_view(state: TaskOwnershipState) -> TaskOwnershipView:
    self_owned = state is TaskOwnershipState.OWNED_BY_SELF
    peer_owned = state in {
        TaskOwnershipState.CLAIMED_BY_PEER_FRESH,
        TaskOwnershipState.CLAIMED_BY_PEER_STALE,
        TaskOwnershipState.COMPLETE,
    }
    owner_agent_id = (
        OBSERVER_AGENT_ID if self_owned else PEER_AGENT_ID if peer_owned else None
    )
    claim_id = None if owner_agent_id is None else (TASK_ID, owner_agent_id, 1)
    freshness = (
        ClaimFreshness.STALE
        if state is TaskOwnershipState.CLAIMED_BY_PEER_STALE
        else ClaimFreshness.FRESH
        if owner_agent_id is not None
        else None
    )
    return TaskOwnershipView(
        task_id=TASK_ID,
        state=state,
        known_owner_agent_id=owner_agent_id,
        claim_id=claim_id,
        epoch=None if claim_id is None else claim_id[2],
        claim_age=None if claim_id is None else 0.0,
        claim_freshness=freshness,
        contested=state is TaskOwnershipState.CONTESTED,
        reconciliation_winner_agent_id=None,
        reconciliation_winner_claim_id=None,
        released=False,
        released_claim_ids=(),
        completion=None,
        known_claim_observations=(),
    )


def _coordination_event(
    timestamp: float,
    contact_states: tuple[ContactState, ...],
    *,
    contests: int = 0,
    new_remote_evidence: bool = False,
) -> CoordinationEvidenceEvent:
    return CoordinationEvidenceEvent(
        CoordinationEventKind.LOCAL_EVIDENCE_UPDATED,
        timestamp,
        contact_states,
        contests,
        new_remote_evidence,
    )


def _coordination_step(
    configuration: CoordinationConfiguration,
    event: CoordinationEvidenceEvent,
) -> CoordinationConfiguration:
    result = transition_coordination_mode(
        configuration,
        event,
        COORDINATION_POLICY,
    )
    return CoordinationConfiguration(result.next_state, result.next_variables)


@pytest.mark.parametrize(
    ("transition", "configuration", "event", "extra_arguments"),
    [
        (
            transition_contact,
            initial_contact_configuration(),
            ContactEvent(
                ContactEventKind.PEER_EVIDENCE_RECEIVED,
                1.0,
                PEER_AGENT_ID,
            ),
            (CONTACT_POLICY,),
        ),
        (
            transition_peer_availability,
            initial_peer_availability_configuration(),
            PeerAvailabilityEvent(
                PeerAvailabilityEventKind.OBSERVATION_RECEIVED,
                1.0,
                PEER_AGENT_ID,
            ),
            (),
        ),
        (
            transition_task_ownership,
            initial_task_ownership_configuration(),
            _task_event(TaskOwnershipEventKind.LOCAL_VALID_CLAIM, 1.0),
            (OBSERVER_AGENT_ID,),
        ),
        (
            transition_coordination_mode,
            initial_coordination_configuration(),
            _coordination_event(1.0, (ContactState.ACTIVE,)),
            (COORDINATION_POLICY,),
        ),
    ],
)
def test_transition_relations_are_deterministic_and_do_not_mutate_inputs(
    transition: Callable[..., object],
    configuration: object,
    event: object,
    extra_arguments: tuple[object, ...],
) -> None:
    before = configuration

    first = transition(configuration, event, *extra_arguments)
    second = transition(configuration, event, *extra_arguments)

    assert first == second
    assert configuration == before


def test_contact_thresholds_are_strict_and_every_state_is_reachable() -> None:
    initial = initial_contact_configuration()
    assert initial.state is ContactState.UNKNOWN

    active = _contact_step(
        initial,
        ContactEventKind.PEER_EVIDENCE_RECEIVED,
        0.0,
    )
    assert active.state is ContactState.ACTIVE

    at_degraded_boundary = _contact_step(
        active,
        ContactEventKind.LOCAL_TIME_ADVANCED,
        CONTACT_POLICY.degraded_after,
    )
    assert at_degraded_boundary.state is ContactState.ACTIVE

    degraded = _contact_step(
        active,
        ContactEventKind.LOCAL_TIME_ADVANCED,
        CONTACT_POLICY.degraded_after + 0.0001,
    )
    assert degraded.state is ContactState.DEGRADED

    at_lost_boundary = _contact_step(
        active,
        ContactEventKind.LOCAL_TIME_ADVANCED,
        CONTACT_POLICY.lost_after,
    )
    assert at_lost_boundary.state is ContactState.DEGRADED

    lost = _contact_step(
        degraded,
        ContactEventKind.LOCAL_TIME_ADVANCED,
        CONTACT_POLICY.lost_after + 0.0001,
    )
    assert lost.state is ContactState.LOST

    recovering = _contact_step(
        lost,
        ContactEventKind.PEER_EVIDENCE_RECEIVED,
        5.0,
    )
    assert recovering.state is ContactState.RECOVERING
    assert recovering.variables.recovery_count == 1

    restored = _contact_step(
        recovering,
        ContactEventKind.PEER_EVIDENCE_RECEIVED,
        5.5,
    )
    assert restored.state is ContactState.ACTIVE
    assert restored.variables.recovery_count == CONTACT_POLICY.recovery_receipts
    assert {
        initial.state,
        active.state,
        degraded.state,
        lost.state,
        recovering.state,
    } == (set(ContactState))


def test_contact_restoration_requires_explicit_recovery_receipts() -> None:
    configuration = initial_contact_configuration()
    configuration = _contact_step(
        configuration,
        ContactEventKind.PEER_EVIDENCE_RECEIVED,
        0.0,
    )
    configuration = _contact_step(
        configuration,
        ContactEventKind.LOCAL_TIME_ADVANCED,
        4.1,
    )
    assert configuration.state is ContactState.LOST

    first_receipt = transition_contact(
        configuration,
        ContactEvent(
            ContactEventKind.PEER_EVIDENCE_RECEIVED,
            4.2,
            PEER_AGENT_ID,
        ),
        CONTACT_POLICY,
    )
    assert first_receipt.next_state is ContactState.RECOVERING
    assert [effect.kind for effect in first_receipt.effects] == [
        AutonomyEffectKind.CONTACT_RECOVERING
    ]

    recovering = ContactConfiguration(
        first_receipt.next_state,
        first_receipt.next_variables,
    )
    second_receipt = transition_contact(
        recovering,
        ContactEvent(
            ContactEventKind.PEER_EVIDENCE_RECEIVED,
            4.3,
            PEER_AGENT_ID,
        ),
        CONTACT_POLICY,
    )
    assert second_receipt.next_state is ContactState.ACTIVE
    assert [effect.kind for effect in second_receipt.effects] == [
        AutonomyEffectKind.CONTACT_ACTIVE
    ]


def test_peer_availability_preserves_current_peer_status_semantics() -> None:
    configuration = initial_peer_availability_configuration()
    assert configuration.state is PeerStatus.SILENT

    configuration = _peer_step(
        configuration,
        PeerAvailabilityEventKind.OBSERVATION_RECEIVED,
        0.0,
    )
    assert configuration.state is PeerStatus.HEARD
    assert configuration.variables.observation_count == 1

    configuration = _peer_step(
        configuration,
        PeerAvailabilityEventKind.SILENCE_OBSERVED,
        1.0,
    )
    assert configuration.state is PeerStatus.SILENT

    configuration = _peer_step(
        configuration,
        PeerAvailabilityEventKind.FAILURE_CERTIFICATE_RECEIVED,
        1.0,
    )
    assert configuration.state is PeerStatus.DECLARED_FAILED
    assert configuration.variables.declaration_active is True

    still_declared = _peer_step(
        configuration,
        PeerAvailabilityEventKind.SILENCE_OBSERVED,
        2.0,
    )
    assert still_declared.state is PeerStatus.DECLARED_FAILED

    reheard = _peer_step(
        still_declared,
        PeerAvailabilityEventKind.OBSERVATION_RECEIVED,
        3.0,
    )
    assert reheard.state is PeerStatus.HEARD
    assert reheard.variables.declaration_active is False


@pytest.mark.parametrize(
    ("kind", "expected_state"),
    [
        (TaskOwnershipEventKind.NO_VALID_CLAIM, TaskOwnershipState.UNCLAIMED),
        (
            TaskOwnershipEventKind.LOCAL_VALID_CLAIM,
            TaskOwnershipState.OWNED_BY_SELF,
        ),
        (
            TaskOwnershipEventKind.FRESH_PEER_VALID_CLAIM,
            TaskOwnershipState.CLAIMED_BY_PEER_FRESH,
        ),
        (
            TaskOwnershipEventKind.STALE_PEER_VALID_CLAIM,
            TaskOwnershipState.CLAIMED_BY_PEER_STALE,
        ),
        (TaskOwnershipEventKind.CONTEST_VISIBLE, TaskOwnershipState.CONTESTED),
        (TaskOwnershipEventKind.COMPLETION_EVIDENCE, TaskOwnershipState.COMPLETE),
    ],
)
def test_task_event_vocabulary_maps_to_all_six_existing_states(
    kind: TaskOwnershipEventKind,
    expected_state: TaskOwnershipState,
) -> None:
    result = transition_task_ownership(
        initial_task_ownership_configuration(),
        _task_event(kind, 0.0),
        OBSERVER_AGENT_ID,
    )

    assert result.next_state is expected_state
    assert result.next_variables.contested is (
        expected_state is TaskOwnershipState.CONTESTED
    )
    assert result.next_variables.completion_known is (
        expected_state is TaskOwnershipState.COMPLETE
    )


@pytest.mark.parametrize(
    ("state", "expected_kind"),
    [
        (TaskOwnershipState.UNCLAIMED, TaskOwnershipEventKind.NO_VALID_CLAIM),
        (TaskOwnershipState.OWNED_BY_SELF, TaskOwnershipEventKind.LOCAL_VALID_CLAIM),
        (
            TaskOwnershipState.CLAIMED_BY_PEER_FRESH,
            TaskOwnershipEventKind.FRESH_PEER_VALID_CLAIM,
        ),
        (
            TaskOwnershipState.CLAIMED_BY_PEER_STALE,
            TaskOwnershipEventKind.STALE_PEER_VALID_CLAIM,
        ),
        (TaskOwnershipState.CONTESTED, TaskOwnershipEventKind.CONTEST_VISIBLE),
        (TaskOwnershipState.COMPLETE, TaskOwnershipEventKind.COMPLETION_EVIDENCE),
    ],
)
def test_receiver_local_task_views_map_to_the_formal_event_vocabulary(
    state: TaskOwnershipState,
    expected_kind: TaskOwnershipEventKind,
) -> None:
    event = task_ownership_event_from_view(
        OBSERVER_AGENT_ID,
        _task_view(state),
        1.0,
    )

    assert event.kind is expected_kind
    assert event.task_id == TASK_ID


def test_task_complete_is_absorbing_for_every_later_evidence_class() -> None:
    configuration = _task_step(
        initial_task_ownership_configuration(),
        TaskOwnershipEventKind.COMPLETION_EVIDENCE,
        0.0,
    )

    for timestamp, kind in enumerate(TaskOwnershipEventKind, start=1):
        result = transition_task_ownership(
            configuration,
            _task_event(kind, float(timestamp)),
            OBSERVER_AGENT_ID,
        )
        assert result.next_state is TaskOwnershipState.COMPLETE
        assert result.next_variables.completion_known is True
        assert result.effects == ()
        configuration = TaskOwnershipConfiguration(
            result.next_state,
            result.next_variables,
        )


def test_coordination_mode_lifecycle_uses_only_local_evidence() -> None:
    configuration = initial_coordination_configuration()
    assert configuration.state is CoordinationMode.COOPERATIVE

    mildly_degraded = (ContactState.ACTIVE, ContactState.UNKNOWN)
    configuration = _coordination_step(
        configuration,
        _coordination_event(0.0, mildly_degraded),
    )
    configuration = _coordination_step(
        configuration,
        _coordination_event(1.0, mildly_degraded),
    )
    assert configuration.state is CoordinationMode.DEGRADED

    unavailable = (ContactState.LOST, ContactState.LOST)
    configuration = _coordination_step(
        configuration,
        _coordination_event(1.0, unavailable),
    )
    configuration = _coordination_step(
        configuration,
        _coordination_event(2.0, unavailable),
    )
    assert configuration.state is CoordinationMode.LOCAL_AUTONOMY

    configuration = _coordination_step(
        configuration,
        _coordination_event(
            2.1,
            unavailable,
            new_remote_evidence=True,
        ),
    )
    assert configuration.state is CoordinationMode.RECONCILING

    healthy = (ContactState.ACTIVE, ContactState.ACTIVE)
    configuration = _coordination_step(
        configuration,
        _coordination_event(2.2, healthy),
    )
    configuration = _coordination_step(
        configuration,
        _coordination_event(3.2, healthy),
    )
    assert configuration.state is CoordinationMode.COOPERATIVE


def test_two_receivers_can_legitimately_hold_different_coordination_modes() -> None:
    first = initial_coordination_configuration()
    second = initial_coordination_configuration()

    first = _coordination_step(
        first,
        _coordination_event(1.0, (ContactState.ACTIVE,)),
    )
    severe = (ContactState.LOST,)
    second = _coordination_step(second, _coordination_event(0.0, severe))
    second = _coordination_step(second, _coordination_event(1.0, severe))
    second = _coordination_step(second, _coordination_event(2.0, severe))

    assert first.state is CoordinationMode.COOPERATIVE
    assert second.state is CoordinationMode.LOCAL_AUTONOMY


def _event_sequences(
    alphabet: Sequence[Any], maximum_length: int
) -> Iterator[tuple[Any, ...]]:
    yield ()
    for length in range(1, maximum_length + 1):
        yield from product(alphabet, repeat=length)


def test_bounded_event_sequences_are_deterministic_and_preserve_invariants() -> None:
    contact_reachable = {ContactState.UNKNOWN}
    for sequence in _event_sequences(tuple(ContactEventKind), 5):

        def replay_contact() -> tuple[ContactConfiguration, tuple[object, ...]]:
            configuration = initial_contact_configuration()
            history: list[object] = []
            for timestamp, kind in enumerate(sequence, start=1):
                result = transition_contact(
                    configuration,
                    ContactEvent(kind, float(timestamp), PEER_AGENT_ID),
                    ContactPolicy(1.0, 1.0, 2.0, 2),
                )
                history.append(result)
                configuration = ContactConfiguration(
                    result.next_state,
                    result.next_variables,
                )
                contact_reachable.add(configuration.state)
            return configuration, tuple(history)

        assert replay_contact() == replay_contact()

    peer_reachable = {PeerStatus.SILENT}
    for sequence in _event_sequences(tuple(PeerAvailabilityEventKind), 4):

        def replay_peer() -> tuple[PeerAvailabilityConfiguration, tuple[object, ...]]:
            configuration = initial_peer_availability_configuration()
            history: list[object] = []
            for timestamp, kind in enumerate(sequence, start=1):
                result = transition_peer_availability(
                    configuration,
                    PeerAvailabilityEvent(kind, float(timestamp), PEER_AGENT_ID),
                )
                history.append(result)
                configuration = PeerAvailabilityConfiguration(
                    result.next_state,
                    result.next_variables,
                )
                peer_reachable.add(configuration.state)
            return configuration, tuple(history)

        assert replay_peer() == replay_peer()

    task_reachable = {TaskOwnershipState.UNCLAIMED}
    for sequence in _event_sequences(tuple(TaskOwnershipEventKind), 4):

        def replay_task() -> tuple[TaskOwnershipConfiguration, tuple[object, ...]]:
            configuration = initial_task_ownership_configuration()
            history: list[object] = []
            complete_seen = False
            for timestamp, kind in enumerate(sequence, start=1):
                result = transition_task_ownership(
                    configuration,
                    _task_event(kind, float(timestamp)),
                    OBSERVER_AGENT_ID,
                )
                history.append(result)
                if complete_seen:
                    assert result.next_state is TaskOwnershipState.COMPLETE
                complete_seen |= result.next_state is TaskOwnershipState.COMPLETE
                configuration = TaskOwnershipConfiguration(
                    result.next_state,
                    result.next_variables,
                )
                task_reachable.add(configuration.state)
            return configuration, tuple(history)

        assert replay_task() == replay_task()

    coordination_alphabet = (
        ((ContactState.ACTIVE,), 0, False),
        ((ContactState.ACTIVE, ContactState.UNKNOWN), 0, False),
        ((ContactState.LOST,), 0, False),
        ((ContactState.LOST,), 0, True),
        ((ContactState.ACTIVE,), 1, False),
    )
    coordination_reachable = {CoordinationMode.COOPERATIVE}
    for sequence in _event_sequences(coordination_alphabet, 4):

        def replay_coordination() -> tuple[
            CoordinationConfiguration,
            tuple[object, ...],
        ]:
            configuration = initial_coordination_configuration()
            history: list[object] = []
            for timestamp, evidence in enumerate(sequence, start=1):
                states, contests, novel = evidence
                result = transition_coordination_mode(
                    configuration,
                    _coordination_event(
                        float(timestamp),
                        states,
                        contests=contests,
                        new_remote_evidence=novel,
                    ),
                    COORDINATION_POLICY,
                )
                history.append(result)
                configuration = CoordinationConfiguration(
                    result.next_state,
                    result.next_variables,
                )
                coordination_reachable.add(configuration.state)
            return configuration, tuple(history)

        assert replay_coordination() == replay_coordination()

    assert contact_reachable == set(ContactState)
    assert peer_reachable == set(PeerStatus)
    assert task_reachable == set(TaskOwnershipState)
    assert coordination_reachable == set(CoordinationMode)


def test_autonomy_transition_source_has_no_world_or_observer_dependencies() -> None:
    source = inspect.getsource(autonomy)
    tree = ast.parse(source)
    forbidden_modules = {
        "agent",
        "communication",
        "mission",
        "simulation",
        "trace",
    }
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imported_modules.isdisjoint(forbidden_modules)

    forbidden_guard_tokens = {
        "CommunicationGraph",
        "connected_components",
        "is_fully_connected",
        "can_deliver",
        "responsive",
        "assigned_agent",
        "neighbor_ids",
    }
    transition_source = "\n".join(
        inspect.getsource(transition)
        for transition in (
            transition_contact,
            transition_peer_availability,
            transition_task_ownership,
            transition_coordination_mode,
        )
    )
    transition_tree = ast.parse(transition_source)
    referenced_names = {
        node.id for node in ast.walk(transition_tree) if isinstance(node, ast.Name)
    } | {
        node.attr
        for node in ast.walk(transition_tree)
        if isinstance(node, ast.Attribute)
    }
    assert referenced_names.isdisjoint(forbidden_guard_tokens)


def test_canonical_machine_definitions_cover_vocabulary_and_rendered_tables() -> None:
    definitions = machine_definitions()
    assert tuple(definition.name for definition in definitions) == tuple(MachineName)

    expected_states = {
        MachineName.CONTACT: {state.value for state in ContactState},
        MachineName.PEER_AVAILABILITY: {state.value for state in PeerStatus},
        MachineName.TASK_OWNERSHIP: {state.value for state in TaskOwnershipState},
        MachineName.COORDINATION_MODE: {state.value for state in CoordinationMode},
    }
    expected_events = {
        MachineName.CONTACT: {event.value for event in ContactEventKind},
        MachineName.PEER_AVAILABILITY: {
            event.value for event in PeerAvailabilityEventKind
        },
        MachineName.TASK_OWNERSHIP: {event.value for event in TaskOwnershipEventKind},
        MachineName.COORDINATION_MODE: {event.value for event in CoordinationEventKind},
    }
    effect_values = {effect.value for effect in AutonomyEffectKind}
    rendered = render_transition_tables()
    assert rendered == render_transition_tables()

    for definition in definitions:
        state_values = expected_states[definition.name]
        assert set(definition.states) == state_values
        assert definition.initial_state in state_values
        assert state_values <= {
            definition.initial_state,
            *(rule.target for rule in definition.rules),
        }
        assert f"### {definition.name.value}" in rendered
        for rule in definition.rules:
            assert rule.source in state_values | {"*", "NONCOMPLETE"}
            assert rule.target in state_values
            assert rule.event in expected_events[definition.name] | {"*"}
            assert set(rule.effects) <= effect_values
            assert f"`{rule.source}`" in rendered
            assert f"`{rule.event}`" in rendered
            assert rule.guard in rendered
            assert f"`{rule.target}`" in rendered


def test_checked_in_transition_tables_are_generated_from_canonical_specs() -> None:
    documentation = (Path(__file__).parents[1] / "docs" / "autonomy_efsm.md").read_text(
        encoding="utf-8"
    )
    generated = documentation.split("<!-- BEGIN GENERATED TRANSITION TABLES -->\n", 1)[
        1
    ].split("<!-- END GENERATED TRANSITION TABLES -->", 1)[0]

    assert generated.strip() == render_transition_tables().strip()
