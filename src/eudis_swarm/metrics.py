"""Event-derived mission, network, peer-state, and allocation metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean
from typing import TYPE_CHECKING, Collection

from .agent import Position
from .validation import validate_timestamp

if TYPE_CHECKING:
    from .communication import CommunicationGraph, CommunicationUpdate
    from .task_allocator import Allocation


@dataclass(slots=True)
class FailureRecord:
    agent_id: int
    injected_at: float | None = None
    injection_position: Position | None = None
    detected_at: float | None = None
    last_heartbeat: float | None = None


@dataclass(slots=True)
class RecoveryRecord:
    task_id: int
    failed_agent_id: int
    orphaned_at: float
    reassigned_agent_id: int | None = None
    reassigned_at: float | None = None
    completed_at: float | None = None


@dataclass(frozen=True, slots=True)
class AllocationRecord:
    timestamp: float
    allocation: Allocation


@dataclass(slots=True)
class SimulationMetrics:
    """Metrics calculated from real state transitions in simulated time."""

    total_task_count: int
    agents_started: int
    start_time: float = 0.0
    end_time: float | None = None
    mission_completed: bool = False
    human_interventions: int = 0
    duplicated_task_completion_count: int = 0
    belief_divergence_event_count: int = 0
    declaration_retraction_count: int = 0
    claim_refused_allocation_count: int = 0
    contested_task_yield_count: int = 0
    rejoin_surrendered_task_count: int = 0
    allocation_policy: str = "distance"
    connectivity_aware_assignment_count: int = 0
    predicted_isolation_assignment_count: int = 0
    total_predicted_peer_degree: int = 0
    minimum_predicted_peer_degree: int | None = None
    allocation_decisions: list[AllocationRecord] = field(default_factory=list)
    completed_task_ids: set[int] = field(default_factory=set)
    reinstated_agent_ids: set[int] = field(default_factory=set)
    failures: dict[int, FailureRecord] = field(default_factory=dict)
    recoveries: dict[int, RecoveryRecord] = field(default_factory=dict)
    initial_link_count: int | None = None
    minimum_link_count: int | None = None
    maximum_connected_component_count: int | None = None
    isolation_event_count: int = 0
    link_loss_event_count: int = 0
    link_restoration_event_count: int = 0
    total_communication_degraded_duration: float = 0.0
    network_ended_connected: bool | None = None
    healthy_unreachable_event_count: int = 0
    peer_messages_attempted: int = 0
    peer_messages_delivered: int = 0
    peer_messages_undelivered: int = 0
    peer_state_stale_transition_count: int = 0
    peer_state_refresh_transition_count: int = 0
    maximum_simultaneous_stale_peer_observations: int = 0
    _communication_initialized: bool = field(default=False, init=False, repr=False)
    _last_communication_update: float | None = field(
        default=None, init=False, repr=False
    )
    _previous_network_degraded: bool = field(default=False, init=False, repr=False)
    _communication_finalized: bool = field(default=False, init=False, repr=False)
    _last_event_time: float | None = field(default=None, init=False, repr=False)

    def _observe_time(self, timestamp: float) -> float:
        value = validate_timestamp(
            timestamp,
            previous=self._last_event_time,
            name="metrics timestamp",
        )
        self._last_event_time = value
        return value

    def record_failure_injection(
        self, agent_id: int, timestamp: float, position: Position
    ) -> None:
        timestamp = self._observe_time(timestamp)
        record = self.failures.setdefault(agent_id, FailureRecord(agent_id=agent_id))
        if record.injected_at is None:
            record.injected_at = timestamp
            record.injection_position = position

    def record_failure_detection(
        self, agent_id: int, timestamp: float, last_heartbeat: float
    ) -> None:
        last_heartbeat = validate_timestamp(
            last_heartbeat,
            name="last heartbeat timestamp",
        )
        timestamp = validate_timestamp(timestamp, name="metrics timestamp")
        if last_heartbeat > timestamp:
            raise ValueError("last heartbeat timestamp cannot follow detection")
        timestamp = self._observe_time(timestamp)
        record = self.failures.setdefault(agent_id, FailureRecord(agent_id=agent_id))
        if record.detected_at is None:
            record.detected_at = timestamp
            record.last_heartbeat = last_heartbeat

    def record_orphan(
        self, task_id: int, failed_agent_id: int, timestamp: float
    ) -> None:
        timestamp = self._observe_time(timestamp)
        self.recoveries.setdefault(
            task_id,
            RecoveryRecord(
                task_id=task_id,
                failed_agent_id=failed_agent_id,
                orphaned_at=timestamp,
            ),
        )

    def record_reassignment(
        self, task_id: int, agent_id: int, timestamp: float
    ) -> None:
        timestamp = self._observe_time(timestamp)
        recovery = self.recoveries[task_id]
        if recovery.reassigned_agent_id is None:
            # a UAV whose declaration was withdrawn is not failed any more, so
            # it may legitimately be given back the work it lost.
            if (
                agent_id == recovery.failed_agent_id
                and agent_id not in self.reinstated_agent_ids
            ):
                raise ValueError("an orphaned task cannot return to its failed owner")
            recovery.reassigned_agent_id = agent_id
            recovery.reassigned_at = timestamp

    def record_declaration_retraction(self, agent_id: int) -> None:
        """Record that first-hand contact reinstated a wrongly declared UAV."""

        self.reinstated_agent_ids.add(agent_id)
        self.declaration_retraction_count += 1

    def record_task_completion(self, task_id: int, timestamp: float) -> None:
        timestamp = self._observe_time(timestamp)
        self.completed_task_ids.add(task_id)
        recovery = self.recoveries.get(task_id)
        if recovery is not None and recovery.completed_at is None:
            recovery.completed_at = timestamp

    def record_allocation(self, allocation: Allocation, timestamp: float) -> None:
        """Record explanatory metadata for an applied allocation proposal."""

        self._observe_time(timestamp)
        if allocation.policy != "connectivity":
            self.allocation_decisions.append(
                AllocationRecord(timestamp=timestamp, allocation=allocation)
            )
            return
        if (
            allocation.predicted_peer_degree is None
            or allocation.predicted_isolation is None
        ):
            raise ValueError("connectivity allocations require prediction metadata")
        self.allocation_decisions.append(
            AllocationRecord(timestamp=timestamp, allocation=allocation)
        )
        self.connectivity_aware_assignment_count += 1
        self.total_predicted_peer_degree += allocation.predicted_peer_degree
        if allocation.predicted_isolation:
            self.predicted_isolation_assignment_count += 1
        if self.minimum_predicted_peer_degree is None:
            self.minimum_predicted_peer_degree = allocation.predicted_peer_degree
        else:
            self.minimum_predicted_peer_degree = min(
                self.minimum_predicted_peer_degree,
                allocation.predicted_peer_degree,
            )

    def record_communication_update(
        self,
        timestamp: float,
        graph: CommunicationGraph,
        update: CommunicationUpdate,
        healthy_unreachable_agent_ids: Collection[int],
    ) -> None:
        """Record one observed topology and its transition from the prior state."""

        if self._communication_finalized:
            raise RuntimeError("communication metrics have already been finalized")
        timestamp = self._observe_time(timestamp)

        component_count = len(graph.connected_components)
        healthy_unreachable_count = len(set(healthy_unreachable_agent_ids))

        if not self._communication_initialized:
            self.initial_link_count = graph.link_count
            self.minimum_link_count = graph.link_count
            self.maximum_connected_component_count = component_count
            self.network_ended_connected = graph.is_fully_connected
            self._communication_initialized = True
            self._last_communication_update = timestamp
            self._previous_network_degraded = not graph.is_fully_connected
            return

        previous_timestamp = self._last_communication_update
        if previous_timestamp is None:
            raise RuntimeError("communication metric lifecycle is inconsistent")
        if timestamp < previous_timestamp:
            raise ValueError("communication update timestamps must be non-decreasing")
        if self._previous_network_degraded:
            self.total_communication_degraded_duration += timestamp - previous_timestamp

        if self.minimum_link_count is None:
            self.minimum_link_count = graph.link_count
        else:
            self.minimum_link_count = min(self.minimum_link_count, graph.link_count)
        if self.maximum_connected_component_count is None:
            self.maximum_connected_component_count = component_count
        else:
            self.maximum_connected_component_count = max(
                self.maximum_connected_component_count, component_count
            )

        self.link_loss_event_count += len(update.lost_links)
        self.link_restoration_event_count += len(update.restored_links)
        self.isolation_event_count += len(update.newly_isolated_agent_ids)
        self.healthy_unreachable_event_count += healthy_unreachable_count
        self.network_ended_connected = graph.is_fully_connected
        self._last_communication_update = timestamp
        self._previous_network_degraded = not graph.is_fully_connected

    def record_peer_message_batch(
        self,
        timestamp: float,
        *,
        attempted: int,
        delivered: int,
        undelivered: int,
    ) -> None:
        """Record one graph-mediated state-publication batch."""

        self._observe_time(timestamp)
        if min(attempted, delivered, undelivered) < 0:
            raise ValueError("peer message counts must be non-negative")
        if attempted != delivered + undelivered:
            raise ValueError("attempted peer messages must equal delivery outcomes")
        self.peer_messages_attempted += attempted
        self.peer_messages_delivered += delivered
        self.peer_messages_undelivered += undelivered

    def record_peer_state_transitions(
        self,
        timestamp: float,
        *,
        stale_transitions: int = 0,
        refresh_transitions: int = 0,
        simultaneous_stale: int,
    ) -> None:
        """Record receiver-local freshness changes without changing mission state."""

        self._observe_time(timestamp)
        if min(stale_transitions, refresh_transitions, simultaneous_stale) < 0:
            raise ValueError("peer-state metric counts must be non-negative")
        self.peer_state_stale_transition_count += stale_transitions
        self.peer_state_refresh_transition_count += refresh_transitions
        self.maximum_simultaneous_stale_peer_observations = max(
            self.maximum_simultaneous_stale_peer_observations,
            simultaneous_stale,
        )

    def finish(self, timestamp: float, mission_completed: bool) -> None:
        timestamp = self._observe_time(timestamp)
        if self._communication_initialized and not self._communication_finalized:
            previous_timestamp = self._last_communication_update
            if previous_timestamp is None:
                raise RuntimeError("communication metric lifecycle is inconsistent")
            if timestamp < previous_timestamp:
                raise ValueError("mission cannot finish before the last network update")
            if self._previous_network_degraded:
                self.total_communication_degraded_duration += (
                    timestamp - previous_timestamp
                )
            self._last_communication_update = timestamp
            self._communication_finalized = True
        self.end_time = timestamp
        self.mission_completed = mission_completed

    @property
    def simulation_duration(self) -> float:
        if self.end_time is None:
            return 0.0
        return self.end_time - self.start_time

    @property
    def completed_task_count(self) -> int:
        return len(self.completed_task_ids)

    @property
    def failed_agent_count(self) -> int:
        return sum(record.detected_at is not None for record in self.failures.values())

    @property
    def orphaned_task_count(self) -> int:
        return len(self.recoveries)

    @property
    def reassigned_task_count(self) -> int:
        return sum(
            recovery.reassigned_agent_id is not None
            for recovery in self.recoveries.values()
        )

    @property
    def recovered_task_count(self) -> int:
        """Count orphaned tasks completed after assignment to a survivor."""

        return sum(
            recovery.reassigned_agent_id is not None
            and recovery.completed_at is not None
            for recovery in self.recoveries.values()
        )

    @property
    def failure_detection_latencies(self) -> dict[int, float]:
        return {
            agent_id: record.detected_at - record.injected_at
            for agent_id, record in self.failures.items()
            if record.injected_at is not None and record.detected_at is not None
        }

    @property
    def heartbeat_detection_delays(self) -> dict[int, float]:
        return {
            agent_id: record.detected_at - record.last_heartbeat
            for agent_id, record in self.failures.items()
            if record.detected_at is not None and record.last_heartbeat is not None
        }

    @property
    def task_reassignment_latencies(self) -> dict[int, float]:
        return {
            task_id: recovery.reassigned_at - recovery.orphaned_at
            for task_id, recovery in self.recoveries.items()
            if recovery.reassigned_at is not None
        }

    @property
    def mean_predicted_peer_degree(self) -> float | None:
        if self.connectivity_aware_assignment_count == 0:
            return None
        return (
            self.total_predicted_peer_degree / self.connectivity_aware_assignment_count
        )

    @staticmethod
    def _mean_or_na(values: list[float]) -> str:
        return "N/A" if not values else f"{fmean(values):.2f} s"

    def format_summary(self) -> str:
        """Build the concise terminal result block from recorded values."""

        lines = [
            "PROTOTYPE 0.1 RESULT",
            "",
            f"Mission completed: {'YES' if self.mission_completed else 'NO'}",
            f"Tasks completed: {self.completed_task_count} / {self.total_task_count}",
            f"Agents started: {self.agents_started}",
            f"Agents failed: {self.failed_agent_count}",
            f"Orphaned tasks: {self.orphaned_task_count}",
            f"Tasks reassigned: {self.reassigned_task_count}",
            f"Recovered tasks: {self.recovered_task_count}",
            f"Belief divergences: {self.belief_divergence_event_count}",
            f"Declarations retracted: {self.declaration_retraction_count}",
            f"Contested tasks yielded: {self.contested_task_yield_count}",
            f"Duplicated completions: {self.duplicated_task_completion_count}",
            f"Simulation duration: {self.simulation_duration:.2f} s",
            "Failure detection latency: "
            + self._mean_or_na(list(self.failure_detection_latencies.values())),
            "Task reassignment latency: "
            + self._mean_or_na(list(self.task_reassignment_latencies.values())),
            f"Human interventions: {self.human_interventions}",
        ]
        if self._communication_initialized:
            lines.extend(
                [
                    "",
                    "NETWORK (PROTOTYPE 0.2A)",
                    f"Initial communication links: {self.initial_link_count}",
                    f"Minimum communication links: {self.minimum_link_count}",
                    "Maximum connected components: "
                    f"{self.maximum_connected_component_count}",
                    f"Isolation events: {self.isolation_event_count}",
                    f"Link-loss events: {self.link_loss_event_count}",
                    f"Link-restoration events: {self.link_restoration_event_count}",
                    "Communication-degraded duration: "
                    f"{self.total_communication_degraded_duration:.2f} s",
                    "Network ended connected: "
                    f"{'YES' if self.network_ended_connected else 'NO'}",
                    "Healthy but unreachable UAV events: "
                    f"{self.healthy_unreachable_event_count}",
                ]
            )
        lines.extend(
            [
                "",
                "PEER STATE (PROTOTYPE 0.2B)",
                f"Messages attempted: {self.peer_messages_attempted}",
                f"Messages delivered: {self.peer_messages_delivered}",
                f"Messages undelivered: {self.peer_messages_undelivered}",
                f"Peer stale transitions: {self.peer_state_stale_transition_count}",
                f"Peer refresh transitions: {self.peer_state_refresh_transition_count}",
                "Maximum simultaneous stale observations: "
                f"{self.maximum_simultaneous_stale_peer_observations}",
            ]
        )
        mean_degree = self.mean_predicted_peer_degree
        lines.extend(
            [
                "",
                "ALLOCATION (PROTOTYPE 0.3A)",
                f"Allocation policy: {self.allocation_policy}",
                "Connectivity-aware assignments: "
                f"{self.connectivity_aware_assignment_count}",
                "Assignments predicting isolation: "
                f"{self.predicted_isolation_assignment_count}",
                "Mean predicted peer degree: "
                + ("N/A" if mean_degree is None else f"{mean_degree:.2f}"),
                "Minimum predicted peer degree: "
                + (
                    "N/A"
                    if self.minimum_predicted_peer_degree is None
                    else str(self.minimum_predicted_peer_degree)
                ),
            ]
        )
        return "\n".join(lines)
