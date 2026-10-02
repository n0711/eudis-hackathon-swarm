"""In-memory stand-ins for the field node's clock and UDP sockets.

They let node decision steps run with an injected clock and an in-memory
datagram network: each fake socket has an address, ``sendto`` queues bytes at
the destination, ``recvfrom`` pops them or raises ``BlockingIOError`` exactly
like a non-blocking UDP socket with nothing to read. Delivery is perfect; any
loss or partition comes from the node's own fault injector, as in a lab run.
"""

from __future__ import annotations

import io
import json
from collections import deque
from pathlib import Path
from typing import Any

from eudis_swarm.field_faults import FaultInjector
from eudis_swarm.field_mission import FieldMission, parse
from eudis_swarm.field_node import FieldNode, JsonlLog, build_transport


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeNetwork:
    def __init__(self) -> None:
        self.queues: dict[tuple[str, int], deque[tuple[bytes, tuple[str, int]]]] = {}
        self.sent: list[tuple[tuple[str, int], tuple[str, int], bytes]] = []

    def socket(self, address: tuple[str, int]) -> FakeSocket:
        self.queues.setdefault(address, deque())
        return FakeSocket(self, address)

    def deliver(
        self, source: tuple[str, int], target: tuple[str, int], data: bytes
    ) -> None:
        self.sent.append((source, target, data))
        queue = self.queues.get(target)
        if queue is not None:
            queue.append((data, source))


class FakeSocket:
    def __init__(self, network: FakeNetwork, address: tuple[str, int]) -> None:
        self.network = network
        self.address = address
        self.closed = False
        self.fail_sends = False

    def sendto(self, data: bytes, address: tuple[str, int], /) -> int:
        if self.fail_sends:
            raise OSError("network is unreachable")
        self.network.deliver(self.address, address, bytes(data))
        return len(data)

    def recvfrom(self, bufsize: int, /) -> tuple[bytes, Any]:
        queue = self.network.queues[self.address]
        if not queue:
            raise BlockingIOError
        data, source = queue.popleft()
        return data[:bufsize], source

    def inject(self, data: bytes, source: tuple[str, int] = ("10.0.0.9", 9)) -> None:
        self.network.queues[self.address].append((data, source))

    def close(self) -> None:
        self.closed = True


def mission_document(**overrides: Any) -> dict[str, Any]:
    """A four-UAV, four-task mission: 1-2 short tasks, 3-4 long legs."""

    document: dict[str, Any] = {
        "format": "eudis-field-mission",
        "version": 1,
        "mission_id": "unit",
        "speed": 5.0,
        "completion_tolerance": 0.25,
        "agents": [
            {"id": 1, "start": [0.0, 0.0], "address": "127.0.0.1:41001"},
            {"id": 2, "start": [0.0, 10.0], "address": "127.0.0.1:41002"},
            {"id": 3, "start": [50.0, 0.0], "address": "127.0.0.1:41003"},
            {"id": 4, "start": [50.0, 10.0], "address": "127.0.0.1:41004"},
        ],
        "tasks": [
            {"id": 1, "position": [2.0, 0.0]},
            {"id": 2, "position": [2.0, 10.0]},
            {"id": 3, "position": [20.0, 0.0]},
            {"id": 4, "position": [20.0, 10.0]},
        ],
        "timing": {
            "heartbeat_interval": 0.1,
            "peer_stale_after": 0.4,
            "heartbeat_timeout": 0.5,
            "lease_timeout": 1.0,
            "freshness_timeout": 0.5,
            "round_interval": 0.05,
            "rebroadcast_interval": 0.2,
            "claim_settle": 0.1,
        },
        "network": {"mode": "unicast"},
    }
    document.update(overrides)
    return document


def make_mission(**overrides: Any) -> FieldMission:
    return parse(mission_document(**overrides))


class Swarm:
    """N field nodes on one fake network, stepped in lock-step on one clock."""

    def __init__(
        self,
        mission: FieldMission,
        *,
        log_dir: Path | None = None,
        control_path: Path | None = None,
        fail_stop_at: dict[int, float] | None = None,
    ) -> None:
        self.mission = mission
        self.clock = FakeClock()
        self.network = FakeNetwork()
        self.wall = FakeClock(1_000_000.0)
        self.streams: dict[int, io.StringIO] = {}
        self.log_paths: dict[int, Path] = {}
        self.nodes: dict[int, FieldNode] = {}
        self.sockets: dict[int, FakeSocket] = {}
        for agent in mission.agents:
            assert agent.address is not None
            sock = self.network.socket(agent.address)
            faults = FaultInjector(
                agent.agent_id,
                control_path=control_path,
                fail_stop_at=(fail_stop_at or {}).get(agent.agent_id),
            )
            transport = build_transport(
                sock, mission, agent.agent_id, clock=self.clock, faults=faults
            )
            if log_dir is None:
                stream = io.StringIO()
                self.streams[agent.agent_id] = stream
                log = JsonlLog(stream)
            else:
                path = log_dir / f"node-{agent.agent_id}.jsonl"
                self.log_paths[agent.agent_id] = path
                log = JsonlLog.open(path)
            node = FieldNode(
                mission,
                agent.agent_id,
                transport=transport,
                clock=self.clock,
                log=log,
                faults=faults,
                wall_clock=lambda: self.wall.t + self.clock.t,
            )
            transport.set_reject_hook(node.on_transport_reject)
            self.nodes[agent.agent_id] = node
            self.sockets[agent.agent_id] = sock

    def step(self, dt: float = 0.05) -> None:
        for _, node in sorted(self.nodes.items()):
            node.step()
        self.clock.advance(dt)

    def run(self, seconds: float, dt: float = 0.05) -> None:
        for _ in range(round(seconds / dt)):
            self.step(dt)

    def stop(self) -> None:
        for node in self.nodes.values():
            if not node.stopped or node.fail_stopped:
                node.stop("test")

    def records(self, agent_id: int) -> list[dict[str, Any]]:
        if agent_id in self.streams:
            text = self.streams[agent_id].getvalue()
        else:
            text = self.log_paths[agent_id].read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]

    def kinds(self, agent_id: int, kind: str) -> list[dict[str, Any]]:
        return [item for item in self.records(agent_id) if item["kind"] == kind]
