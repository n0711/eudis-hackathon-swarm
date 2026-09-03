"""Apply authoritative mission transitions after distributed decisions exist.

The mission owns observer-facing world mutation but never supplies peer truth
to a local utility scorer, ownership replica, or failure detector.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from .agent import Agent, AgentStatus, Heartbeat, Position
from .failure_manager import FailureDeclaration, FailureManager
from .metrics import SimulationMetrics
from .task import Task, TaskStatus
from .task_allocator import Allocation, AllocationPolicy
from .task_claims import TaskClaimStore
from .validation import validate_timestamp

LOGGER = logging.getLogger(__name__)


class MissionEventKind(str, Enum):
    MISSION_STARTED = "MISSION_STARTED"
    TASK_CLAIMED = "TASK_CLAIMED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    FAILURE_INJECTED = "FAILURE_INJECTED"
    HEARTBEAT_TIMEOUT = "HEARTBEAT_TIMEOUT"
    FAILURE_DECLARED = "FAILURE_DECLARED"
    FAILURE_RETRACTED = "FAILURE_RETRACTED"
    TASK_RELEASED = "TASK_RELEASED"
    TASK_REASSIGNED = "TASK_REASSIGNED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_DUPLICATED = "TASK_DUPLICATED"
    TASK_YIELDED = "TASK_YIELDED"
    TASK_STOOD_DOWN = "TASK_STOOD_DOWN"
    MISSION_COMPLETED = "MISSION_COMPLETED"
    MISSION_TIMED_OUT = "MISSION_TIMED_OUT"


class MissionState(str, Enum):
    """Minimal lifecycle for legal mission operations."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"


@dataclass(frozen=True, slots=True)
class MissionEvent:
    kind: MissionEventKind
    timestamp: float
    agent_id: int | None = None
    task_id: int | None = None
    position: Position | None = None


class Mission:
    """Own all state changes spanning agents and tasks."""

    def __init__(
        self,
        agents: Iterable[Agent],
        tasks: Iterable[Task],
        allocator: AllocationPolicy,
        failure_manager: FailureManager,
        metrics: SimulationMetrics,
        task_claim_stores: Mapping[int, TaskClaimStore] | None = None,
        *,
        distributed_task_control: bool = False,
    ) -> None:
        self.agents = {agent.agent_id: agent for agent in agents}
        self.tasks = {task.task_id: task for task in tasks}
        if len(self.agents) != metrics.agents_started:
            raise ValueError("agent IDs must be unique and match metrics")
        if len(self.tasks) != metrics.total_task_count:
            raise ValueError("task IDs must be unique and match metrics")
        self.allocator = allocator
        self.task_claim_stores = (
            None if task_claim_stores is None else dict(task_claim_stores)
        )
        self.distributed_task_control = distributed_task_control
        if distributed_task_control and self.task_claim_stores is None:
            raise ValueError("distributed task control requires claim stores")
        if self.task_claim_stores is not None:
            if set(self.task_claim_stores) != set(self.agents):
                raise ValueError("claim stores must match mission UAV IDs")
            expected_agent_ids = tuple(sorted(self.agents))
            expected_task_ids = tuple(sorted(self.tasks))
            for owner_agent_id, store in self.task_claim_stores.items():
                if owner_agent_id != store.owner_agent_id:
                    raise ValueError("claim-store key must match its owner")
                if store.agent_ids != expected_agent_ids:
                    raise ValueError("claim stores must match mission UAV IDs")
                if store.task_ids != expected_task_ids:
                    raise ValueError("claim stores must match mission task IDs")
        # agent membership is immutable, so one traversal order can be reused.
        self._ordered_agents = tuple(
            sorted(self.agents.values(), key=lambda item: item.agent_id)
        )
        self.failure_manager = failure_manager
        self.metrics = metrics
        self.events: list[MissionEvent] = []
        self._state = MissionState.CREATED
        self._last_timestamp: float | None = None

    @property
    def state(self) -> MissionState:
        return self._state

    @property
    def started(self) -> bool:
        return self.state is not MissionState.CREATED

    @property
    def finished(self) -> bool:
        return self.state in {MissionState.COMPLETED, MissionState.TIMED_OUT}

    @property
    def ordered_agents(self) -> tuple[Agent, ...]:
        return self._ordered_agents

    def _require_running(self) -> None:
        if self.state is not MissionState.RUNNING:
            raise RuntimeError("mission operations require a running mission")

    def _observe_time(self, timestamp: float) -> float:
        value = validate_timestamp(
            timestamp,
            previous=self._last_timestamp,
            name="mission timestamp",
        )
        self._last_timestamp = value
        return value

    def _event(
        self,
        kind: MissionEventKind,
        timestamp: float,
        *,
        agent_id: int | None = None,
        task_id: int | None = None,
        position: Position | None = None,
    ) -> None:
        self.events.append(
            MissionEvent(
                kind=kind,
                timestamp=timestamp,
                agent_id=agent_id,
                task_id=task_id,
                position=position,
            )
        )

    def start(self, timestamp: float = 0.0) -> tuple[Heartbeat, ...]:
        if self.state is not MissionState.CREATED:
            raise RuntimeError("mission has already started")
        timestamp = self._observe_time(timestamp)
        self._state = MissionState.RUNNING
        self.metrics.start_time = timestamp
        LOGGER.info(
            "[MISSION] Mission started with %d agents and %d tasks",
            len(self.agents),
            len(self.tasks),
        )
        self._event(MissionEventKind.MISSION_STARTED, timestamp)
        heartbeats = self.exchange_heartbeats(timestamp)
        if not self.distributed_task_control:
            self.allocate_tasks(timestamp)
        self.assert_consistent()
        return heartbeats

    def exchange_heartbeats(self, timestamp: float) -> tuple[Heartbeat, ...]:
        """Collect source snapshots without bypassing modeled delivery."""

        self._require_running()
        timestamp = self._observe_time(timestamp)
        heartbeats: list[Heartbeat] = []
        for agent in self._ordered_agents:
            heartbeat = agent.send_heartbeat(timestamp)
            if heartbeat is not None:
                heartbeats.append(heartbeat)
        return tuple(heartbeats)

    def inject_failure(self, agent_id: int, timestamp: float) -> bool:
        self._require_running()
        timestamp = self._observe_time(timestamp)
        agent = self.agents[agent_id]
        if not agent.inject_failure(timestamp):
            return False
        self.metrics.record_failure_injection(agent_id, timestamp, agent.position)
        self._event(
            MissionEventKind.FAILURE_INJECTED,
            timestamp,
            agent_id=agent_id,
            task_id=agent.current_task,
            position=agent.position,
        )
        LOGGER.info(
            "[FAULT] Injecting failure into UAV %d at t=%.2fs", agent_id, timestamp
        )
        return True

    def _apply_allocation(self, allocation: Allocation, timestamp: float) -> None:
        agent = self.agents[allocation.agent_id]
        task = self.tasks[allocation.task_id]
        agent.assign_task(task.task_id)
        try:
            task.assign(agent.agent_id)
        except Exception:
            agent.release_task(task.task_id)
            raise
        self.metrics.record_allocation(allocation, timestamp)

        recovery = self.metrics.recoveries.get(task.task_id)
        if recovery is not None and recovery.reassigned_agent_id is None:
            self.metrics.record_reassignment(task.task_id, agent.agent_id, timestamp)
            self._event(
                MissionEventKind.TASK_REASSIGNED,
                timestamp,
                agent_id=agent.agent_id,
                task_id=task.task_id,
            )
            if allocation.policy == "distance":
                LOGGER.info(
                    "[ALLOC] Task %d reassigned -> UAV %d",
                    task.task_id,
                    agent.agent_id,
                )
        else:
            self._event(
                MissionEventKind.TASK_ASSIGNED,
                timestamp,
                agent_id=agent.agent_id,
                task_id=task.task_id,
            )
            if allocation.policy == "distance":
                LOGGER.info("[ALLOC] Task %d -> UAV %d", task.task_id, agent.agent_id)
        if allocation.policy == "connectivity":
            LOGGER.info(
                "[ALLOC-COMM] Task %d -> UAV %d distance=%.2f predicted_links=%d%s",
                task.task_id,
                agent.agent_id,
                allocation.distance,
                allocation.predicted_peer_degree,
                " predicted_isolation=YES"
                if allocation.predicted_isolation
                else " predicted_isolation=NO",
            )

    def allocate_tasks(self, timestamp: float) -> list[Allocation]:
        """Apply the legacy centralized baseline outside distributed-control runs."""

        self._require_running()
        timestamp = self._observe_time(timestamp)
        if self.distributed_task_control:
            raise RuntimeError(
                "centralized allocation is disabled under distributed task control"
            )
        proposed = self.allocator.allocate(self.agents.values(), self.tasks.values())
        allocations: list[Allocation] = []
        for allocation in proposed:
            # the coordinator is no longer the sole writer of ownership: a task
            # whose lease is still valid somewhere in the swarm cannot be
            # handed out, even if this replica believes it is free.
            store = (
                None
                if self.task_claim_stores is None
                else self.task_claim_stores[allocation.agent_id]
            )
            if store is not None and not store.can_create_claim(
                allocation.task_id, timestamp
            ):
                self.metrics.claim_refused_allocation_count += 1
                continue
            self._apply_allocation(allocation, timestamp)
            if store is not None:
                store.create_claim(allocation.task_id, timestamp)
            allocations.append(allocation)
        return allocations

    def record_task_claim(self, agent_id: int, task_id: int, timestamp: float) -> None:
        """Record a locally created intent without making it executable."""

        self._require_running()
        timestamp = self._observe_time(timestamp)
        if not self.distributed_task_control:
            raise RuntimeError("task-claim events require distributed task control")
        self._event(
            MissionEventKind.TASK_CLAIMED,
            timestamp,
            agent_id=agent_id,
            task_id=task_id,
        )

    def activate_claimed_task(
        self, agent_id: int, task_id: int, timestamp: float
    ) -> bool:
        """Bind a local execution intent only after the local claim is authoritative.

        ``Task.assigned_agent`` is maintained only as an observer-facing world
        projection.  It is deliberately not consulted for authorization because
        one mutable field cannot represent partition-local split ownership.
        """

        self._require_running()
        timestamp = self._observe_time(timestamp)
        if not self.distributed_task_control or self.task_claim_stores is None:
            raise RuntimeError("claim activation requires distributed task control")
        agent = self.agents[agent_id]
        if not agent.responsive or agent.current_task is not None:
            return False
        if not self.task_claim_stores[agent_id].owns_task(task_id, timestamp):
            return False

        if agent.wrongly_declared:
            # The coordinator-visible FAILED label is not available to this live,
            # isolated process.  Preserve that observer belief while binding its
            # locally authorized intent.
            agent.current_task = task_id
        else:
            agent.assign_task(task_id)

        task = self.tasks[task_id]
        if task.status is TaskStatus.UNASSIGNED:
            task.assign(agent_id)

        recovery = self.metrics.recoveries.get(task_id)
        if recovery is not None and recovery.reassigned_agent_id is None:
            self.metrics.record_reassignment(task_id, agent_id, timestamp)
            kind = MissionEventKind.TASK_REASSIGNED
        else:
            kind = MissionEventKind.TASK_ASSIGNED
        self._event(kind, timestamp, agent_id=agent_id, task_id=task_id)
        LOGGER.info(
            "[CLAIM] UAV %d may execute Task %d after local ownership resolution",
            agent_id,
            task_id,
        )
        return True

    def task_is_executable(self, agent_id: int, task_id: int, timestamp: float) -> bool:
        """Return the single authorization predicate used by physical behavior."""

        timestamp = validate_timestamp(
            timestamp,
            previous=0.0 if self._last_timestamp is None else self._last_timestamp,
            name="execution authorization timestamp",
        )
        agent = self.agents[agent_id]
        if not agent.responsive or agent.current_task != task_id:
            return False
        if not self.distributed_task_control:
            return agent.status is AgentStatus.ACTIVE or agent.wrongly_declared
        if self.task_claim_stores is None:
            raise RuntimeError("distributed task control has no claim stores")
        return self.task_claim_stores[agent_id].owns_task(task_id, timestamp)

    def stand_down_unowned_task(
        self,
        agent_id: int,
        task_id: int,
        timestamp: float,
        *,
        contested: bool = False,
    ) -> bool:
        """Clear an execution intent after local ownership ceases to authorize it."""

        self._require_running()
        timestamp = self._observe_time(timestamp)
        if not self.distributed_task_control or self.task_claim_stores is None:
            raise RuntimeError("claim stand-down requires distributed task control")
        agent = self.agents[agent_id]
        if agent.current_task != task_id:
            return False
        if self.task_claim_stores[agent_id].owns_task(task_id, timestamp):
            raise ValueError("an actionable local owner must not stand down")

        agent.release_task(task_id)
        task = self.tasks[task_id]
        if task.status is TaskStatus.ASSIGNED and task.assigned_agent == agent_id:
            task.release()
        if contested:
            self.metrics.contested_task_yield_count += 1
        self._event(
            MissionEventKind.TASK_YIELDED
            if contested
            else MissionEventKind.TASK_STOOD_DOWN,
            timestamp,
            agent_id=agent_id,
            task_id=task_id,
        )
        LOGGER.info(
            "[RECONCILE] UAV %d stopped Task %d: local claim is not actionable",
            agent_id,
            task_id,
        )
        return True

    def release_owned_task(self, agent_id: int, task_id: int, timestamp: float) -> bool:
        """Voluntarily release locally owned work and publish its tombstone."""

        self._require_running()
        timestamp = self._observe_time(timestamp)
        if not self.distributed_task_control or self.task_claim_stores is None:
            raise RuntimeError("distributed release requires claim stores")
        agent = self.agents[agent_id]
        if agent.current_task != task_id:
            return False
        store = self.task_claim_stores[agent_id]
        if not store.owns_task(task_id, timestamp):
            raise ValueError("only the locally actionable owner may release a task")
        store.release_claim(task_id, timestamp)
        agent.release_task(task_id)
        task = self.tasks[task_id]
        if task.status is TaskStatus.ASSIGNED and task.assigned_agent == agent_id:
            task.release()
        self._event(
            MissionEventKind.TASK_RELEASED,
            timestamp,
            agent_id=agent_id,
            task_id=task_id,
        )
        return True

    def _declare_and_release(
        self, declaration: FailureDeclaration, timestamp: float
    ) -> None:
        agent = self.agents[declaration.agent_id]
        task_id = (
            declaration.task_id if self.distributed_task_control else agent.current_task
        )
        self.metrics.record_failure_detection(
            agent.agent_id, timestamp, declaration.last_heartbeat
        )
        self._event(
            MissionEventKind.HEARTBEAT_TIMEOUT,
            timestamp,
            agent_id=agent.agent_id,
            task_id=declaration.task_id,
        )
        LOGGER.info(
            "[HEARTBEAT] UAV %d heartbeat timeout at t=%.2fs",
            agent.agent_id,
            timestamp,
        )

        agent.declare_failed()
        self._event(
            MissionEventKind.FAILURE_DECLARED,
            timestamp,
            agent_id=agent.agent_id,
            task_id=declaration.task_id,
        )
        LOGGER.info(
            "[FAILURE] UAV %d declared FAILED by UAV %d with %d/%d votes",
            agent.agent_id,
            declaration.declarer_agent_id,
            len(declaration.voter_agent_ids),
            declaration.required_votes,
        )

        # In distributed mode a declaration never revokes another process's
        # claim.  A real fail-stop owner simply stops renewing; peer replicas can
        # acquire the task only after their own received copy of its lease expires.
        if task_id is None:
            return
        task = self.tasks[task_id]
        if task.status is TaskStatus.COMPLETED:
            return
        if task.status is TaskStatus.ASSIGNED and task.assigned_agent == agent.agent_id:
            task.release()
        if agent.wrongly_declared:
            # This live UAV never received the remote declaration and therefore
            # keeps following its own claim state.
            self.metrics.belief_divergence_event_count += 1
            LOGGER.info(
                "[DIVERGENCE] UAV %d still owns Task %d it was declared dead holding",
                agent.agent_id,
                task.task_id,
            )
        elif agent.current_task is not None:
            failed_task_id = agent.current_task
            agent.release_task(failed_task_id)
            failed_task = self.tasks[failed_task_id]
            if (
                failed_task.status is TaskStatus.ASSIGNED
                and failed_task.assigned_agent == agent.agent_id
            ):
                failed_task.release()
        self.metrics.record_orphan(task.task_id, agent.agent_id, timestamp)
        self._event(
            MissionEventKind.TASK_RELEASED,
            timestamp,
            agent_id=agent.agent_id,
            task_id=task.task_id,
        )
        LOGGER.info(
            "[RECOVERY] Releasing Task %d from UAV %d",
            task.task_id,
            agent.agent_id,
        )

    def detect_and_recover(
        self,
        timestamp: float,
        declarations: Iterable[FailureDeclaration],
    ) -> list[FailureDeclaration]:
        """Apply only quorum-backed declarations produced by the failure protocol."""

        self._require_running()
        timestamp = self._observe_time(timestamp)
        detected = tuple(declarations)
        applied: list[FailureDeclaration] = []
        for declaration in detected:
            if not self.failure_manager.recognizes_declaration(declaration):
                raise ValueError(
                    "failure declaration was not produced by the configured protocol"
                )
            # protocol certificates may retry, but world mutation happens once.
            if self.agents[declaration.agent_id].status is AgentStatus.FAILED:
                continue
            self._declare_and_release(declaration, timestamp)
            applied.append(declaration)
        if applied and not self.distributed_task_control:
            self.allocate_tasks(timestamp)
        if applied:
            self.assert_consistent()
        return applied

    def yield_contested_task(
        self, agent_id: int, task_id: int, timestamp: float
    ) -> bool:
        """Give up work this UAV lost in distributed reconciliation."""

        if self.distributed_task_control:
            return self.stand_down_unowned_task(
                agent_id,
                task_id,
                timestamp,
                contested=True,
            )

        self._require_running()
        timestamp = self._observe_time(timestamp)
        agent = self.agents[agent_id]
        if agent.current_task != task_id:
            return False
        task = self.tasks[task_id]
        agent.release_task(task_id)
        if task.status is TaskStatus.ASSIGNED and task.assigned_agent == agent_id:
            task.release()
        self.metrics.contested_task_yield_count += 1
        self._event(
            MissionEventKind.TASK_YIELDED,
            timestamp,
            agent_id=agent_id,
            task_id=task_id,
        )
        return True

    def retract_declaration(self, agent_id: int, timestamp: float) -> bool:
        """Undo a declaration that first-hand contact has disproved."""

        self._require_running()
        timestamp = self._observe_time(timestamp)
        agent = self.agents[agent_id]
        if not agent.wrongly_declared:
            return False
        # In the distributed path no observer-facing Task pointer can force a
        # surrender.  Delivered claim evidence and local reconciliation already
        # decide whether this UAV keeps or yields the work.
        if not self.distributed_task_control and agent.current_task is not None:
            stale_task_id = agent.current_task
            if self.tasks[stale_task_id].assigned_agent != agent_id:
                agent.release_task(stale_task_id)
                if self.task_claim_stores is not None:
                    store = self.task_claim_stores[agent_id]
                    if store.owns_task(stale_task_id, timestamp):
                        store.release_claim(stale_task_id, timestamp)
                self.metrics.rejoin_surrendered_task_count += 1
                LOGGER.info(
                    "[REJOIN] UAV %d surrendered Task %d, reassigned while it was "
                    "believed dead",
                    agent_id,
                    stale_task_id,
                )
        agent.status = (
            AgentStatus.ACTIVE if agent.current_task is not None else AgentStatus.IDLE
        )
        self.failure_manager.retract_declaration(agent_id)
        self.metrics.failures.pop(agent_id, None)
        self.metrics.record_declaration_retraction(agent_id)
        self._event(
            MissionEventKind.FAILURE_RETRACTED,
            timestamp,
            agent_id=agent_id,
            task_id=agent.current_task,
        )
        LOGGER.info(
            "[RETRACTION] UAV %d is alive after all; declaration withdrawn",
            agent_id,
        )
        return True

    def complete_task(self, agent_id: int, task_id: int, timestamp: float) -> None:
        self._require_running()
        timestamp = self._observe_time(timestamp)
        agent = self.agents[agent_id]
        task = self.tasks[task_id]
        if self.distributed_task_control:
            if self.task_claim_stores is None:
                raise RuntimeError("distributed task control has no claim stores")
            store = self.task_claim_stores[agent_id]
            if not self.task_is_executable(agent_id, task_id, timestamp):
                raise RuntimeError(
                    f"UAV {agent_id} cannot complete Task {task_id} without "
                    "an actionable local claim"
                )

            # Terminal evidence is durable before observer-facing world mutation.
            store.create_completion(task_id, timestamp)
            agent.complete_task(task_id)
            if task.status is TaskStatus.COMPLETED:
                self.metrics.duplicated_task_completion_count += 1
                self._event(
                    MissionEventKind.TASK_DUPLICATED,
                    timestamp,
                    agent_id=agent_id,
                    task_id=task_id,
                    position=agent.position,
                )
                LOGGER.info(
                    "[DUPLICATE] UAV %d finished already-completed Task %d",
                    agent_id,
                    task_id,
                )
                return

            # The mutable Task owner is an observer projection, not authority.
            # Let the first locally authorized physical completion replace a
            # stale projection left by a partition-local executor.
            if task.status is TaskStatus.ASSIGNED and task.assigned_agent != agent_id:
                task.release()
            if task.status is TaskStatus.UNASSIGNED:
                task.assign(agent_id)
            task.complete(agent_id)
            self.metrics.record_task_completion(task_id, timestamp)
            self._event(
                MissionEventKind.TASK_COMPLETED,
                timestamp,
                agent_id=agent_id,
                task_id=task_id,
                position=agent.position,
            )
            LOGGER.info("[TASK] UAV %d completed Task %d", agent_id, task_id)
            return

        if agent.wrongly_declared:
            # the coordinator believes this UAV is dead and cannot hear it, so
            # the effort is real but invisible: duplicated, not completed.
            agent.release_task(task_id)
            self.metrics.duplicated_task_completion_count += 1
            self._event(
                MissionEventKind.TASK_DUPLICATED,
                timestamp,
                agent_id=agent_id,
                task_id=task_id,
                position=agent.position,
            )
            LOGGER.info(
                "[DUPLICATE] UAV %d finished Task %d while believed dead",
                agent_id,
                task_id,
            )
            return
        task.complete(agent_id)
        agent.complete_task(task_id)
        if self.task_claim_stores is not None:
            store = self.task_claim_stores[agent_id]
            if store.owns_task(task_id, timestamp):
                store.create_completion(task_id, timestamp)
        self.metrics.record_task_completion(task_id, timestamp)
        self._event(
            MissionEventKind.TASK_COMPLETED,
            timestamp,
            agent_id=agent_id,
            task_id=task_id,
            position=agent.position,
        )
        LOGGER.info("[TASK] UAV %d completed Task %d", agent_id, task_id)

    @property
    def all_tasks_completed(self) -> bool:
        return bool(self.tasks) and all(
            task.status is TaskStatus.COMPLETED for task in self.tasks.values()
        )

    def finish(self, timestamp: float, completed: bool) -> None:
        self._require_running()
        timestamp = self._observe_time(timestamp)
        self.metrics.finish(timestamp, completed)
        self._state = MissionState.COMPLETED if completed else MissionState.TIMED_OUT
        if completed:
            self._event(MissionEventKind.MISSION_COMPLETED, timestamp)
            LOGGER.info("[MISSION] Mission completed at t=%.2fs", timestamp)
        else:
            self._event(MissionEventKind.MISSION_TIMED_OUT, timestamp)
            LOGGER.error("[MISSION] Mission timed out at t=%.2fs", timestamp)

    def assert_consistent(self) -> None:
        """Fail fast if mission state violates its selected ownership model."""

        if self.distributed_task_control:
            self._assert_distributed_consistent()
            return

        active_task_ids: set[int] = set()
        for agent in self.agents.values():
            if agent.wrongly_declared:
                # a partitioned UAV may hold work the coordinator reassigned.
                # That divergence is the phenomenon under study, not a defect,
                # and it is resolved by the ownership layer on reconnection.
                continue
            if agent.status is AgentStatus.FAILED and agent.current_task is not None:
                raise RuntimeError("a failed UAV still owns a task")
            if agent.current_task is None:
                if agent.status is AgentStatus.ACTIVE:
                    raise RuntimeError("an active UAV has no task")
                continue
            if agent.current_task in active_task_ids:
                raise RuntimeError("multiple UAVs own the same task")
            active_task_ids.add(agent.current_task)
            task = self.tasks[agent.current_task]
            if (
                task.status is not TaskStatus.ASSIGNED
                or task.assigned_agent != agent.agent_id
            ):
                raise RuntimeError("agent/task ownership links do not match")

        for task in self.tasks.values():
            if task.status is TaskStatus.UNASSIGNED and task.assigned_agent is not None:
                raise RuntimeError("an unassigned task has an owner")
            if task.status is TaskStatus.ASSIGNED:
                if task.assigned_agent is None:
                    raise RuntimeError("an assigned task has no owner")
                owner = self.agents[task.assigned_agent]
                if owner.wrongly_declared:
                    continue
                if owner.current_task != task.task_id:
                    raise RuntimeError("task/agent ownership links do not match")

    def _assert_distributed_consistent(self) -> None:
        """Check local execution authority without demanding global agreement."""

        if self.task_claim_stores is None:
            raise RuntimeError("distributed task control has no claim stores")
        timestamp = 0.0 if self._last_timestamp is None else self._last_timestamp
        for agent in self.agents.values():
            if agent.current_task is None:
                if agent.status is AgentStatus.ACTIVE:
                    raise RuntimeError("an active UAV has no execution intent")
                continue
            if agent.current_task not in self.tasks:
                raise RuntimeError("an execution intent names an unknown task")
            # An unresponsive process has no advancing local clock and cannot
            # execute.  Its pointer may remain until a declaration is applied.
            if agent.responsive and not self.task_claim_stores[
                agent.agent_id
            ].owns_task(agent.current_task, timestamp):
                raise RuntimeError(
                    "a responsive UAV has an execution intent without local ownership"
                )

        for task in self.tasks.values():
            if task.status is TaskStatus.UNASSIGNED and task.assigned_agent is not None:
                raise RuntimeError("an unassigned task has an observer owner")
            if task.status is TaskStatus.ASSIGNED and task.assigned_agent is None:
                raise RuntimeError("an assigned task has no observer owner")
