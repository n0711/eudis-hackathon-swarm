"""Versioned operator intent and the per-UAV mission phase machine.

Two properties carry the design. Intent is versioned and never rolls backwards,
because delivery is unreliable and reordered. And the phase is per-UAV: two
aircraft on opposite sides of a partition are allowed to disagree about what
the mission is doing, since agreeing would need the consensus the partition
denies.
"""

from __future__ import annotations

import pytest

from eudis_swarm.autonomy import (
    MISSION_PHASE_MACHINE,
    AutonomyEffectKind,
    CoordinationMode,
    MachineName,
    MissionPhase,
    MissionPhaseConfiguration,
    MissionPhaseEvent,
    MissionPhaseEventKind,
    initial_mission_phase_configuration,
    transition_mission_phase,
)
from eudis_swarm.mission_contract import (
    DEFAULT_PRIORITY,
    DegradationPolicy,
    MissionContract,
    build_contract,
)


def _contract(
    *,
    version: int = 1,
    policy: DegradationPolicy = DegradationPolicy.CONTINUE,
    minimum_participants: int = 1,
    degraded_grace: float = 5.0,
    resume_after_stable_for: float = 2.0,
) -> MissionContract:
    return build_contract(
        version,
        "survey",
        minimum_participants=minimum_participants,
        degradation_policy=policy,
        degraded_grace=degraded_grace,
        resume_after_stable_for=resume_after_stable_for,
    )


def _event(
    timestamp: float,
    *,
    contract: MissionContract | None,
    mode: CoordinationMode = CoordinationMode.COOPERATIVE,
    participants: int = 4,
    outstanding: int = 3,
    kind: MissionPhaseEventKind = MissionPhaseEventKind.LOCAL_EVIDENCE_UPDATED,
) -> MissionPhaseEvent:
    return MissionPhaseEvent(
        kind=kind,
        timestamp=timestamp,
        contract=contract,
        coordination_mode=mode,
        participants_known=participants,
        objectives_outstanding=outstanding,
    )


def _drive(
    configuration: MissionPhaseConfiguration, *events: MissionPhaseEvent
) -> MissionPhaseConfiguration:
    for event in events:
        result = transition_mission_phase(configuration, event)
        configuration = MissionPhaseConfiguration(
            state=result.next_state, variables=result.next_variables
        )
    return configuration


# --------------------------------------------------------------------------
# the contract itself
# --------------------------------------------------------------------------


def test_priorities_are_stored_sorted_for_deterministic_replay() -> None:
    contract = build_contract(1, "survey", task_priorities={7: 3, 2: 9, 5: 1})

    assert contract.task_priorities == ((2, 9), (5, 1), (7, 3))


def test_unlisted_objectives_carry_the_default_priority() -> None:
    contract = build_contract(1, "survey", task_priorities={2: 9})

    assert contract.priority_for(2) == 9
    assert contract.priority_for(11) == DEFAULT_PRIORITY


def test_the_operator_ordering_is_by_priority_then_identifier() -> None:
    contract = build_contract(1, "survey", task_priorities={7: 3, 2: 9, 5: 3})

    assert contract.prioritised_task_ids == (2, 5, 7)


def test_intent_never_rolls_backwards() -> None:
    """Replayed or reordered delivery must not resurrect older intent."""

    first = _contract(version=1)
    second = _contract(version=2)

    assert second.supersedes(first)
    assert second.supersedes(None)
    assert not first.supersedes(second)
    assert not first.supersedes(first)


def test_a_contract_is_immutable() -> None:
    contract = _contract()

    with pytest.raises(Exception):
        contract.version = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"version": 0},
        {"minimum_participants": 0},
        {"degraded_grace": 0.0},
        {"resume_after_stable_for": -1.0},
    ],
)
def test_invalid_intent_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _contract(**kwargs)  # type: ignore[arg-type]


def test_an_unsorted_or_duplicated_priority_table_is_rejected() -> None:
    with pytest.raises(ValueError, match="sorted"):
        MissionContract(version=1, name="survey", task_priorities=((5, 1), (2, 9)))
    with pytest.raises(ValueError, match="duplicate"):
        MissionContract(version=1, name="survey", task_priorities=((2, 1), (2, 9)))


def test_a_nameless_contract_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        MissionContract(version=1, name="   ")


# --------------------------------------------------------------------------
# the phase machine
# --------------------------------------------------------------------------


def test_a_uav_without_a_contract_stays_briefed() -> None:
    """Absence of intent is not permission to act."""

    result = transition_mission_phase(
        initial_mission_phase_configuration(), _event(1.0, contract=None)
    )

    assert result.next_state is MissionPhase.BRIEFED
    assert result.guard == "no contract held"


def test_execution_waits_for_the_contracted_minimum_participants() -> None:
    contract = _contract(minimum_participants=3)
    configuration = initial_mission_phase_configuration()

    thin = transition_mission_phase(
        configuration, _event(1.0, contract=contract, participants=2)
    )
    assert thin.next_state is MissionPhase.BRIEFED

    enough = transition_mission_phase(
        configuration, _event(1.0, contract=contract, participants=3)
    )
    assert enough.next_state is MissionPhase.EXECUTING


def test_continue_policy_keeps_working_through_degradation() -> None:
    contract = _contract(policy=DegradationPolicy.CONTINUE, degraded_grace=2.0)
    configuration = _drive(
        initial_mission_phase_configuration(), _event(1.0, contract=contract)
    )
    assert configuration.state is MissionPhase.EXECUTING

    configuration = _drive(
        configuration,
        _event(2.0, contract=contract, mode=CoordinationMode.LOCAL_AUTONOMY),
        _event(9.0, contract=contract, mode=CoordinationMode.LOCAL_AUTONOMY),
    )

    assert configuration.state is MissionPhase.EXECUTING


def test_hold_policy_stops_taking_work_after_the_contracted_grace() -> None:
    contract = _contract(policy=DegradationPolicy.HOLD, degraded_grace=2.0)
    configuration = _drive(
        initial_mission_phase_configuration(), _event(1.0, contract=contract)
    )

    # inside the grace: still executing
    configuration = _drive(
        configuration,
        _event(2.0, contract=contract, mode=CoordinationMode.DEGRADED),
        _event(3.5, contract=contract, mode=CoordinationMode.DEGRADED),
    )
    assert configuration.state is MissionPhase.EXECUTING

    configuration = _drive(
        configuration, _event(5.0, contract=contract, mode=CoordinationMode.DEGRADED)
    )
    assert configuration.state is MissionPhase.HOLDING


def test_abort_policy_stands_down_and_is_absorbing() -> None:
    contract = _contract(policy=DegradationPolicy.ABORT, degraded_grace=1.0)
    configuration = _drive(
        initial_mission_phase_configuration(), _event(1.0, contract=contract)
    )

    # the first degraded observation only starts the clock
    configuration = _drive(
        configuration,
        _event(2.0, contract=contract, mode=CoordinationMode.LOCAL_AUTONOMY),
    )
    assert configuration.state is MissionPhase.EXECUTING

    result = transition_mission_phase(
        configuration,
        _event(5.0, contract=contract, mode=CoordinationMode.LOCAL_AUTONOMY),
    )
    assert result.next_state is MissionPhase.ABORTED
    assert [effect.kind for effect in result.effects] == [
        AutonomyEffectKind.STAND_DOWN_TASK
    ]

    aborted = MissionPhaseConfiguration(
        state=result.next_state, variables=result.next_variables
    )
    recovered = transition_mission_phase(
        aborted, _event(9.0, contract=contract, mode=CoordinationMode.COOPERATIVE)
    )

    assert recovered.next_state is MissionPhase.ABORTED
    assert recovered.guard == "abort is absorbing"


def test_only_superseding_intent_revives_an_abort() -> None:
    """Recovery is an operator decision, never an inference from good comms."""

    contract = _contract(version=1, policy=DegradationPolicy.ABORT, degraded_grace=1.0)
    configuration = _drive(
        initial_mission_phase_configuration(),
        _event(1.0, contract=contract),
        _event(2.0, contract=contract, mode=CoordinationMode.LOCAL_AUTONOMY),
        _event(5.0, contract=contract, mode=CoordinationMode.LOCAL_AUTONOMY),
    )
    assert configuration.state is MissionPhase.ABORTED

    amended = _contract(version=2, policy=DegradationPolicy.CONTINUE)
    result = transition_mission_phase(
        configuration,
        _event(
            6.0,
            contract=amended,
            kind=MissionPhaseEventKind.CONTRACT_RECEIVED,
        ),
    )

    assert result.next_state is MissionPhase.BRIEFED
    assert result.guard == "new contract version supersedes an abort"


def test_holding_resumes_only_after_the_contracted_stable_window() -> None:
    contract = _contract(
        policy=DegradationPolicy.HOLD, degraded_grace=1.0, resume_after_stable_for=3.0
    )
    configuration = _drive(
        initial_mission_phase_configuration(),
        _event(1.0, contract=contract),
        _event(2.0, contract=contract, mode=CoordinationMode.DEGRADED),
        _event(4.0, contract=contract, mode=CoordinationMode.DEGRADED),
    )
    assert configuration.state is MissionPhase.HOLDING

    configuration = _drive(
        configuration, _event(5.0, contract=contract, mode=CoordinationMode.COOPERATIVE)
    )
    assert configuration.state is MissionPhase.HOLDING, "resumed before the window"

    configuration = _drive(
        configuration, _event(9.0, contract=contract, mode=CoordinationMode.COOPERATIVE)
    )
    assert configuration.state is MissionPhase.EXECUTING


def test_finishing_the_work_completes_the_phase_and_can_reopen() -> None:
    contract = _contract()
    configuration = _drive(
        initial_mission_phase_configuration(),
        _event(1.0, contract=contract),
        _event(2.0, contract=contract, outstanding=0),
    )
    assert configuration.state is MissionPhase.COMPLETE

    configuration = _drive(configuration, _event(3.0, contract=contract, outstanding=2))
    assert configuration.state is MissionPhase.EXECUTING


def test_two_uavs_holding_different_versions_may_diverge() -> None:
    """The reason intent is versioned rather than assumed."""

    holds = _contract(version=2, policy=DegradationPolicy.HOLD, degraded_grace=1.0)
    presses_on = _contract(
        version=1, policy=DegradationPolicy.CONTINUE, degraded_grace=1.0
    )
    start = initial_mission_phase_configuration()
    events = (CoordinationMode.DEGRADED, CoordinationMode.DEGRADED)

    cautious = _drive(
        start,
        _event(1.0, contract=holds),
        *(
            _event(time, contract=holds, mode=mode)
            for time, mode in zip((3.0, 5.0), events, strict=True)
        ),
    )
    bold = _drive(
        start,
        _event(1.0, contract=presses_on),
        *(
            _event(time, contract=presses_on, mode=mode)
            for time, mode in zip((3.0, 5.0), events, strict=True)
        ),
    )

    assert cautious.state is MissionPhase.HOLDING
    assert bold.state is MissionPhase.EXECUTING


def test_time_must_not_move_backwards() -> None:
    contract = _contract()
    configuration = _drive(
        initial_mission_phase_configuration(), _event(5.0, contract=contract)
    )

    with pytest.raises(ValueError):
        transition_mission_phase(configuration, _event(1.0, contract=contract))


def test_an_unchanged_phase_requests_no_effects() -> None:
    """Effects are edge-triggered; a steady state must stay quiet."""

    contract = _contract()
    configuration = _drive(
        initial_mission_phase_configuration(), _event(1.0, contract=contract)
    )

    result = transition_mission_phase(configuration, _event(2.0, contract=contract))

    assert result.next_state is MissionPhase.EXECUTING
    assert result.effects == ()


# --------------------------------------------------------------------------
# the published transition table
# --------------------------------------------------------------------------


def test_the_machine_definition_matches_the_implemented_states() -> None:
    assert MISSION_PHASE_MACHINE.name is MachineName.MISSION_PHASE
    assert set(MISSION_PHASE_MACHINE.states) == {state.value for state in MissionPhase}
    assert MISSION_PHASE_MACHINE.initial_state == MissionPhase.BRIEFED.value


def test_every_published_rule_names_real_states_and_effects() -> None:
    states = set(MISSION_PHASE_MACHINE.states)
    effects = {kind.value for kind in AutonomyEffectKind}

    for rule in MISSION_PHASE_MACHINE.rules:
        assert rule.source in states
        assert rule.target in states
        assert set(rule.effects) <= effects


def test_every_published_guard_is_actually_reachable() -> None:
    """A documented transition nothing can trigger is a lie about the design."""

    contract_hold = _contract(policy=DegradationPolicy.HOLD, degraded_grace=1.0)
    contract_abort = _contract(policy=DegradationPolicy.ABORT, degraded_grace=1.0)
    amended = _contract(version=2)
    start = initial_mission_phase_configuration()

    observed: set[str] = set()

    def record(configuration: MissionPhaseConfiguration, event: MissionPhaseEvent):
        result = transition_mission_phase(configuration, event)
        if result.next_state is not configuration.state:
            observed.add(result.guard)
        return MissionPhaseConfiguration(
            state=result.next_state, variables=result.next_variables
        )

    # BRIEFED -> EXECUTING -> HOLDING -> EXECUTING -> COMPLETE
    configuration = record(start, _event(1.0, contract=contract_hold))
    configuration = record(
        configuration,
        _event(2.0, contract=contract_hold, mode=CoordinationMode.DEGRADED),
    )
    configuration = record(
        configuration,
        _event(4.0, contract=contract_hold, mode=CoordinationMode.DEGRADED),
    )
    configuration = record(
        configuration,
        _event(9.0, contract=contract_hold, mode=CoordinationMode.COOPERATIVE),
    )
    configuration = record(
        configuration,
        _event(12.0, contract=contract_hold, mode=CoordinationMode.COOPERATIVE),
    )
    configuration = record(
        configuration, _event(13.0, contract=contract_hold, outstanding=0)
    )

    # a separate run for ABORT and its supersession
    aborted = record(start, _event(1.0, contract=contract_abort))
    aborted = record(
        aborted,
        _event(2.0, contract=contract_abort, mode=CoordinationMode.LOCAL_AUTONOMY),
    )
    aborted = record(
        aborted,
        _event(5.0, contract=contract_abort, mode=CoordinationMode.LOCAL_AUTONOMY),
    )
    record(aborted, _event(6.0, contract=amended))

    published = {rule.guard for rule in MISSION_PHASE_MACHINE.rules}
    assert published <= observed, f"unreachable: {sorted(published - observed)}"


# --------------------------------------------------------------------------
# the knowledge boundary
# --------------------------------------------------------------------------


def test_operator_intent_cannot_become_a_route_to_world_truth() -> None:
    """An allowlist, because a blocklist cannot anticipate the next module.

    ``autonomy`` gained ``mission_contract`` as a dependency. That is only safe
    while the contract module itself stays pure, so the closure is checked
    rather than the single hop.
    """

    import ast
    from pathlib import Path

    source_root = Path(__file__).resolve().parent.parent / "src" / "eudis_swarm"
    allowed = {
        "autonomy": {
            "mission_contract",
            "peer_state",
            "task",
            "task_claims",
            "validation",
        },
        "mission_contract": {"validation"},
        "task_utility": {"validation"},
    }

    for module_name, permitted in allowed.items():
        tree = ast.parse(
            (source_root / f"{module_name}.py").read_text(encoding="utf-8")
        )
        local_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.level
            and node.module is not None
        }
        assert local_imports <= permitted, (
            f"{module_name} imports {sorted(local_imports - permitted)}, which is "
            "outside its allowlist and may reach world truth"
        )


# --------------------------------------------------------------------------
# the contract in a running mission
# --------------------------------------------------------------------------


def _partition_run(contract: MissionContract | None):
    from dataclasses import replace as _replace

    from eudis_swarm.config import SimulationConfig
    from eudis_swarm.simulation import SCENARIO_PRESETS, Simulation

    base = _replace(SimulationConfig(), **SCENARIO_PRESETS["demo-partition"])  # type: ignore[arg-type]
    return Simulation(_replace(base, mission_contract=contract)).run()


def test_an_uncontracted_mission_is_unconstrained() -> None:
    """A contract only ever adds restraint; its absence changes nothing."""

    uncontracted = _partition_run(None).metrics
    permissive = _partition_run(
        build_contract(1, "survey", degradation_policy=DegradationPolicy.CONTINUE)
    ).metrics

    assert uncontracted.completed_task_count == 20
    assert uncontracted.simulation_duration == permissive.simulation_duration
    assert (
        uncontracted.concurrent_ownership_duration
        == permissive.concurrent_ownership_duration
    )


def test_hold_trades_mission_time_for_zero_double_booking() -> None:
    """The operator's policy is a dial on the measured trade-off.

    Holding through a partition removes concurrent ownership entirely, and
    pays for it in mission duration. Neither outcome is simply better, which
    is exactly why the choice belongs to the operator rather than the code.
    """

    pressing_on = _partition_run(
        build_contract(
            1,
            "survey",
            degradation_policy=DegradationPolicy.CONTINUE,
            degraded_grace=1.0,
        )
    ).metrics
    holding = _partition_run(
        build_contract(
            1, "survey", degradation_policy=DegradationPolicy.HOLD, degraded_grace=1.0
        )
    ).metrics

    assert pressing_on.completed_task_count == holding.completed_task_count == 20
    assert pressing_on.concurrent_ownership_duration > 0.0
    assert holding.concurrent_ownership_duration == 0.0
    assert holding.simulation_duration > pressing_on.simulation_duration


def test_abort_stops_the_mission_instead_of_burning_the_clock() -> None:
    """Standing everybody down ends the run honestly, not at a timeout."""

    result = _partition_run(
        build_contract(
            1, "survey", degradation_policy=DegradationPolicy.ABORT, degraded_grace=1.0
        )
    )
    metrics = result.metrics

    assert metrics.mission_completed is False
    assert metrics.completed_task_count < metrics.total_task_count
    # far below max_simulation_time: it stopped because it was told to
    assert metrics.simulation_duration is not None
    assert metrics.simulation_duration < 10.0
    assert result.autonomy_kernels is not None
    assert all(
        kernel.mission_phase is MissionPhase.ABORTED
        for kernel in result.autonomy_kernels.values()
    )


def test_the_trace_records_each_uav_phase_and_contract_version() -> None:
    from dataclasses import replace as _replace

    from eudis_swarm.config import SimulationConfig
    from eudis_swarm.simulation import SCENARIO_PRESETS, Simulation

    base = _replace(SimulationConfig(), **SCENARIO_PRESETS["demo-partition"])  # type: ignore[arg-type]
    contract = build_contract(
        3, "survey", degradation_policy=DegradationPolicy.HOLD, degraded_grace=1.0
    )
    trace = (
        Simulation(_replace(base, mission_contract=contract), capture_trace=True)
        .run()
        .trace
    )
    assert trace is not None

    phases = {agent.mission_phase for frame in trace.frames for agent in frame.agents}
    versions = {
        agent.contract_version for frame in trace.frames for agent in frame.agents
    }

    assert versions == {3}, "every UAV was briefed with the same version"
    assert MissionPhase.EXECUTING.value in phases
    assert MissionPhase.HOLDING.value in phases


# --------------------------------------------------------------------------
# amendments propagate as evidence, and can diverge
# --------------------------------------------------------------------------


def _amended_run(amend_at: float, *, capture: bool = True):
    from dataclasses import replace as _replace

    from eudis_swarm.config import SimulationConfig
    from eudis_swarm.simulation import SCENARIO_PRESETS, Simulation

    base = _replace(SimulationConfig(), **SCENARIO_PRESETS["demo-partition"])  # type: ignore[arg-type]
    original = build_contract(
        1, "survey", degradation_policy=DegradationPolicy.CONTINUE, degraded_grace=1.0
    )
    amendment = build_contract(
        2,
        "survey-amended",
        degradation_policy=DegradationPolicy.HOLD,
        degraded_grace=1.0,
    )
    return Simulation(
        _replace(
            base,
            mission_contract=original,
            contract_amendment=amendment,
            contract_amendment_time=amend_at,
            contract_amendment_agent_id=1,
        ),
        capture_trace=capture,
    ).run()


def test_the_briefed_uav_actually_takes_delivery_of_the_amendment() -> None:
    """Gossip seeds an origin as knowing a message without applying it.

    That is right for a claim its owner already holds, and wrong for operator
    intent: the aircraft the operator reached must hold what it was handed.
    """

    trace = _amended_run(6.0).trace
    assert trace is not None

    at_issue = next(frame for frame in trace.frames if frame.timestamp >= 6.0)
    briefed = next(agent for agent in at_issue.agents if agent.agent_id == 1)

    assert briefed.contract_version == 2


def test_an_amendment_issued_mid_partition_diverges_then_converges() -> None:
    """The reason intent is versioned rather than assumed."""

    result = _amended_run(6.0)
    trace = result.trace
    assert trace is not None

    diverged = [
        frame
        for frame in trace.frames
        if len({agent.contract_version for agent in frame.agents}) > 1
    ]

    assert diverged, "the amendment never diverged, so nothing was demonstrated"
    at_issue = diverged[0]
    versions = {agent.agent_id: agent.contract_version for agent in at_issue.agents}
    assert versions[1] == 2
    assert {versions[peer] for peer in (2, 3, 4)} == {1}

    # and every UAV converges once the network heals
    assert result.autonomy_kernels is not None
    assert {kernel.contract_version for kernel in result.autonomy_kernels.values()} == {
        2
    }
    assert result.metrics.completed_task_count == 20


def test_an_older_amendment_cannot_roll_intent_backwards() -> None:
    from eudis_swarm.messaging import MissionContractMessage

    result = _amended_run(6.0, capture=False)
    assert result.autonomy_kernels is not None
    kernel = result.autonomy_kernels[1]
    assert kernel.contract_version == 2

    stale = build_contract(1, "survey", degradation_policy=DegradationPolicy.ABORT)
    accepted = kernel.receive_contract(stale, 99.0)

    assert accepted is False
    assert kernel.contract_version == 2
    # the message type still validates its own shape
    assert MissionContractMessage(contract=stale, briefed_agent_id=1).version == 1


def test_an_amendment_must_supersede_the_contract_it_amends() -> None:
    from dataclasses import replace as _replace

    from eudis_swarm.config import SimulationConfig

    original = build_contract(2, "survey")
    with pytest.raises(ValueError, match="supersede"):
        _replace(
            SimulationConfig(),
            mission_contract=original,
            contract_amendment=build_contract(1, "older"),
        )
    with pytest.raises(ValueError, match="initial mission_contract"):
        _replace(SimulationConfig(), contract_amendment=build_contract(2, "orphan"))
