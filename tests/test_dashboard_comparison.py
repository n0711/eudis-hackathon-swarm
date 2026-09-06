"""The side-by-side belief view.

Streamlit rendering is not unit-testable, but everything that decides *what*
gets shown is pure and is tested here. The property that matters most is that a
panel never states a conclusion the evidence does not support: a peer is
"SILENT 8.7s", never "lost".
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.dashboard_app import (
    CONTACT_COLOURS,
    OWNERSHIP_COLOURS,
    OWNERSHIP_LABELS,
    REPLAY_PROVENANCE,
    aligned_frame_index,
    contact_evidence_label,
    ownership_evidence_label,
)
from eudis_swarm.simulation import SCENARIO_PRESETS, Simulation
from eudis_swarm.trace import (
    TraceAgentState,
    TraceContactState,
    TraceOwnershipView,
    TracePeerKnowledge,
)

OWNERSHIP_STATES = (
    "UNCLAIMED",
    "OWNED_BY_SELF",
    "CLAIMED_BY_PEER_FRESH",
    "CLAIMED_BY_PEER_STALE",
    "CONTESTED",
    "COMPLETE",
)


def _agent(
    *,
    peer_status: str | None,
    last_rx_at: float | None,
    received_at: float | None = None,
    contact_state: str | None = "HEARD",
) -> TraceAgentState:
    knowledge = TracePeerKnowledge(
        peer_agent_id=2,
        state="FRESH",
        snapshot_timestamp=None,
        received_at=received_at,
        last_known_position=None,
        last_known_status=None,
        last_known_task=None,
        peer_status=peer_status,
    )
    contact = TraceContactState(
        peer_agent_id=2,
        state=contact_state or "HEARD",
        last_rx_at=last_rx_at,
        previous_rx_at=None,
        consecutive_expected_misses=0,
        successful_rx_count=1,
        recovery_count=0,
    )
    contacts = () if contact_state is None else (contact,)
    return TraceAgentState(
        agent_id=1,
        physical_state="ACTIVE",
        coordinator_state="ACTIVE",
        responsive=True,
        failure_detected=False,
        current_task=None,
        position=(0.0, 0.0),
        neighbor_ids=(),
        fresh_peer_count=1,
        stale_peer_count=0,
        unknown_peer_count=0,
        peer_knowledge=(knowledge,),
        coordination_mode="NOMINAL",
        contact_states=contacts,
    )


def _view(state: str, *, contested: bool = False) -> TraceOwnershipView:
    return TraceOwnershipView(
        observer_agent_id=1,
        task_id=7,
        state=state,
        known_owner_agent_id=1,
        contested=contested,
    )


# --------------------------------------------------------------------------
# evidence, never conclusion
# --------------------------------------------------------------------------


def test_a_heard_peer_is_labelled_with_the_age_of_the_evidence() -> None:
    label, colour = contact_evidence_label(
        _agent(peer_status="HEARD", last_rx_at=9.6), 2, now=10.0
    )

    assert label == "2: HEARD 0.4s"
    assert colour == CONTACT_COLOURS["HEARD"]


def test_a_silent_peer_reports_how_long_the_silence_has_lasted() -> None:
    label, _ = contact_evidence_label(
        _agent(peer_status="SILENT", last_rx_at=1.3), 2, now=10.0
    )

    assert label == "2: SILENT 8.7s"


def test_a_declaration_names_the_quorum_that_produced_it() -> None:
    label, colour = contact_evidence_label(
        _agent(peer_status="DECLARED_FAILED", last_rx_at=1.0), 2, now=10.0
    )

    assert label == "2: DECLARED FAILED (quorum)"
    assert colour == CONTACT_COLOURS["DECLARED_FAILED"]


def test_a_peer_never_heard_from_reports_absence_of_evidence() -> None:
    """Not "alive", not "dead" -- nothing has been received."""

    label, _ = contact_evidence_label(
        _agent(peer_status=None, last_rx_at=None, contact_state=None), 2, now=10.0
    )

    assert label == "2: NO EVIDENCE"


def test_no_panel_can_claim_a_uav_is_alive_or_unreachable() -> None:
    """Two vocabularies are deliberately absent.

    "ALIVE" is a conclusion no receiver can justify. "UNREACHABLE" was removed
    with the link oracle: a radio cannot tell a jammed peer from a destroyed
    one, so silence is reported as silence.
    """

    vocabulary = set(CONTACT_COLOURS) | set(OWNERSHIP_LABELS.values())

    assert "ALIVE" not in vocabulary
    assert "UNREACHABLE" not in vocabulary
    assert set(CONTACT_COLOURS) == {"HEARD", "SILENT", "DECLARED_FAILED"}


def test_evidence_age_never_goes_negative() -> None:
    """Frame alignment can put a receipt marginally ahead of the frame clock."""

    label, _ = contact_evidence_label(
        _agent(peer_status="HEARD", last_rx_at=10.5), 2, now=10.0
    )

    assert label == "2: HEARD 0.0s"


# --------------------------------------------------------------------------
# ownership vocabulary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", OWNERSHIP_STATES)
def test_every_ownership_state_has_a_label_and_a_colour(state: str) -> None:
    assert state in OWNERSHIP_LABELS
    assert state in OWNERSHIP_COLOURS


def test_a_contest_outranks_the_underlying_state_in_the_label() -> None:
    """A replica can own a task and see a competing claim at the same time."""

    label, colour = ownership_evidence_label(_view("OWNED_BY_SELF", contested=True))

    assert label == "T7: CONTESTED"
    assert colour == OWNERSHIP_COLOURS["CONTESTED"]


def test_an_uncontested_state_is_labelled_plainly() -> None:
    label, colour = ownership_evidence_label(_view("CLAIMED_BY_PEER_STALE"))

    assert label == "T7: CLAIMED BY PEER (stale)"
    assert colour == OWNERSHIP_COLOURS["CLAIMED_BY_PEER_STALE"]


# --------------------------------------------------------------------------
# reachability: a legend nobody can trigger is a lie
# --------------------------------------------------------------------------


def test_every_contact_state_is_reachable_in_the_demo_scenarios() -> None:
    seen: set[str] = set()
    for name in ("baseline", "demo-partition", "demo-failstop"):
        config = replace(SimulationConfig(), **SCENARIO_PRESETS[name])  # type: ignore[arg-type]
        trace = Simulation(config, capture_trace=True).run().trace
        assert trace is not None
        for frame in trace.frames:
            for agent in frame.agents:
                seen.update(
                    peer.peer_status
                    for peer in agent.peer_knowledge
                    if peer.peer_status is not None
                )

    assert set(CONTACT_COLOURS) <= seen


def test_every_ownership_state_is_reachable_and_displayable() -> None:
    """All six must appear on screen, so all six must occur somewhere.

    CONTESTED is carried by the ``contested`` flag rather than the state
    string, and UNCLAIMED is the absence of recorded evidence, so both are
    derived by the renderer rather than read straight from a field.
    """

    from eudis_swarm.communication import RegionJammer

    rendered: set[str] = set()
    configs = [
        replace(SimulationConfig(), **SCENARIO_PRESETS["demo-partition"]),  # type: ignore[arg-type]
        replace(
            SimulationConfig(),
            communication_range=50.0,
            task_count=3,
            failure_time=1000.0,
            region_jammer=RegionJammer(40.0, 50.0, 15.0, 3.0, 8.0),
        ),
    ]
    for config in configs:
        trace = Simulation(config, capture_trace=True).run().trace
        assert trace is not None
        for frame in trace.frames:
            for view in frame.ownership:
                label, _ = ownership_evidence_label(view)
                rendered.add(label.split(": ", 1)[1])
            # tasks with no recorded view are UNCLAIMED to that observer
            for agent in frame.agents:
                observed = {
                    view.task_id
                    for view in frame.ownership
                    if view.observer_agent_id == agent.agent_id
                }
                if len(observed) < len(frame.tasks):
                    rendered.add("UNCLAIMED")

    expected = {OWNERSHIP_LABELS[state] for state in OWNERSHIP_STATES}
    assert expected <= rendered, f"never displayable: {sorted(expected - rendered)}"


# --------------------------------------------------------------------------
# alignment and provenance
# --------------------------------------------------------------------------


def test_the_two_runs_align_by_logical_time_not_frame_number() -> None:
    left = (
        Simulation(
            replace(SimulationConfig(), **SCENARIO_PRESETS["baseline"]),  # type: ignore[arg-type]
            capture_trace=True,
        )
        .run()
        .trace
    )
    right = (
        Simulation(
            replace(SimulationConfig(), **SCENARIO_PRESETS["demo-partition"]),  # type: ignore[arg-type]
            capture_trace=True,
        )
        .run()
        .trace
    )
    assert left is not None and right is not None

    for timestamp in (0.0, 3.0, 5.5, 9.0):
        for trace in (left, right):
            index = aligned_frame_index(trace, timestamp)
            assert trace.frames[index].timestamp <= timestamp + 1e-9
            if index + 1 < len(trace.frames):
                assert trace.frames[index + 1].timestamp > timestamp + 1e-9


def test_a_finished_run_holds_its_last_frame_while_the_other_runs_on() -> None:
    """The pitch pairing puts a 10.75 s run beside one that never finishes.

    Past the end of the shorter trace there is no frame to advance to, so the
    completed side must hold its final state rather than disappear or raise.
    That frozen panel beside a still-advancing one is the whole visual argument,
    so it is pinned here rather than left to the renderer.
    """

    finished = (
        Simulation(
            replace(SimulationConfig(), **SCENARIO_PRESETS["demo-quorum-floor"]),  # type: ignore[arg-type]
            capture_trace=True,
        )
        .run()
        .trace
    )
    unfinished = (
        Simulation(
            replace(SimulationConfig(), **SCENARIO_PRESETS["baseline-quorum-floor"]),  # type: ignore[arg-type]
            capture_trace=True,
        )
        .run()
        .trace
    )
    assert finished is not None and unfinished is not None

    last_finished = finished.frames[-1].timestamp
    last_unfinished = unfinished.frames[-1].timestamp
    # The premise of the pairing: one run ends long before the other.
    assert last_unfinished > last_finished * 2.0

    final_index = len(finished.frames) - 1
    for timestamp in (last_finished, last_finished + 1.0, last_unfinished):
        assert aligned_frame_index(finished, timestamp) == final_index

    # Meanwhile the failing run is still producing new frames at those instants.
    assert aligned_frame_index(unfinished, last_finished) < len(unfinished.frames) - 1


def test_an_instant_before_the_first_frame_selects_the_first_frame() -> None:
    trace = (
        Simulation(
            replace(SimulationConfig(), **SCENARIO_PRESETS["demo-partition"]),  # type: ignore[arg-type]
            capture_trace=True,
        )
        .run()
        .trace
    )
    assert trace is not None

    assert aligned_frame_index(trace, -5.0) == 0


def test_the_view_states_where_its_data_came_from() -> None:
    """An unlabelled leak is the failure mode; say which mode is active."""

    assert "Post-mission replay" in REPLAY_PROVENANCE
    assert "each UAV's own recorded log" in REPLAY_PROVENANCE
    assert "evaluator-computed" in REPLAY_PROVENANCE


# --------------------------------------------------------------------------
# contracted posture on screen
# --------------------------------------------------------------------------


def test_every_mission_phase_has_a_colour() -> None:
    from eudis_swarm.autonomy import MissionPhase
    from eudis_swarm.dashboard_app import PHASE_COLOURS

    assert {phase.value for phase in MissionPhase} <= set(PHASE_COLOURS)
    assert "UNAVAILABLE" in PHASE_COLOURS


def test_the_phase_label_names_the_intent_version_behind_it() -> None:
    from eudis_swarm.dashboard_app import phase_label
    from eudis_swarm.trace import TraceAgentState

    def agent(phase: str, version: int | None) -> TraceAgentState:
        return TraceAgentState(
            agent_id=1,
            physical_state="ACTIVE",
            coordinator_state="ACTIVE",
            responsive=True,
            failure_detected=False,
            current_task=None,
            position=(0.0, 0.0),
            neighbor_ids=(),
            fresh_peer_count=0,
            stale_peer_count=0,
            unknown_peer_count=0,
            peer_knowledge=(),
            coordination_mode="COOPERATIVE",
            contact_states=(),
            mission_phase=phase,
            contract_version=version,
        )

    assert phase_label(agent("HOLDING", 2)) == "HOLDING (contract v2)"
    assert phase_label(agent("EXECUTING", None)) == "EXECUTING (uncontracted)"


def test_divergent_intent_is_visible_on_screen() -> None:
    """Two aircraft holding different versions must read differently."""

    from dataclasses import replace as _replace

    from eudis_swarm.config import SimulationConfig
    from eudis_swarm.dashboard_app import phase_label
    from eudis_swarm.simulation import SCENARIO_PRESETS, Simulation

    config = _replace(SimulationConfig(), **SCENARIO_PRESETS["demo-amendment"])  # type: ignore[arg-type]
    trace = Simulation(config, capture_trace=True).run().trace
    assert trace is not None

    labels_by_frame = [
        {phase_label(agent) for agent in frame.agents} for frame in trace.frames
    ]
    mixed = [labels for labels in labels_by_frame if len(labels) > 1]

    assert mixed, "no frame showed the swarm holding differing intent"
    assert any(
        any("v1" in label for label in labels)
        and any("v2" in label for label in labels)
        for labels in mixed
    )
