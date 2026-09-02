"""Capture immutable, JSON-serializable playback traces for inspection.

Trace data may observe world truth and agent belief side by side but never feeds
either representation back into simulation decisions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .agent import AgentStatus, Position
from .mission import MissionEvent, MissionEventKind
from .peer_state import PeerKnowledgeState, PeerStateStore
from .simulation_events import (
    CommunicationEvent,
    PeerStateEvent,
    PeerStateEventKind,
)

if TYPE_CHECKING:
    from .communication import CommunicationGraph
    from .config import SimulationConfig
    from .mission import Mission


TRACE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class TraceMetadata:
    prototype: str
    seed: int
    allocation_policy: str
    agent_count: int
    task_count: int
    area_width: float
    area_height: float
    duration: float


@dataclass(frozen=True, slots=True)
class TraceEvent:
    timestamp: float
    category: str
    kind: str
    message: str
    agent_id: int | None = None
    peer_agent_id: int | None = None
    task_id: int | None = None
    policy: str | None = None
    distance: float | None = None
    predicted_peer_degree: int | None = None
    predicted_isolation: bool | None = None
    component_count: int | None = None
    last_heartbeat: float | None = None


@dataclass(frozen=True, slots=True)
class TracePeerKnowledge:
    peer_agent_id: int
    state: str
    snapshot_timestamp: float | None
    received_at: float | None
    last_known_position: Position | None
    last_known_status: str | None
    last_known_task: int | None
    peer_status: str | None = None


@dataclass(frozen=True, slots=True)
class TraceAgentState:
    agent_id: int
    physical_state: str
    coordinator_state: str
    responsive: bool
    failure_detected: bool
    current_task: int | None
    position: Position
    neighbor_ids: tuple[int, ...]
    fresh_peer_count: int
    stale_peer_count: int
    unknown_peer_count: int
    peer_knowledge: tuple[TracePeerKnowledge, ...]


@dataclass(frozen=True, slots=True)
class TraceTaskState:
    task_id: int
    state: str
    assigned_agent_id: int | None
    position: Position


@dataclass(frozen=True, slots=True)
class TraceLink:
    source_agent_id: int
    destination_agent_id: int
    distance: float


@dataclass(frozen=True, slots=True)
class TraceMetrics:
    completed_tasks: int
    total_tasks: int
    failed_uavs: int
    active_links: int
    component_count: int
    isolated_uavs: int
    stale_peer_observations: int
    messages_attempted: int
    messages_delivered: int
    messages_dropped: int
    allocation_policy: str


@dataclass(frozen=True, slots=True)
class TraceFrame:
    timestamp: float
    agents: tuple[TraceAgentState, ...]
    tasks: tuple[TraceTaskState, ...]
    active_links: tuple[TraceLink, ...]
    components: tuple[tuple[int, ...], ...]
    metrics: TraceMetrics
    events: tuple[TraceEvent, ...]


@dataclass(frozen=True, slots=True)
class SimulationTrace:
    schema_version: int
    metadata: TraceMetadata
    frames: tuple[TraceFrame, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the stable public JSON representation."""

        return asdict(self)

    def write_json(self, path: str | Path) -> None:
        """Write a human-readable trace artifact without changing simulation state."""

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read_json(cls, path: str | Path) -> SimulationTrace:
        with Path(path).open(encoding="utf-8") as source:
            return cls.from_dict(json.load(source))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SimulationTrace:
        schema_version = int(data["schema_version"])
        if schema_version != TRACE_SCHEMA_VERSION:
            raise ValueError(f"unsupported trace schema version: {schema_version}")
        metadata = TraceMetadata(**data["metadata"])
        frames = tuple(_frame_from_dict(frame) for frame in data["frames"])
        if not frames:
            raise ValueError("a simulation trace must contain at least one frame")
        if any(
            current.timestamp < previous.timestamp
            for previous, current in zip(frames, frames[1:], strict=False)
        ):
            raise ValueError("trace frame timestamps must be monotonic")
        return cls(schema_version=schema_version, metadata=metadata, frames=frames)


def _position(value: list[float] | tuple[float, float] | None) -> Position | None:
    if value is None:
        return None
    return (float(value[0]), float(value[1]))


def _frame_from_dict(data: Mapping[str, Any]) -> TraceFrame:
    agents = []
    for agent_data in data["agents"]:
        peer_knowledge = tuple(
            TracePeerKnowledge(
                **{
                    **peer_data,
                    "last_known_position": _position(peer_data["last_known_position"]),
                }
            )
            for peer_data in agent_data["peer_knowledge"]
        )
        agents.append(
            TraceAgentState(
                **{
                    **agent_data,
                    "position": _position(agent_data["position"]),
                    "neighbor_ids": tuple(agent_data["neighbor_ids"]),
                    "peer_knowledge": peer_knowledge,
                }
            )
        )
    tasks = tuple(
        TraceTaskState(**{**item, "position": _position(item["position"])})
        for item in data["tasks"]
    )
    return TraceFrame(
        timestamp=float(data["timestamp"]),
        agents=tuple(agents),
        tasks=tasks,
        active_links=tuple(TraceLink(**item) for item in data["active_links"]),
        components=tuple(tuple(component) for component in data["components"]),
        metrics=TraceMetrics(**data["metrics"]),
        events=tuple(TraceEvent(**item) for item in data["events"]),
    )


class TraceRecorder:
    """Capture post-boundary state without participating in simulation decisions."""

    def __init__(self, config: SimulationConfig) -> None:
        self._config = config
        self._frames: list[TraceFrame] = []
        self._mission_event_count = 0
        self._communication_event_count = 0
        self._peer_event_count = 0

    def capture(
        self,
        mission: Mission,
        graph: CommunicationGraph,
        peer_stores: Mapping[int, PeerStateStore],
        communication_events: list[CommunicationEvent],
        peer_events: list[PeerStateEvent],
        timestamp: float,
        positions: Mapping[int, Position] | None = None,
    ) -> None:
        new_events = self._collect_events(mission, communication_events, peer_events)
        agents = tuple(
            self._agent_state(agent.agent_id, mission, graph, peer_stores, positions)
            for agent in mission.ordered_agents
        )
        frame = TraceFrame(
            timestamp=timestamp,
            agents=agents,
            tasks=tuple(
                TraceTaskState(
                    task_id=task.task_id,
                    state=task.status.value,
                    assigned_agent_id=task.assigned_agent,
                    position=task.position,
                )
                for task in sorted(
                    mission.tasks.values(), key=lambda item: item.task_id
                )
            ),
            active_links=tuple(
                TraceLink(
                    source_agent_id=link.source_agent_id,
                    destination_agent_id=link.destination_agent_id,
                    distance=link.distance,
                )
                for link in graph.active_links
            ),
            components=tuple(
                tuple(sorted(component)) for component in graph.connected_components
            ),
            metrics=TraceMetrics(
                completed_tasks=mission.metrics.completed_task_count,
                total_tasks=mission.metrics.total_task_count,
                failed_uavs=sum(agent.physical_state == "FAILED" for agent in agents),
                active_links=graph.link_count,
                component_count=len(graph.connected_components),
                isolated_uavs=len(graph.isolated_agent_ids),
                stale_peer_observations=sum(agent.stale_peer_count for agent in agents),
                messages_attempted=mission.metrics.peer_messages_attempted,
                messages_delivered=mission.metrics.peer_messages_delivered,
                messages_dropped=mission.metrics.peer_messages_undelivered,
                allocation_policy=mission.metrics.allocation_policy,
            ),
            events=new_events,
        )
        if self._frames and self._frames[-1].timestamp == timestamp:
            previous_events = self._frames[-1].events
            self._frames[-1] = replace(frame, events=previous_events + frame.events)
        else:
            self._frames.append(frame)

    def finish(self, mission: Mission) -> SimulationTrace:
        duration = mission.metrics.simulation_duration
        return SimulationTrace(
            schema_version=TRACE_SCHEMA_VERSION,
            metadata=TraceMetadata(
                prototype="0.3A",
                seed=self._config.random_seed,
                allocation_policy=self._config.allocation_policy,
                agent_count=self._config.agent_count,
                task_count=self._config.task_count,
                area_width=self._config.area_width,
                area_height=self._config.area_height,
                duration=duration,
            ),
            frames=tuple(self._frames),
        )

    def _agent_state(
        self,
        agent_id: int,
        mission: Mission,
        graph: CommunicationGraph,
        peer_stores: Mapping[int, PeerStateStore],
        positions: Mapping[int, Position] | None,
    ) -> TraceAgentState:
        agent = mission.agents[agent_id]
        store = peer_stores[agent_id]
        knowledge = tuple(
            self._peer_knowledge(store, peer_id) for peer_id in store.peer_agent_ids
        )
        counts = {
            state: sum(item.state == state.value for item in knowledge)
            for state in PeerKnowledgeState
        }
        return TraceAgentState(
            agent_id=agent_id,
            physical_state=(
                AgentStatus.FAILED.value if not agent.responsive else agent.status.value
            ),
            coordinator_state=agent.status.value,
            responsive=agent.responsive,
            failure_detected=agent.status is AgentStatus.FAILED,
            current_task=agent.current_task,
            position=agent.position if positions is None else positions[agent_id],
            neighbor_ids=tuple(sorted(graph.neighbors(agent_id))),
            fresh_peer_count=counts[PeerKnowledgeState.FRESH],
            stale_peer_count=counts[PeerKnowledgeState.STALE],
            unknown_peer_count=counts[PeerKnowledgeState.UNKNOWN],
            peer_knowledge=knowledge,
        )

    @staticmethod
    def _peer_knowledge(store: PeerStateStore, peer_id: int) -> TracePeerKnowledge:
        state = store.state_for(peer_id)
        observation = store.observation_for(peer_id)
        snapshot = None if observation is None else observation.snapshot
        return TracePeerKnowledge(
            peer_agent_id=peer_id,
            state=state.value,
            snapshot_timestamp=None if snapshot is None else snapshot.timestamp,
            received_at=None if observation is None else observation.received_at,
            last_known_position=None if snapshot is None else snapshot.position,
            last_known_status=None if snapshot is None else snapshot.status.value,
            last_known_task=None if snapshot is None else snapshot.current_task,
            peer_status=store.status_for(peer_id).value,
        )

    def _collect_events(
        self,
        mission: Mission,
        communication_events: list[CommunicationEvent],
        peer_events: list[PeerStateEvent],
    ) -> tuple[TraceEvent, ...]:
        allocations = {
            (
                record.timestamp,
                record.allocation.agent_id,
                record.allocation.task_id,
            ): record.allocation
            for record in mission.metrics.allocation_decisions
        }
        mission_events = mission.events[self._mission_event_count :]
        communication_delta = communication_events[self._communication_event_count :]
        peer_delta = peer_events[self._peer_event_count :]
        self._mission_event_count = len(mission.events)
        self._communication_event_count = len(communication_events)
        self._peer_event_count = len(peer_events)

        events = [
            _mission_trace_event(event, mission, allocations)
            for event in mission_events
        ]
        events.extend(
            _communication_trace_event(event) for event in communication_delta
        )
        events.extend(_peer_trace_event(event) for event in peer_delta)
        return tuple(sorted(events, key=lambda event: (event.timestamp, event.kind)))


def _mission_trace_event(
    event: MissionEvent,
    mission: Mission,
    allocations: Mapping[tuple[float, int, int], Any],
) -> TraceEvent:
    category_by_kind = {
        MissionEventKind.MISSION_STARTED: "MISSION",
        MissionEventKind.MISSION_COMPLETED: "MISSION",
        MissionEventKind.MISSION_TIMED_OUT: "MISSION",
        MissionEventKind.TASK_ASSIGNED: "ALLOCATION",
        MissionEventKind.TASK_REASSIGNED: "RECOVERY",
        MissionEventKind.TASK_COMPLETED: "TASK",
        MissionEventKind.FAILURE_INJECTED: "FAILURE",
        MissionEventKind.HEARTBEAT_TIMEOUT: "FAILURE",
        MissionEventKind.FAILURE_DECLARED: "FAILURE",
        MissionEventKind.TASK_DUPLICATED: "DIVERGENCE",
        MissionEventKind.FAILURE_RETRACTED: "RECOVERY",
        MissionEventKind.TASK_YIELDED: "RECOVERY",
        MissionEventKind.TASK_RELEASED: "RECOVERY",
    }
    message = event.kind.value.replace("_", " ").title()
    if event.agent_id is not None:
        message += f" — UAV {event.agent_id}"
    if event.task_id is not None:
        message += f" / Task {event.task_id}"
    allocation = None
    if event.agent_id is not None and event.task_id is not None:
        allocation = allocations.get((event.timestamp, event.agent_id, event.task_id))
    last_heartbeat = None
    if event.kind is MissionEventKind.HEARTBEAT_TIMEOUT and event.agent_id is not None:
        failure = mission.metrics.failures.get(event.agent_id)
        last_heartbeat = None if failure is None else failure.last_heartbeat
    return TraceEvent(
        timestamp=event.timestamp,
        category=category_by_kind[event.kind],
        kind=event.kind.value,
        message=message,
        agent_id=event.agent_id,
        task_id=event.task_id,
        policy=None if allocation is None else allocation.policy,
        distance=None if allocation is None else allocation.distance,
        predicted_peer_degree=(
            None if allocation is None else allocation.predicted_peer_degree
        ),
        predicted_isolation=(
            None if allocation is None else allocation.predicted_isolation
        ),
        last_heartbeat=last_heartbeat,
    )


def _communication_trace_event(event: CommunicationEvent) -> TraceEvent:
    message = event.kind.value.replace("_", " ").title()
    if event.agent_id is not None:
        message += f" — UAV {event.agent_id}"
    if event.peer_agent_id is not None:
        message += f" ↔ UAV {event.peer_agent_id}"
    return TraceEvent(
        timestamp=event.timestamp,
        category="NETWORK",
        kind=event.kind.value,
        message=message,
        agent_id=event.agent_id,
        peer_agent_id=event.peer_agent_id,
        component_count=event.component_count,
    )


def _peer_trace_event(event: PeerStateEvent) -> TraceEvent:
    action = "became stale" if event.kind is PeerStateEventKind.STALE else "refreshed"
    return TraceEvent(
        timestamp=event.timestamp,
        category="PEER",
        kind=event.kind.value,
        message=(
            f"UAV {event.observer_agent_id} view of UAV {event.peer_agent_id} {action}"
        ),
        agent_id=event.observer_agent_id,
        peer_agent_id=event.peer_agent_id,
    )


__all__ = [
    "SimulationTrace",
    "TRACE_SCHEMA_VERSION",
    "TraceAgentState",
    "TraceEvent",
    "TraceFrame",
    "TraceLink",
    "TraceMetadata",
    "TraceMetrics",
    "TracePeerKnowledge",
    "TraceRecorder",
    "TraceTaskState",
]
