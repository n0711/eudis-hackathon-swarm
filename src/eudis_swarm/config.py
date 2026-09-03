"""Central configuration for the deterministic swarm simulation."""

from __future__ import annotations

from dataclasses import dataclass, field

from .communication import RadioModel, RegionJammer
from .validation import (
    validate_nonnegative_real,
    validate_positive_integer,
    validate_positive_real,
)


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Validated parameters for a deterministic simulation run."""

    agent_count: int = 4
    task_count: int = 20
    random_seed: int = 2026
    area_width: float = 100.0
    area_height: float = 100.0
    agent_speed: float = 10.0
    time_step: float = 0.25
    completion_tolerance: float = 0.75
    heartbeat_interval: float = 1.0
    failure_timeout: float = 2.5
    peer_state_stale_after: float = 2.5
    allocation_policy: str = "distance"
    claim_lease_duration: float = 2.5
    failure_agent_id: int = 2
    failure_time: float = 4.0
    max_simulation_time: float = 300.0
    communication_range: float = 130.0
    link_model: str = "range"
    stochastic_delivery: bool = False
    radio_model: RadioModel = field(default_factory=RadioModel)
    blocked_links: frozenset[tuple[int, int]] = frozenset()
    region_jammer: RegionJammer | None = None
    comm_fault_agent_id: int | None = None
    comm_fault_start: float = 4.0
    comm_fault_end: float = 8.0

    def __post_init__(self) -> None:
        validate_positive_integer(self.agent_count, name="agent_count")
        validate_positive_integer(self.task_count, name="task_count")
        validate_positive_integer(self.failure_agent_id, name="failure_agent_id")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool):
            raise ValueError("random_seed must be an integer")
        if self.comm_fault_agent_id is not None:
            validate_positive_integer(
                self.comm_fault_agent_id, name="comm_fault_agent_id"
            )
        if self.allocation_policy not in {"distance", "connectivity"}:
            raise ValueError("allocation_policy must be 'distance' or 'connectivity'")
        if self.link_model not in {"range", "radio"}:
            raise ValueError("link_model must be 'range' or 'radio'")
        if not isinstance(self.stochastic_delivery, bool):
            raise ValueError("stochastic_delivery must be a boolean")
        if not isinstance(self.radio_model, RadioModel):
            raise ValueError("radio_model must be a RadioModel instance")
        if self.stochastic_delivery and self.link_model != "radio":
            raise ValueError("stochastic_delivery requires link_model='radio'")

        try:
            supplied_blocked_links = list(self.blocked_links)
        except TypeError:
            raise ValueError(
                "blocked_links must be an iterable of UAV ID pairs"
            ) from None
        canonical_blocked_links: set[tuple[int, int]] = set()
        for pair in supplied_blocked_links:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or any(
                    not isinstance(endpoint, int) or isinstance(endpoint, bool)
                    for endpoint in pair
                )
            ):
                raise ValueError("blocked_links entries must be two-item UAV ID tuples")
            left_agent_id, right_agent_id = pair
            if left_agent_id == right_agent_id:
                raise ValueError("blocked_links must not contain self-links")
            if not (
                1 <= left_agent_id <= self.agent_count
                and 1 <= right_agent_id <= self.agent_count
            ):
                raise ValueError(
                    "blocked_links entries must identify configured agents"
                )
            canonical_blocked_links.add(
                (
                    min(left_agent_id, right_agent_id),
                    max(left_agent_id, right_agent_id),
                )
            )
        object.__setattr__(self, "blocked_links", frozenset(canonical_blocked_links))
        if self.region_jammer is not None and not isinstance(
            self.region_jammer, RegionJammer
        ):
            raise ValueError("region_jammer must be a RegionJammer instance")

        positive_floats = {
            "area_width": self.area_width,
            "area_height": self.area_height,
            "agent_speed": self.agent_speed,
            "time_step": self.time_step,
            "heartbeat_interval": self.heartbeat_interval,
            "failure_timeout": self.failure_timeout,
            "peer_state_stale_after": self.peer_state_stale_after,
            "claim_lease_duration": self.claim_lease_duration,
            "max_simulation_time": self.max_simulation_time,
            "communication_range": self.communication_range,
        }
        for name, value in positive_floats.items():
            validate_positive_real(value, name=name)

        non_negative_floats = {
            "completion_tolerance": self.completion_tolerance,
            "failure_time": self.failure_time,
            "comm_fault_start": self.comm_fault_start,
            "comm_fault_end": self.comm_fault_end,
        }
        for name, value in non_negative_floats.items():
            validate_nonnegative_real(value, name=name)

        if self.comm_fault_end <= self.comm_fault_start:
            raise ValueError("comm_fault_end must be greater than comm_fault_start")
        if self.failure_timeout < self.heartbeat_interval:
            raise ValueError("failure_timeout must be at least heartbeat_interval")
        if not 1 <= self.failure_agent_id <= self.agent_count:
            raise ValueError("failure_agent_id must identify a configured agent")
        if self.comm_fault_agent_id is not None and not (
            1 <= self.comm_fault_agent_id <= self.agent_count
        ):
            raise ValueError("comm_fault_agent_id must identify a configured agent")
