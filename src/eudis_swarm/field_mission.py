"""Load the shared mission file every field node starts from.

The mission file is the one thing all nodes legitimately share before take-off:
who is in the swarm, the immutable task catalogue, where each aircraft starts,
and the protocol timing every claim store and peer store must agree on (claim
lease policy is validated on receipt, so a node with different timings would
reject its peers' claims). It holds no runtime state.

Format (JSON, ``"format": "eudis-field-mission"``, ``"version": 1``)::

    {
      "format": "eudis-field-mission", "version": 1,
      "mission_id": "lab-2x2",
      "speed": 2.0, "completion_tolerance": 0.5,
      "agents": [{"id": 1, "start": [0, 0], "address": "127.0.0.1:47001"}, ...],
      "tasks":  [{"id": 1, "position": [10, 0]}, ...],
      "timing": {"heartbeat_interval": 0.1, "peer_stale_after": 0.5,
                 "heartbeat_timeout": 0.6, "lease_timeout": 1.5,
                 "freshness_timeout": 0.75, "round_interval": 0.05,
                 "rebroadcast_interval": 0.25, "claim_settle": 0.15},
      "network": {"mode": "unicast"}
    }

``network.mode`` is ``"unicast"`` (each agent's ``address`` is where its
datagrams go and where it binds) or ``"broadcast"`` with
``"broadcast_address"`` and ``"port"``. Every ``timing`` key is optional and
falls back to the default shown in :class:`FieldTiming`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from math import isfinite
from pathlib import Path
from typing import Any

from .field_transport import Address, parse_address
from .task_utility import TaskObjective

MISSION_FORMAT = "eudis-field-mission"
MISSION_VERSION = 1


class MissionFileError(ValueError):
    """The mission file is missing, malformed, or inconsistent."""


@dataclass(frozen=True, slots=True)
class FieldTiming:
    """Protocol timing, in seconds of each node's own monotonic clock."""

    #: How often a node emits its one-hop heartbeat.
    heartbeat_interval: float = 0.1
    #: Peer snapshot age after which it is STALE (PeerStateStore stale_after).
    peer_stale_after: float = 0.5
    #: Silence after which a node votes (FailureManager heartbeat_timeout).
    heartbeat_timeout: float = 0.6
    #: Claim lease; a claim older than this is EXPIRED.
    lease_timeout: float = 1.5
    #: Claim age after which it is STALE and the owner renews it.
    freshness_timeout: float = 0.75
    #: Main loop period.
    round_interval: float = 0.05
    #: Period of persistent re-offer (own evidence and held relay evidence).
    rebroadcast_interval: float = 0.25
    #: How long a fresh claim is held before the node binds and moves, so a
    #: simultaneous competing claim can arrive and be reconciled first.
    claim_settle: float = 0.15

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or value < 0.0
            ):
                raise MissionFileError(f"timing.{item.name} must be a number >= 0")
            if item.name != "claim_settle" and value == 0.0:
                raise MissionFileError(f"timing.{item.name} must be positive")
        if self.freshness_timeout >= self.lease_timeout:
            raise MissionFileError("freshness_timeout must be less than lease_timeout")
        if self.claim_settle >= self.freshness_timeout:
            # an unbound claim is not renewed, so it must bind while FRESH.
            raise MissionFileError("claim_settle must be less than freshness_timeout")
        if self.rebroadcast_interval >= self.freshness_timeout:
            # a claim must be re-offered at least once while still FRESH, or a
            # single lost packet would make healthy ownership look STALE.
            raise MissionFileError(
                "rebroadcast_interval must be less than freshness_timeout"
            )


@dataclass(frozen=True, slots=True)
class FieldAgent:
    agent_id: int
    start: tuple[float, float]
    address: Address | None


@dataclass(frozen=True, slots=True)
class FieldNetwork:
    mode: str
    broadcast_address: Address | None = None


@dataclass(frozen=True, slots=True)
class FieldMission:
    mission_id: str
    agents: tuple[FieldAgent, ...]
    objectives: tuple[TaskObjective, ...]
    speed: float
    completion_tolerance: float
    timing: FieldTiming
    network: FieldNetwork

    @property
    def agent_ids(self) -> tuple[int, ...]:
        return tuple(agent.agent_id for agent in self.agents)

    @property
    def task_ids(self) -> tuple[int, ...]:
        return tuple(objective.task_id for objective in self.objectives)

    def agent(self, agent_id: int) -> FieldAgent:
        for agent in self.agents:
            if agent.agent_id == agent_id:
                return agent
        raise MissionFileError(f"agent {agent_id} is not in mission {self.mission_id}")

    def objective(self, task_id: int) -> TaskObjective:
        for objective in self.objectives:
            if objective.task_id == task_id:
                return objective
        raise KeyError(task_id)

    def to_json(self) -> dict[str, Any]:
        """Return the canonical JSON form (round-trips through :func:`parse`)."""

        network: dict[str, Any] = {"mode": self.network.mode}
        if self.network.broadcast_address is not None:
            network["broadcast_address"] = self.network.broadcast_address[0]
            network["port"] = self.network.broadcast_address[1]
        return {
            "format": MISSION_FORMAT,
            "version": MISSION_VERSION,
            "mission_id": self.mission_id,
            "speed": self.speed,
            "completion_tolerance": self.completion_tolerance,
            "agents": [
                {
                    "id": agent.agent_id,
                    "start": list(agent.start),
                    **(
                        {}
                        if agent.address is None
                        else {"address": f"{agent.address[0]}:{agent.address[1]}"}
                    ),
                }
                for agent in self.agents
            ],
            "tasks": [
                {"id": objective.task_id, "position": list(objective.position)}
                for objective in self.objectives
            ],
            "timing": {
                item.name: getattr(self.timing, item.name)
                for item in fields(self.timing)
            },
            "network": network,
        }


def _positive_id(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MissionFileError(f"{name} must be a positive integer")
    return value


def _real(value: object, name: str, *, positive: bool = False) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(value)
    ):
        raise MissionFileError(f"{name} must be a finite number")
    if positive and value <= 0.0:
        raise MissionFileError(f"{name} must be positive")
    return float(value)


def _point(value: object, name: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise MissionFileError(f"{name} must be a two-element list")
    return (_real(value[0], name), _real(value[1], name))


def _object(value: object, name: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MissionFileError(f"{name} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise MissionFileError(f"{name} has unknown keys {unknown}")
    return value


def parse(raw: object) -> FieldMission:
    """Validate a decoded mission document."""

    document = _object(
        raw,
        "mission",
        {
            "format",
            "version",
            "mission_id",
            "speed",
            "completion_tolerance",
            "agents",
            "tasks",
            "timing",
            "network",
            "description",
        },
    )
    if document.get("format") != MISSION_FORMAT:
        raise MissionFileError(f"mission format must be {MISSION_FORMAT!r}")
    if document.get("version") != MISSION_VERSION:
        raise MissionFileError(f"unsupported mission version {document.get('version')}")
    mission_id = document.get("mission_id")
    if (
        not isinstance(mission_id, str)
        or not mission_id
        or len(mission_id) > 64
        or not mission_id.isprintable()
    ):
        raise MissionFileError("mission_id must be a short printable string")

    agents_raw = document.get("agents")
    if not isinstance(agents_raw, list) or len(agents_raw) < 2:
        raise MissionFileError("agents must list at least two UAVs")
    agents: list[FieldAgent] = []
    for index, item in enumerate(agents_raw):
        entry = _object(item, f"agents[{index}]", {"id", "start", "address"})
        address_text = entry.get("address")
        address: Address | None = None
        if address_text is not None:
            if not isinstance(address_text, str):
                raise MissionFileError(f"agents[{index}].address must be host:port")
            try:
                address = parse_address(address_text)
            except ValueError as error:
                raise MissionFileError(str(error)) from None
        agents.append(
            FieldAgent(
                agent_id=_positive_id(entry.get("id"), f"agents[{index}].id"),
                start=_point(entry.get("start"), f"agents[{index}].start"),
                address=address,
            )
        )
    agent_ids = [agent.agent_id for agent in agents]
    if len(set(agent_ids)) != len(agent_ids):
        raise MissionFileError("agent ids must be unique")

    tasks_raw = document.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise MissionFileError("tasks must list at least one task")
    objectives: list[TaskObjective] = []
    for index, item in enumerate(tasks_raw):
        entry = _object(item, f"tasks[{index}]", {"id", "position"})
        objectives.append(
            TaskObjective(
                task_id=_positive_id(entry.get("id"), f"tasks[{index}].id"),
                position=_point(entry.get("position"), f"tasks[{index}].position"),
            )
        )
    task_ids = [objective.task_id for objective in objectives]
    if len(set(task_ids)) != len(task_ids):
        raise MissionFileError("task ids must be unique")

    timing_raw = _object(
        document.get("timing", {}),
        "timing",
        {item.name for item in fields(FieldTiming)},
    )
    timing = FieldTiming(**timing_raw)

    network_raw = _object(
        document.get("network", {"mode": "unicast"}),
        "network",
        {"mode", "broadcast_address", "port"},
    )
    mode = network_raw.get("mode")
    if mode == "unicast":
        network = FieldNetwork(mode="unicast")
    elif mode == "broadcast":
        host = network_raw.get("broadcast_address")
        port = network_raw.get("port")
        if not isinstance(host, str) or not host:
            raise MissionFileError("broadcast mode needs network.broadcast_address")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            raise MissionFileError("broadcast mode needs network.port in 1..65535")
        network = FieldNetwork(mode="broadcast", broadcast_address=(host, port))
    else:
        raise MissionFileError("network.mode must be 'unicast' or 'broadcast'")

    return FieldMission(
        mission_id=mission_id,
        agents=tuple(sorted(agents, key=lambda agent: agent.agent_id)),
        objectives=tuple(sorted(objectives, key=lambda item: item.task_id)),
        speed=_real(document.get("speed"), "speed", positive=True),
        completion_tolerance=_real(
            document.get("completion_tolerance", 0.5),
            "completion_tolerance",
            positive=True,
        ),
        timing=timing,
        network=network,
    )


def load_mission(path: str | Path) -> FieldMission:
    """Read and validate a mission file."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise MissionFileError(f"cannot read mission file {path}: {error}") from None
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise MissionFileError(f"mission file {path} is not JSON: {error}") from None
    return parse(raw)


__all__ = [
    "FieldAgent",
    "FieldMission",
    "FieldNetwork",
    "FieldTiming",
    "MISSION_FORMAT",
    "MISSION_VERSION",
    "MissionFileError",
    "load_mission",
    "parse",
]
