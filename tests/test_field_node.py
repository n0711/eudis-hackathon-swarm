"""Field-node decision steps on an in-memory network with an injected clock."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from field_fakes import Swarm, make_mission

from eudis_swarm.agent import AgentStatus, Heartbeat
from eudis_swarm.field_codec import encode_envelope
from eudis_swarm.field_faults import ControlState, parse_control, write_control
from eudis_swarm.field_node import FieldNode, MavlinkFieldVehicle, PointMassVehicle
from eudis_swarm.field_report import evaluate, load_logs
from eudis_swarm.peer_state import PeerStatus
from eudis_swarm.task import TaskOwnershipState
from eudis_swarm.task_claims import TaskClaim

SOURCE = Path(__file__).resolve().parents[1] / "src" / "eudis_swarm"
FIELD_MODULES = (
    "field_codec",
    "field_detector",
    "field_faults",
    "field_launch",
    "field_mission",
    "field_node",
    "field_probe",
    "field_report",
    "field_transport",
)


def _states(node: FieldNode, now: float) -> dict[int, TaskOwnershipState]:
    return {
        task: node.claim_store.view(task, now).state
        for task in node.claim_store.task_ids
    }


def test_a_connected_swarm_completes_every_task_once() -> None:
    swarm = Swarm(make_mission())
    swarm.run(8.0)
    swarm.stop()
    now = swarm.clock.t
    for node in swarm.nodes.values():
        assert set(_states(node, now).values()) == {TaskOwnershipState.COMPLETE}
        assert node.deterministic is False
    completers = [
        (record["task"], record["node"])
        for agent_id in swarm.nodes
        for record in swarm.kinds(agent_id, "TASK_COMPLETED")
    ]
    assert sorted(task for task, _ in completers) == [1, 2, 3, 4]


def test_a_claim_is_created_before_binding_and_bound_only_after_settle() -> None:
    swarm = Swarm(make_mission())
    swarm.run(1.0)
    for agent_id in swarm.nodes:
        records = swarm.records(agent_id)
        claimed = next(r for r in records if r["kind"] == "TASK_CLAIMED")
        bound = next(r for r in records if r["kind"] == "TASK_BOUND")
        assert claimed["task"] == bound["task"]
        assert bound["local_t"] - claimed["local_t"] >= 0.1
        # no physical motion before the bind.
        before = [r for r in records if r["seq"] < bound["seq"]]
        assert {tuple(r["position"]) for r in before} == {tuple(records[0]["position"])}


def test_simultaneous_claims_resolve_to_the_lowest_id_without_moving_the_loser() -> (
    None
):
    mission = make_mission(
        agents=[
            {"id": 1, "start": [0.0, 0.0], "address": "127.0.0.1:41001"},
            {"id": 2, "start": [0.0, 0.0], "address": "127.0.0.1:41002"},
        ],
        tasks=[{"id": 1, "position": [5.0, 0.0]}, {"id": 2, "position": [9.0, 0.0]}],
    )
    swarm = Swarm(mission)
    # make the two decisions truly simultaneous: node 2 decides before node
    # 1's claim reaches it, exactly as on a real network within one RTT.
    swarm.nodes[1].step()
    queue = swarm.network.queues[("127.0.0.1", 41002)]
    held = list(queue)
    queue.clear()
    swarm.nodes[2].step()
    queue.extend(held)
    swarm.clock.advance(0.05)
    swarm.run(0.5)
    # both chose task 1 from identical positions; only the lowest ID kept it.
    assert [r["task"] for r in swarm.kinds(2, "TASK_CLAIMED")][:2] == [1, 2]
    (decision,) = [r for r in swarm.kinds(2, "RECONCILED") if r["task"] == 1]
    assert decision["winner"] == 1 and decision["yielded"] is True
    assert swarm.nodes[1].current_task == 1
    assert swarm.nodes[2].current_task == 2
    assert not swarm.kinds(2, "TASK_STOOD_DOWN")


def test_a_partition_splits_ownership_and_the_heal_converges(tmp_path: Path) -> None:
    control = tmp_path / "control.json"
    write_control(control, ControlState())
    swarm = Swarm(make_mission(), log_dir=tmp_path, control_path=control)
    swarm.run(1.0)
    write_control(control, parse_control({"partition": [[1, 2], [3, 4]]}))
    swarm.run(2.5)
    now = swarm.clock.t
    # split brain: each half believes it owns tasks 3 and 4.
    left = _states(swarm.nodes[1], now)
    right = _states(swarm.nodes[3], now)
    assert TaskOwnershipState.OWNED_BY_SELF in (left[3], left[4])
    assert right[3] is TaskOwnershipState.OWNED_BY_SELF
    assert swarm.nodes[1].peer_store.status_for(3) is PeerStatus.DECLARED_FAILED
    write_control(control, ControlState())
    swarm.run(4.5)
    swarm.stop()
    now = swarm.clock.t
    views = [_states(node, now) for node in swarm.nodes.values()]
    assert all(view == views[0] for view in views)
    assert set(views[0].values()) == {TaskOwnershipState.COMPLETE}
    stood_down = {
        r["node"]: r for a in (3, 4) for r in swarm.kinds(a, "TASK_STOOD_DOWN")
    }
    assert set(stood_down) == {3, 4}
    assert all(item["contested"] for item in stood_down.values())
    # contact retracted the partition-era declarations.
    assert swarm.nodes[1].peer_store.status_for(3) is PeerStatus.HEARD
    assert swarm.kinds(1, "FAILURE_RETRACTED")
    report = evaluate(load_logs([tmp_path]))
    assert report.exit_code == 0, report.violations
    assert report.summary["divergent_tasks_at_reconnect"]
    assert report.summary["convergence_s"] <= report.summary["bound_s"]


def test_chain_topology_floods_evidence_but_never_heartbeats(tmp_path: Path) -> None:
    """Negative: 1 and 3 cannot hear each other, yet 1's claim reaches 3 via 2."""

    control = tmp_path / "control.json"
    write_control(control, parse_control({"blocked_links": [[1, 3]]}))
    mission = make_mission(
        agents=[
            {"id": 1, "start": [0.0, 0.0], "address": "127.0.0.1:41001"},
            {"id": 2, "start": [10.0, 0.0], "address": "127.0.0.1:41002"},
            {"id": 3, "start": [40.0, 0.0], "address": "127.0.0.1:41003"},
        ],
        tasks=[{"id": 1, "position": [60.0, 0.0]}],
    )
    swarm = Swarm(mission, control_path=control)
    swarm.run(2.0)
    now = swarm.clock.t
    node3 = swarm.nodes[3]
    # first-hand contact never crosses the missing link...
    assert node3.peer_store.observation_for(1) is None
    assert node3.peer_store.status_for(1) is PeerStatus.SILENT
    assert node3.peer_store.status_for(2) is PeerStatus.HEARD
    # ...but the claim does, store-and-forward through node 2, and stays
    # fresh because every renewal is relayed too.
    view = node3.claim_store.view(1, now)
    assert view.known_owner_agent_id == 1
    assert view.state is TaskOwnershipState.CLAIMED_BY_PEER_FRESH
    assert node3.relay.counters.first_deliveries > 0
    assert swarm.nodes[2].relay.counters.forwarded > 0
    # and 3 never claimed the task out from under the owner it cannot hear.
    assert not swarm.kinds(3, "TASK_CLAIMED")


def test_a_forwarded_heartbeat_is_rejected_not_believed() -> None:
    swarm = Swarm(make_mission())
    swarm.step()
    relayed = Heartbeat(3, (1.0, 1.0), AgentStatus.IDLE, None, 0.0)
    swarm.sockets[1].inject(encode_envelope("unit", 2, relayed))
    swarm.sockets[1].inject(b"\x00garbage")
    swarm.sockets[1].inject(
        encode_envelope("unit", 2, Heartbeat(7, (0, 0), AgentStatus.IDLE, None, 0.0))
    )
    swarm.nodes[1].step()
    node = swarm.nodes[1]
    assert node.counters.heartbeats_rejected == 2
    # a heartbeat about any third UAV, known or not, is a relayed one.
    assert node.counters.rejections["forwarded_heartbeat"] == 2
    assert node.counters.rejections["malformed"] == 1
    reasons = [r["reason"] for r in swarm.kinds(1, "REJECTED")]
    assert {"forwarded_heartbeat", "malformed"} <= set(reasons)
    observation = node.peer_store.observation_for(3)
    assert observation is None or observation.snapshot.position != (1.0, 1.0)


def test_reordered_heartbeats_and_invalid_evidence_are_dropped() -> None:
    swarm = Swarm(make_mission())
    swarm.run(0.3)
    node = swarm.nodes[1]
    late = Heartbeat(2, (0.0, 10.0), AgentStatus.IDLE, None, 0.0)
    swarm.sockets[1].inject(encode_envelope("unit", 2, late))
    alien = TaskClaim(1, 2, 99, 0.1, 0.25, 3.0)  # wrong lease policy
    swarm.sockets[1].inject(encode_envelope("unit", 2, alien))
    sent_before = len(swarm.network.sent)
    node.step()
    assert node.counters.heartbeats_reordered == 1
    assert node.counters.evidence_rejected == 1
    # invalid evidence is never forwarded onward.
    forwarded = [
        d for s, _, d in swarm.network.sent[sent_before:] if b'"epoch":99' in d
    ]
    assert forwarded == []
    # rejection is rate-limited in the log but always counted.
    for _ in range(10):
        swarm.sockets[1].inject(b"junk")
    node.step()
    assert node.counters.rejections["malformed"] == 10
    assert (
        len([r for r in swarm.kinds(1, "REJECTED") if r["reason"] == "malformed"]) == 5
    )


def test_fail_stop_silences_a_node_and_survivors_declare_it() -> None:
    swarm = Swarm(make_mission(), fail_stop_at={4: 1.0})
    swarm.run(8.0)
    swarm.stop()
    assert swarm.nodes[4].fail_stopped and swarm.nodes[4].step() is False
    assert swarm.kinds(4, "FAIL_STOP")
    for agent_id in (1, 2, 3):
        store = swarm.nodes[agent_id].peer_store
        assert store.status_for(4) is PeerStatus.DECLARED_FAILED
        assert 4 not in store.retracted_peer_ids
    # its abandoned task was re-claimed once the lease expired, and done.
    completers = {
        r["task"]: r["node"]
        for a in (1, 2, 3)
        for r in swarm.kinds(a, "TASK_COMPLETED")
    }
    assert 4 in completers and completers[4] != 4


def test_point_mass_and_mavlink_vehicles() -> None:
    vehicle = PointMassVehicle((0.0, 0.0), 2.0)
    assert vehicle.move_toward((10.0, 0.0), 1.0) == (2.0, 0.0)
    assert vehicle.move_toward((10.0, 0.0), -1.0) == (2.0, 0.0)
    vehicle.hold()
    with pytest.raises(ValueError):
        PointMassVehicle((0.0, 0.0), 0.0)

    class Telemetry:
        def __init__(self) -> None:
            self.position = (0.0, 0.0)
            self.vehicle_time_s = 0.0
            self.commands: list[tuple[float, float]] = []

        def command_toward(self, target: tuple[float, float]) -> None:
            self.commands.append(target)

        def observe(self, duration: float = 0.0) -> tuple[float, float]:
            self.position = (self.position[0] + 1.0, self.position[1])
            return self.position

    telemetry = Telemetry()
    live = MavlinkFieldVehicle(telemetry, (100.0, 50.0), command_interval=1.0)
    assert live.deterministic is False
    # mission-frame target is translated into the vehicle's own frame.
    assert live.move_toward((110.0, 50.0), 0.1) == (101.0, 50.0)
    assert telemetry.commands == [(10.0, 0.0)]
    live.move_toward((110.0, 50.0), 0.1)
    assert len(telemetry.commands) == 1
    live.move_toward((110.0, 50.0), 1.0)
    assert len(telemetry.commands) == 2
    live.hold()
    assert telemetry.commands[-1] == telemetry.position


def test_field_modules_stay_out_of_the_simulator_import_graph() -> None:
    """The non-deterministic backend never reaches the deterministic path."""

    names = ", ".join(repr(f"eudis_swarm.{name}") for name in FIELD_MODULES)
    probe = (
        "import sys; import eudis_swarm.simulation, eudis_swarm.sweep;"
        f"leaked = [m for m in ({names}, 'socket', 'select') if m in sys.modules"
        " and m.startswith('eudis_swarm')];"
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


def test_field_node_cannot_reach_world_truth() -> None:
    """Knowledge boundary: no import of mission/simulation/topology modules."""

    forbidden = {
        "mission",
        "simulation",
        "communication",
        "messaging",
        "evaluation",
        "metrics",
        "trace",
        "task_allocator",
        "field_report",
        "field_launch",
    }
    for name in ("field_node", "field_transport", "field_detector", "field_codec"):
        tree = ast.parse((SOURCE / f"{name}.py").read_text(encoding="utf-8"))
        imported = {
            node.module.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not imported & forbidden, (name, imported & forbidden)
