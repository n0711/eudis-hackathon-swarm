"""Streamlit/Plotly trace playback interface for the swarm simulator."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Iterable

import plotly.graph_objects as go
import streamlit as st

from eudis_swarm.trace import (
    SimulationTrace,
    TraceAgentState,
    TraceEvent,
    TraceFrame,
    TraceTransition,
)

COMPONENT_COLORS = ("#2F6FED", "#E58E26", "#2A9D8F", "#8E5BB7", "#64748B")
CATEGORY_COLORS = {
    "MISSION": "#2F6FED",
    "ALLOCATION": "#6D5BD0",
    "TASK": "#2A9D8F",
    "NETWORK": "#E58E26",
    "PEER": "#64748B",
    "AUTONOMY": "#0F766E",
    "FAILURE": "#C44536",
    "RECOVERY": "#A16207",
}

TraceActivity = TraceEvent | TraceTransition


def _initial_trace_path() -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--trace", default="trace.json")
    arguments, _ = parser.parse_known_args(sys.argv[1:])
    return Path(arguments.trace)


@st.cache_data(show_spinner=False)
def _read_trace_path(path: str) -> SimulationTrace:
    return SimulationTrace.read_json(path)


@st.cache_data(show_spinner=False)
def _read_trace_bytes(payload: bytes) -> SimulationTrace:
    return SimulationTrace.from_dict(json.loads(payload.decode("utf-8")))


def _all_events(trace: SimulationTrace) -> tuple[TraceEvent, ...]:
    return tuple(event for frame in trace.frames for event in frame.events)


def _all_transitions(trace: SimulationTrace) -> tuple[TraceTransition, ...]:
    return tuple(
        transition for frame in trace.frames for transition in frame.transitions
    )


def _activity_category(activity: TraceActivity) -> str:
    return "AUTONOMY" if isinstance(activity, TraceTransition) else activity.category


def _activity_message(activity: TraceActivity) -> str:
    if isinstance(activity, TraceEvent):
        return activity.message
    return (
        f"UAV {activity.observer_agent_id} · {activity.machine}: "
        f"{activity.previous_state} → {activity.next_state} · {activity.event}"
    )


def _activity_sort_key(activity: TraceActivity) -> tuple[float, int, int, int]:
    if isinstance(activity, TraceTransition):
        return (
            activity.timestamp,
            1,
            activity.observer_agent_id,
            activity.sequence,
        )
    return (activity.timestamp, 0, activity.agent_id or 0, 0)


def _agent(frame: TraceFrame, agent_id: int) -> TraceAgentState:
    return next(agent for agent in frame.agents if agent.agent_id == agent_id)


def _component_index(frame: TraceFrame, agent_id: int) -> int:
    return next(
        index
        for index, component in enumerate(frame.components)
        if agent_id in component
    )


def _mission_figure(
    trace: SimulationTrace,
    frame_index: int,
    *,
    show_paths: bool,
    show_links: bool,
    inspected_agent_id: int,
) -> go.Figure:
    frame = trace.frames[frame_index]
    figure = go.Figure()
    if show_paths:
        for agent in frame.agents:
            history = [
                _agent(item, agent.agent_id).position
                for item in trace.frames[: frame_index + 1]
            ]
            figure.add_trace(
                go.Scatter(
                    x=[position[0] for position in history],
                    y=[position[1] for position in history],
                    mode="lines",
                    line={"width": 1, "color": "rgba(71,85,105,0.45)"},
                    hoverinfo="skip",
                    name=f"UAV {agent.agent_id} path",
                    showlegend=False,
                )
            )
    if show_links:
        positions = {agent.agent_id: agent.position for agent in frame.agents}
        for link in frame.active_links:
            source = positions[link.source_agent_id]
            destination = positions[link.destination_agent_id]
            figure.add_trace(
                go.Scatter(
                    x=[source[0], destination[0]],
                    y=[source[1], destination[1]],
                    mode="lines",
                    line={"width": 1.5, "color": "#64748B", "dash": "dash"},
                    text=[
                        f"UAV {link.source_agent_id} ↔ UAV {link.destination_agent_id}<br>"
                        f"Distance: {link.distance:.2f}<br>Active: YES"
                    ]
                    * 2,
                    hoverinfo="text",
                    name="Active communication link",
                    showlegend=False,
                )
            )

    for state, symbol, color, opacity in (
        ("UNASSIGNED", "circle-open", "#D97706", 0.9),
        ("ASSIGNED", "diamond", "#7C3AED", 1.0),
        ("COMPLETED", "circle", "#16A34A", 0.28),
    ):
        tasks = [task for task in frame.tasks if task.state == state]
        if not tasks:
            continue
        figure.add_trace(
            go.Scatter(
                x=[task.position[0] for task in tasks],
                y=[task.position[1] for task in tasks],
                mode="markers",
                marker={
                    "symbol": symbol,
                    "size": 9 if state != "ASSIGNED" else 11,
                    "color": color,
                    "opacity": opacity,
                    "line": {"width": 1.5, "color": color},
                },
                text=[
                    f"Task {task.task_id}<br>State: {task.state}<br>"
                    f"Owner: {'—' if task.assigned_agent_id is None else f'UAV {task.assigned_agent_id}'}<br>"
                    f"Position: {task.position[0]:.1f}, {task.position[1]:.1f}"
                    for task in tasks
                ],
                hoverinfo="text",
                name=state.title(),
            )
        )

    for agent in frame.agents:
        component = _component_index(frame, agent.agent_id)
        selected = agent.agent_id == inspected_agent_id
        failed = agent.physical_state == "FAILED"
        isolated = not agent.neighbor_ids and len(frame.agents) > 1
        symbol = "x" if failed else ("triangle-up" if agent.current_task else "circle")
        label = f"UAV {agent.agent_id}"
        if failed:
            label += "<br>FAILED"
        elif isolated:
            label += "<br>HEALTHY · ISOLATED"
        figure.add_trace(
            go.Scatter(
                x=[agent.position[0]],
                y=[agent.position[1]],
                mode="markers+text",
                marker={
                    "symbol": symbol,
                    "size": 22 if selected else 17,
                    "color": "#C44536"
                    if failed
                    else COMPONENT_COLORS[component % len(COMPONENT_COLORS)],
                    "line": {"width": 3 if selected else 1.5, "color": "#111827"},
                },
                text=[label],
                textposition="top center",
                textfont={"size": 11},
                hovertext=[
                    f"UAV {agent.agent_id}<br>Physical: {agent.physical_state}<br>"
                    f"Coordinator: {agent.coordinator_state}<br>"
                    f"Coordination: {agent.coordination_mode}<br>"
                    f"Task: {'—' if agent.current_task is None else agent.current_task}<br>"
                    f"Position: {agent.position[0]:.1f}, {agent.position[1]:.1f}<br>"
                    f"Neighbors: {len(agent.neighbor_ids)}"
                ],
                hoverinfo="text",
                name=f"UAV {agent.agent_id}",
                showlegend=False,
            )
        )
        if len(frame.components) > 1:
            figure.add_trace(
                go.Scatter(
                    x=[agent.position[0]],
                    y=[agent.position[1]],
                    mode="markers",
                    marker={
                        "symbol": "circle-open",
                        "size": 31,
                        "color": COMPONENT_COLORS[component % len(COMPONENT_COLORS)],
                        "line": {"width": 2},
                    },
                    hoverinfo="skip",
                    showlegend=False,
                )
            )

    figure.update_layout(
        title={"text": f"Mission map · t={frame.timestamp:.2f} s", "x": 0.01},
        height=530,
        margin={"l": 20, "r": 20, "t": 48, "b": 20},
        plot_bgcolor="#F8FAFC",
        paper_bgcolor="white",
        hovermode="closest",
        legend={"orientation": "h", "y": -0.08},
        xaxis={
            "range": [-5, trace.metadata.area_width + 5],
            "title": "x",
            "gridcolor": "#E2E8F0",
        },
        yaxis={
            "range": [-5, trace.metadata.area_height + 8],
            "title": "y",
            "gridcolor": "#E2E8F0",
            "scaleanchor": "x",
            "scaleratio": 1,
        },
    )
    return figure


def _network_figure(frame: TraceFrame) -> go.Figure:
    count = len(frame.agents)
    positions = {
        agent.agent_id: (
            math.cos(2 * math.pi * index / count + math.pi / 2),
            math.sin(2 * math.pi * index / count + math.pi / 2),
        )
        for index, agent in enumerate(frame.agents)
    }
    figure = go.Figure()
    for link in frame.active_links:
        source = positions[link.source_agent_id]
        destination = positions[link.destination_agent_id]
        figure.add_trace(
            go.Scatter(
                x=[source[0], destination[0]],
                y=[source[1], destination[1]],
                mode="lines",
                line={"width": 2, "color": "#64748B"},
                hoverinfo="skip",
                showlegend=False,
            )
        )
    for agent in frame.agents:
        component = _component_index(frame, agent.agent_id)
        failed = agent.physical_state == "FAILED"
        isolated = not agent.neighbor_ids and count > 1
        detail = (
            "FAILED"
            if failed
            else ("HEALTHY · ISOLATED" if isolated else f"Component {component + 1}")
        )
        figure.add_trace(
            go.Scatter(
                x=[positions[agent.agent_id][0]],
                y=[positions[agent.agent_id][1]],
                mode="markers+text",
                marker={
                    "symbol": "x"
                    if failed
                    else ("square-open" if isolated else "circle"),
                    "size": 24,
                    "color": "#C44536"
                    if failed
                    else COMPONENT_COLORS[component % len(COMPONENT_COLORS)],
                    "line": {"width": 2},
                },
                text=[f"UAV {agent.agent_id}<br>{detail}"],
                textposition="bottom center",
                hovertext=[
                    f"UAV {agent.agent_id}<br>{detail}<br>"
                    f"Coordination: {agent.coordination_mode}<br>"
                    f"Neighbors: {len(agent.neighbor_ids)}"
                ],
                hoverinfo="text",
                showlegend=False,
            )
        )
    figure.update_layout(
        title={"text": "Communication topology", "x": 0.01},
        height=310,
        margin={"l": 10, "r": 10, "t": 45, "b": 10},
        plot_bgcolor="white",
        paper_bgcolor="white",
        xaxis={"visible": False, "range": [-1.45, 1.45]},
        yaxis={"visible": False, "range": [-1.45, 1.45], "scaleanchor": "x"},
    )
    return figure


def _activity_at_or_before(
    events: tuple[TraceEvent, ...],
    transitions: tuple[TraceTransition, ...],
    timestamp: float,
) -> TraceActivity | None:
    visible: list[TraceActivity] = [
        activity
        for activity in (*events, *transitions)
        if activity.timestamp <= timestamp
    ]
    if not visible:
        return None
    current = [activity for activity in visible if activity.timestamp == timestamp]
    for category in (
        "AUTONOMY",
        "ALLOCATION",
        "FAILURE",
        "RECOVERY",
        "NETWORK",
        "PEER",
        "TASK",
        "MISSION",
    ):
        matching = [
            activity for activity in current if _activity_category(activity) == category
        ]
        if matching:
            return max(matching, key=_activity_sort_key)
    return max(visible, key=_activity_sort_key)


def _decision_panel(activity: TraceActivity | None, frame: TraceFrame) -> None:
    st.subheader("Current decision / event")
    if activity is None:
        st.info("No event has occurred yet.")
        return
    if isinstance(activity, TraceTransition):
        st.caption(f"{activity.timestamp:05.2f} s · AUTONOMY")
        st.markdown(f"### {activity.machine}")
        st.write(
            f"UAV {activity.observer_agent_id}: **{activity.previous_state}** → "
            f"**{activity.next_state}**"
        )
        st.write(f"Event: `{activity.event}`")
        if activity.peer_agent_id is not None:
            st.write(f"Peer: **UAV {activity.peer_agent_id}**")
        if activity.task_id is not None:
            st.write(f"Task: **{activity.task_id}**")
        st.write(f"Guard: {activity.guard}")
        st.caption(activity.reason)
        if activity.effects:
            st.write(f"Requested effects: `{', '.join(activity.effects)}`")
        return

    event = activity
    st.caption(f"{event.timestamp:05.2f} s · {event.category}")
    st.markdown(f"### {event.kind.replace('_', ' ').title()}")
    st.write(event.message)
    if event.policy is not None:
        st.markdown(f"**Policy:** `{event.policy.upper()}`")
    if (
        event.policy is not None
        and event.agent_id is not None
        and event.task_id is not None
    ):
        st.markdown(f"**UAV {event.agent_id} → Task {event.task_id}**")
    elif event.task_id is not None:
        st.write(f"Affected task: **{event.task_id}**")
    if event.distance is not None:
        st.write(f"Travel distance: **{event.distance:.2f}**")
    if event.predicted_peer_degree is not None:
        st.write(f"Predicted peer degree: **{event.predicted_peer_degree}**")
    if event.predicted_isolation is not None:
        st.write(
            f"Predicted isolation: **{'YES' if event.predicted_isolation else 'NO'}**"
        )
    if event.last_heartbeat is not None:
        st.write(f"Last heartbeat: **{event.last_heartbeat:.2f} s**")
        st.write(f"Detected: **{event.timestamp:.2f} s**")
        released = next(
            (
                item
                for item in frame.events
                if item.kind == "TASK_RELEASED"
                and item.agent_id == event.agent_id
                and item.task_id == event.task_id
            ),
            None,
        )
        if released is not None:
            st.write(f"Task released: **{released.task_id}**")
    if event.kind == "AGENT_UNREACHABLE" and event.agent_id is not None:
        agent = _agent(frame, event.agent_id)
        st.write(f"Physical state: **{agent.physical_state}**")


def _status_panel(frame: TraceFrame) -> None:
    st.subheader("UAV status")
    cards = []
    for agent in frame.agents:
        state = agent.physical_state
        if state == "FAILED" and not agent.failure_detected:
            state = "FAILED · UNDETECTED"
        state_class = " failed" if agent.physical_state == "FAILED" else ""
        task = "—" if agent.current_task is None else str(agent.current_task)
        cards.append(
            f'<div class="uav-card{state_class}">'
            f"<div><strong>UAV {agent.agent_id}</strong><b>{state}</b>"
            f"<span>Task {task}</span></div>"
            f"<small>Position ({agent.position[0]:.1f}, {agent.position[1]:.1f}) · "
            f"Neighbors {len(agent.neighbor_ids)}</small>"
            f"<small>Coordination · <strong>{agent.coordination_mode}</strong></small>"
            f"<small>Peer knowledge · F {agent.fresh_peer_count} · "
            f"S {agent.stale_peer_count} · U {agent.unknown_peer_count}</small>"
            "</div>"
        )
    st.markdown("".join(cards), unsafe_allow_html=True)


def _peer_panel(frame: TraceFrame, selected_agent_id: int) -> None:
    selected = _agent(frame, selected_agent_id)
    st.subheader(f"UAV {selected_agent_id} · receiver-local peer knowledge")
    st.caption(
        f"Coordination mode: {selected.coordination_mode}. Positions below are "
        "delivered last-known snapshots, never authoritative peer positions."
    )
    contact_by_peer = {
        contact.peer_agent_id: contact for contact in selected.contact_states
    }
    rows = []
    for peer in selected.peer_knowledge:
        age = None if peer.received_at is None else frame.timestamp - peer.received_at
        contact = contact_by_peer.get(peer.peer_agent_id)
        contact_age = (
            None
            if contact is None or contact.last_rx_at is None
            else frame.timestamp - contact.last_rx_at
        )
        position = peer.last_known_position
        rows.append(
            {
                "Peer": f"UAV {peer.peer_agent_id}",
                "Contact": "—" if contact is None else contact.state,
                "Knowledge": peer.state,
                "Availability": peer.peer_status or "—",
                "Snapshot age": "—" if age is None else f"{age:.2f} s",
                "Contact age": "—" if contact_age is None else f"{contact_age:.2f} s",
                "Misses": "—"
                if contact is None
                else contact.consecutive_expected_misses,
                "Recovery": "—" if contact is None else contact.recovery_count,
                "Last task": "—"
                if peer.last_known_task is None
                else peer.last_known_task,
                "Last-known position": "—"
                if position is None
                else f"({position[0]:.1f}, {position[1]:.1f})",
            }
        )
    st.dataframe(rows, hide_index=True, use_container_width=True)


def _ownership_panel(frame: TraceFrame) -> None:
    st.subheader("Replicated task ownership")
    if not frame.ownership:
        st.caption("No UAV holds ownership evidence for any task yet.")
        return

    if frame.disputed_task_ids:
        disputed = ", ".join(f"Task {task_id}" for task_id in frame.disputed_task_ids)
        st.warning(
            f"Replicas disagree about ownership: {disputed}. "
            "While a partition holds, both halves can believe they own the work."
        )
    else:
        st.caption(
            "Every replica agrees on ownership. Beliefs below are derived from "
            "delivered claims only, never from authoritative task state."
        )

    owners_by_task: dict[int, dict[int, str]] = {}
    for view in frame.ownership:
        label = (
            "—"
            if view.known_owner_agent_id is None
            else f"UAV {view.known_owner_agent_id}"
        )
        if view.contested:
            label += " (contested)"
        owners_by_task.setdefault(view.task_id, {})[view.observer_agent_id] = label

    observer_ids = sorted({view.observer_agent_id for view in frame.ownership})
    rows = []
    for task_id in sorted(owners_by_task):
        row: dict[str, str] = {
            "Task": f"Task {task_id}",
            "Disputed": "YES" if task_id in frame.disputed_task_ids else "",
        }
        for observer_agent_id in observer_ids:
            row[f"UAV {observer_agent_id} believes"] = owners_by_task[task_id].get(
                observer_agent_id, "—"
            )
        rows.append(row)
    st.dataframe(rows, hide_index=True, use_container_width=True)


def _transition_panel(frame: TraceFrame) -> None:
    st.subheader("Autonomy transitions · current frame")
    if not frame.transitions:
        st.caption("No finite control-state change occurred in this frame.")
        return
    st.dataframe(
        [
            {
                "UAV": f"UAV {transition.observer_agent_id}",
                "Sequence": transition.sequence,
                "Machine": transition.machine,
                "Transition": (
                    f"{transition.previous_state} → {transition.next_state}"
                ),
                "Event": transition.event,
                "Guard": transition.guard,
                "Reason": transition.reason,
                "Effects": ", ".join(transition.effects) or "—",
            }
            for transition in frame.transitions
        ],
        hide_index=True,
        use_container_width=True,
    )


def _timeline(
    events: Iterable[TraceEvent],
    transitions: Iterable[TraceTransition],
    duration: float,
    timestamp: float,
) -> go.Figure:
    activities: tuple[TraceActivity, ...] = (*events, *transitions)
    categories = tuple(CATEGORY_COLORS)
    y_index = {category: index for index, category in enumerate(categories)}
    figure = go.Figure()
    for category in categories:
        selected = [
            activity
            for activity in activities
            if _activity_category(activity) == category
        ]
        if selected:
            figure.add_trace(
                go.Scatter(
                    x=[activity.timestamp for activity in selected],
                    y=[y_index[category]] * len(selected),
                    mode="markers",
                    marker={"size": 8, "color": CATEGORY_COLORS[category]},
                    text=[_activity_message(activity) for activity in selected],
                    hoverinfo="text+x",
                    name=category.title(),
                )
            )
    figure.add_vline(x=timestamp, line_width=2, line_color="#111827")
    figure.update_layout(
        height=190,
        margin={"l": 15, "r": 15, "t": 25, "b": 20},
        xaxis={"range": [0, duration], "title": "Simulation time (s)"},
        yaxis={
            "tickmode": "array",
            "tickvals": list(y_index.values()),
            "ticktext": [category.title() for category in categories],
        },
        showlegend=False,
        paper_bgcolor="white",
        plot_bgcolor="#F8FAFC",
    )
    return figure


def _metrics_bar(frame: TraceFrame) -> None:
    values = (
        ("Time", f"{frame.timestamp:.2f} s"),
        ("Mission", f"{frame.metrics.completed_tasks}/{frame.metrics.total_tasks}"),
        ("Allocation", frame.metrics.allocation_policy.upper()),
        ("Links", str(frame.metrics.active_links)),
        ("Components", str(frame.metrics.component_count)),
        ("Isolated", str(frame.metrics.isolated_uavs)),
        ("Physical failures", str(frame.metrics.failed_uavs)),
        ("Stale views", str(frame.metrics.stale_peer_observations)),
        ("Disputed tasks", str(frame.metrics.disputed_task_count)),
        (
            "Delivery",
            f"{frame.metrics.messages_delivered}/{frame.metrics.messages_attempted}",
        ),
    )
    cards = "".join(
        f'<div class="trace-metric"><span>{label}</span>'
        f'<strong class="{("long" if label == "Allocation" else "")}">'
        f"{value}</strong></div>"
        for label, value in values
    )
    st.markdown(f'<div class="trace-metrics">{cards}</div>', unsafe_allow_html=True)


def _load_trace() -> tuple[SimulationTrace | None, str]:
    default_path = _initial_trace_path()
    st.sidebar.header("Trace")
    uploaded = st.sidebar.file_uploader("Upload trace JSON", type=("json",))
    path = st.sidebar.text_input("Trace path", value=str(default_path))
    try:
        if uploaded is not None:
            return _read_trace_bytes(uploaded.getvalue()), uploaded.name
        if Path(path).is_file():
            return _read_trace_path(str(Path(path).resolve())), path
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        st.sidebar.error(f"Could not load trace: {error}")
        return None, path
    st.sidebar.info("Generate a trace or upload/select an existing JSON file.")
    return None, path


def render_dashboard() -> None:
    st.set_page_config(page_title="EUDIS Swarm Playback", layout="wide")
    st.markdown(
        """
        <style>
        .block-container {padding-top: 1.25rem; max-width: 1900px;}
        .trace-metrics {display:grid; grid-template-columns:repeat(9,minmax(0,1fr)); gap:0.5rem; margin-bottom:0.7rem;}
        .trace-metric {background:#F8FAFC; border:1px solid #E2E8F0; padding:0.55rem 0.65rem; border-radius:0.35rem; min-width:0;}
        .trace-metric span {display:block; color:#475569; font-size:0.72rem; white-space:nowrap;}
        .trace-metric strong {display:block; color:#0F172A; font-size:1.15rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
        .trace-metric strong.long {font-size:0.68rem; line-height:1.7rem;}
        .uav-card {border:1px solid #E2E8F0; border-left:4px solid #2F6FED; border-radius:0.35rem; padding:0.55rem 0.65rem; margin-bottom:0.5rem; background:#F8FAFC;}
        .uav-card.failed {border-left-color:#C44536; background:#FFF7F5;}
        .uav-card div {display:grid; grid-template-columns:1fr auto auto; gap:0.7rem; align-items:center;}
        .uav-card b {font-size:0.72rem; color:#334155;}
        .uav-card span {font-size:0.8rem; color:#475569;}
        .uav-card small {display:block; color:#475569; margin-top:0.2rem;}
        @media (max-width: 1200px) {.trace-metrics {grid-template-columns:repeat(5,minmax(0,1fr));}}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("EUDIS Swarm · mission playback")
    trace, source_name = _load_trace()
    if trace is None:
        st.code(
            "eudis-swarm --record-trace trace.json\neudis-swarm-dashboard trace.json"
        )
        return

    trace_key = f"{source_name}:{trace.metadata.seed}:{len(trace.frames)}"
    if st.session_state.get("trace_key") != trace_key:
        st.session_state.trace_key = trace_key
        st.session_state.frame_index = 0
        st.session_state.playing = False

    events = _all_events(trace)
    transitions = _all_transitions(trace)
    header = st.container()
    main_area = st.container()
    lower_area = st.container()
    controls = st.container()
    timeline_area = st.container()
    peer_area = st.container()
    event_area = st.container()

    with controls:
        st.subheader("Playback")
        previous_event, previous_frame, play, pause, next_frame, next_event = (
            st.columns(6)
        )
        current_index = int(st.session_state.frame_index)
        if previous_event.button("◀ Event", use_container_width=True):
            prior = [
                index
                for index, frame in enumerate(trace.frames)
                if (frame.events or frame.transitions) and index < current_index
            ]
            st.session_state.frame_index = prior[-1] if prior else 0
        if previous_frame.button("◀ Frame", use_container_width=True):
            st.session_state.frame_index = max(0, current_index - 1)
        if play.button("▶ Play", use_container_width=True):
            st.session_state.playing = True
        if pause.button("Ⅱ Pause", use_container_width=True):
            st.session_state.playing = False
        if next_frame.button("Frame ▶", use_container_width=True):
            st.session_state.frame_index = min(len(trace.frames) - 1, current_index + 1)
        if next_event.button("Event ▶", use_container_width=True):
            following = [
                index
                for index, frame in enumerate(trace.frames)
                if (frame.events or frame.transitions) and index > current_index
            ]
            st.session_state.frame_index = (
                following[0] if following else len(trace.frames) - 1
            )
        speed = st.selectbox(
            "Speed",
            (0.25, 0.5, 1.0, 2.0, 4.0),
            index=2,
            format_func=lambda value: f"{value:g}×",
            width=120,
        )
        st.slider(
            "Simulation frame",
            min_value=0,
            max_value=len(trace.frames) - 1,
            key="frame_index",
            format="%d",
        )

    frame_index = int(st.session_state.frame_index)
    frame = trace.frames[frame_index]
    agent_ids = [agent.agent_id for agent in frame.agents]
    with st.sidebar:
        st.caption(
            f"Prototype {trace.metadata.prototype} · seed {trace.metadata.seed}\n\n"
            f"{trace.metadata.agent_count} UAVs · {trace.metadata.task_count} tasks · "
            f"{trace.metadata.duration:.2f} s"
        )
        selected_agent_id = st.selectbox(
            "Inspect UAV", agent_ids, format_func=lambda value: f"UAV {value}"
        )
        show_paths = st.toggle("Show paths", value=True)
        show_links = st.toggle("Show communication links", value=True)

    with header:
        _metrics_bar(frame)

    with main_area:
        map_column, status_column = st.columns([2.05, 1])
        map_column.plotly_chart(
            _mission_figure(
                trace,
                frame_index,
                show_paths=show_paths,
                show_links=show_links,
                inspected_agent_id=selected_agent_id,
            ),
            use_container_width=True,
            config={"displaylogo": False},
        )
        with status_column:
            _status_panel(frame)

    with lower_area:
        network_column, decision_column = st.columns([2.05, 1])
        network_column.plotly_chart(
            _network_figure(frame),
            use_container_width=True,
            config={"displaylogo": False},
        )
        with decision_column:
            _decision_panel(
                _activity_at_or_before(events, transitions, frame.timestamp),
                frame,
            )

    with timeline_area:
        st.subheader("Event timeline")
        st.plotly_chart(
            _timeline(
                events,
                transitions,
                trace.metadata.duration,
                frame.timestamp,
            ),
            use_container_width=True,
            config={"displaylogo": False},
        )

    with peer_area:
        _peer_panel(frame, selected_agent_id)
        _ownership_panel(frame)
        _transition_panel(frame)

    with event_area:
        st.subheader("Structured event log")
        selected_categories = st.multiselect(
            "Categories",
            tuple(CATEGORY_COLORS),
            default=tuple(CATEGORY_COLORS),
        )
        visible_activities: list[TraceActivity] = [
            activity
            for activity in (*events, *transitions)
            if activity.timestamp <= frame.timestamp
            and _activity_category(activity) in selected_categories
        ]
        visible_activities.sort(key=_activity_sort_key)
        st.dataframe(
            [
                {
                    "Time": f"{activity.timestamp:05.2f}",
                    "Category": _activity_category(activity),
                    "Event": _activity_message(activity),
                }
                for activity in reversed(visible_activities[-100:])
            ],
            hide_index=True,
            use_container_width=True,
        )

    if st.session_state.playing:
        if frame_index >= len(trace.frames) - 1:
            st.session_state.playing = False
        else:
            time.sleep(0.5 / float(speed))
            st.session_state.frame_index = frame_index + 1
            st.rerun()


if __name__ == "__main__":
    render_dashboard()
