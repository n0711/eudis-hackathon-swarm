"""Central configuration for the deterministic swarm simulation."""

from __future__ import annotations

from dataclasses import dataclass, field

from .communication import RadioModel
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
