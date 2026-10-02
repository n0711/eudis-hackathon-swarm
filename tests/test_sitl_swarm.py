"""The multi-vehicle mission command and the SITL launcher, with fakes.

No autopilot, no pymavlink, no ArduPilot checkout: the mission runs against
fake free-running vehicles, and the launcher is exercised in its DRY_RUN mode
and on its argument checks.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fake_fleet import FakeAutopilot, FakeWorld

from eudis_swarm.geodesy import LocalFrame
from eudis_swarm.sitl_swarm import (
    DEFAULT_ORIGIN,
    build_parser,
    connection_strings,
    main,
    mission_config,
    start_positions,
)

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "sitl-swarm.sh"


class _Factory:
    """Hands out fake vehicles placed where a correct launcher would put them."""

    def __init__(
        self,
        *,
        misplace: int | None = None,
        fail_takeoff: int | None = None,
        stall: int | None = None,
    ) -> None:
        self.world = FakeWorld()
        self.created: dict[str, FakeAutopilot] = {}
        self.origins: list[tuple[float, float]] = []
        self.rates: list[int] = []
        self._misplace = misplace
        self._fail_takeoff = fail_takeoff
        self._stall = stall

    def __call__(
        self, connection: str, origin: tuple[float, float], stream_rate_hz: int
    ) -> FakeAutopilot:
        self.origins.append(origin)
        self.rates.append(stream_rate_hz)
        port = int(connection.rsplit(":", 1)[1])
        agent_id = (port - 5760) // 10 + 1
        config = mission_config(build_parser().parse_args(["--count", "4"]))
        start = start_positions(config)[agent_id]
        if agent_id == self._misplace:
            start = (start[0] + 30.0, start[1])
        vehicle = FakeAutopilot(self.world, start, offset_s=30.0 + agent_id)
        if agent_id == self._fail_takeoff:

            def refuse(_altitude: float) -> None:
                raise TimeoutError("the vehicle never accepted an arm command")

            vehicle.arm_and_takeoff = refuse  # type: ignore[method-assign]
        if agent_id == self._stall:
            original = vehicle.command_toward

            def freeze(target: tuple[float, float]) -> None:
                original(target)
                vehicle.rate = 0.0

            vehicle.command_toward = freeze  # type: ignore[method-assign]
        self.created[connection] = vehicle
        return vehicle


def _run(argv: list[str], factory: _Factory) -> tuple[int, str]:
    out = io.StringIO()
    code = main(argv, vehicle_factory=factory, out=out)
    return code, out.getvalue()


def test_a_four_vehicle_mission_flies_fails_one_and_compares(tmp_path: Path) -> None:
    factory = _Factory()
    log = tmp_path / "evidence.jsonl"

    code, printed = _run(["--count", "4", "--log", str(log)], factory)

    assert code == 0
    assert sorted(factory.created) == [
        "tcp:127.0.0.1:5760",
        "tcp:127.0.0.1:5770",
        "tcp:127.0.0.1:5780",
        "tcp:127.0.0.1:5790",
    ]
    assert factory.origins == [DEFAULT_ORIGIN] * 4
    assert factory.rates == [10] * 4
    vehicles = [factory.created[key] for key in sorted(factory.created)]
    assert [v.armed_to for v in vehicles] == [20.0, 25.0, 30.0, 35.0]
    assert [v.holds for v in vehicles] == [[], ["BRAKE"], [], []]
    assert all(v.modes == ["LAND"] for v in vehicles)
    assert all(v.closed for v in vehicles)
    assert "| Tasks completed | 8/8 | 8/8 |" in printed
    assert "UAV 2 BRAKE at 4.00 s (confirmed" in printed
    assert "NON-deterministic" in printed

    records = [json.loads(line) for line in log.read_text().splitlines()]
    runs = [r["run"] for r in records if r["type"] == "header"]
    assert runs == ["sitl", "point-mass"]
    assert [r["deterministic"] for r in records if r["type"] == "header"] == [
        False,
        True,
    ]


def test_a_live_subset_and_explicit_connections(tmp_path: Path) -> None:
    factory = _Factory()
    connections = [f"tcp:10.0.0.9:{5760 + 10 * index}" for index in range(4)]

    code, _printed = _run(
        [
            "--connect",
            *connections,
            "--live-agents",
            "3,1",
            "--fail-stop-mode",
            "LOITER",
            "--failure-agent",
            "1",
            "--no-land",
            "--log",
            str(tmp_path / "e.jsonl"),
        ],
        factory,
    )

    assert code == 0
    assert sorted(factory.created) == [connections[0], connections[2]]
    first, third = factory.created[connections[0]], factory.created[connections[2]]
    assert first.holds == ["LOITER"] and third.holds == []
    assert first.modes == [] and third.modes == []  # --no-land


def test_no_failure_means_no_fail_stop(tmp_path: Path) -> None:
    factory = _Factory()
    code, printed = _run(
        ["--no-failure", "--tasks", "4", "--log", str(tmp_path / "e.jsonl")], factory
    )
    assert code == 0
    assert all(v.holds == [] for v in factory.created.values())
    assert "| Physical failure | none | none |" in printed


def test_a_misplaced_vehicle_aborts_before_anything_arms(
    tmp_path: Path, capsys
) -> None:
    factory = _Factory(misplace=3)
    code, _printed = _run(["--log", str(tmp_path / "e.jsonl")], factory)

    assert code == 1
    assert "30.0 m off" in capsys.readouterr().err
    assert all(v.armed_to is None for v in factory.created.values())
    assert all(v.closed for v in factory.created.values())
    assert not (tmp_path / "e.jsonl").exists()


def test_a_takeoff_failure_is_reported_and_every_link_closed(
    tmp_path: Path, capsys
) -> None:
    factory = _Factory(fail_takeoff=2)
    code, _printed = _run(["--log", str(tmp_path / "e.jsonl")], factory)
    assert code == 1
    assert "arm command" in capsys.readouterr().err
    assert all(v.closed for v in factory.created.values())


def test_a_stalled_vehicle_mid_mission_puts_every_vehicle_into_brake(
    tmp_path: Path, capsys
) -> None:
    factory = _Factory(stall=4)
    code, printed = _run(
        ["--step-timeout", "0.5", "--log", str(tmp_path / "e.jsonl")], factory
    )

    assert code == 1
    assert "stale position" in capsys.readouterr().err
    assert "mission aborted: putting every vehicle into BRAKE" in printed
    assert all(v.holds[-1:] == ["BRAKE"] for v in factory.created.values())
    assert all(v.closed for v in factory.created.values())


def test_the_abort_path_survives_a_vehicle_that_cannot_hold(tmp_path: Path) -> None:
    factory = _Factory(stall=4)

    def broken_hold(_mode: str = "BRAKE") -> bool:
        raise ConnectionError("link lost")

    original = factory.__call__

    def build(connection: str, origin, rate):  # type: ignore[no-untyped-def]
        vehicle = original(connection, origin, rate)
        if connection.endswith(":5760"):
            vehicle.hold = broken_hold  # type: ignore[method-assign]
        return vehicle

    out = io.StringIO()
    code = main(
        ["--step-timeout", "0.5", "--log", str(tmp_path / "e.jsonl")],
        vehicle_factory=build,
        out=out,
    )
    assert code == 1
    assert "UAV 1: BRAKE failed: link lost" in out.getvalue()
    assert "UAV 2: BRAKE confirmed" in out.getvalue()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--live-agents", "5"], "does not exist"),
        (["--connect", "tcp:a:1", "--live-agents", "2"], "lists 1 connections"),
        (["--failure-agent", "9"], "failure_agent_id"),
    ],
)
def test_inconsistent_arguments_fail_cleanly(
    argv: list[str], message: str, tmp_path: Path, capsys
) -> None:
    factory = _Factory()
    code, _printed = _run([*argv, "--log", str(tmp_path / "e.jsonl")], factory)
    assert code == 1
    assert message in capsys.readouterr().err
    assert factory.created == {}


@pytest.mark.parametrize(
    "argv",
    [
        ["--count", "0"],
        ["--stream-rate", "0"],
        ["--origin", "35.1"],
        ["--origin", "north,east"],
        ["--live-agents", "a,b"],
        ["--live-agents", "0"],
        ["--jammer", "1,2,3"],
        ["--comm-fault", "1,2"],
        ["--fail-stop-mode", "RTL"],
        ["--host", "h", "--connect", "tcp:h:1"],
    ],
)
def test_bad_command_lines_are_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv, vehicle_factory=_Factory(), out=io.StringIO())
    assert raised.value.code == 2


def test_jammer_comm_fault_and_tolerance_reach_the_config() -> None:
    config = mission_config(
        build_parser().parse_args(
            [
                "--jammer",
                "50,50,20,2,6",
                "--comm-fault",
                "3,1.5,4.5",
                "--completion-tolerance",
                "3",
                "--origin",
                "35.0,33.0,100,0",
            ]
        )
    )
    assert config.region_jammer is not None and config.region_jammer.radius == 20.0
    assert (config.comm_fault_agent_id, config.comm_fault_start) == (3, 1.5)
    assert config.completion_tolerance == 3.0
    assert config.task_control == "distributed"


def test_connection_strings_follow_the_sitl_port_convention() -> None:
    arguments = build_parser().parse_args(["--host", "172.20.1.2"])
    assert connection_strings(arguments, (1, 4)) == {
        1: "tcp:172.20.1.2:5760",
        4: "tcp:172.20.1.2:5790",
    }


# --- the launcher -------------------------------------------------------------

needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="the launcher needs bash and python3",
)


def _launch(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


@needs_bash
@pytest.mark.parametrize("count", [1, 4, 6])
def test_launcher_homes_match_the_simulated_start_positions(count: int) -> None:
    """The launcher's own geodesy agrees with the package to a centimetre."""

    result = _launch(str(count), "3", DRY_RUN="1")
    assert result.returncode == 0, result.stderr
    homes = [
        line.split()
        for line in result.stdout.splitlines()
        if line.startswith("instance")
    ]
    assert len(homes) == count

    arguments = build_parser().parse_args(
        ["--count", str(count), "--failure-agent", "1"]
    )
    starts = start_positions(mission_config(arguments))
    frame = LocalFrame(*DEFAULT_ORIGIN)
    for fields in homes:
        instance, agent, port = int(fields[1]), int(fields[3]), int(fields[5])
        assert agent == instance + 1 and port == 5760 + 10 * instance
        lat, lon, alt, heading = (float(v) for v in fields[7].split(","))
        east, north = frame.to_local((lat, lon))
        assert east == pytest.approx(starts[agent][0], abs=0.01)
        assert north == pytest.approx(starts[agent][1], abs=0.01)
        assert (alt, heading) == (100.0, 0.0)
    assert "--speedup 3" in result.stdout and "--sysid 1" in result.stdout


@needs_bash
def test_launcher_honours_origin_and_area() -> None:
    result = _launch("1", DRY_RUN="1", ORIGIN="10.0,20.0,50,90", AREA="200")
    assert result.returncode == 0, result.stderr
    home = result.stdout.split("home ", 1)[1].split()[0]
    lat, lon, alt, heading = (float(v) for v in home.split(","))
    east, north = LocalFrame(10.0, 20.0).to_local((lat, lon))
    assert (round(east, 2), round(north, 2)) == (10.0, 10.0)
    assert (alt, heading) == (50.0, 90.0)
    assert "--speedup 5" in result.stdout  # the default


@needs_bash
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("0",), "N must be an integer from 1 to 16"),
        (("17",), "N must be an integer from 1 to 16"),
        (("four",), "N must be an integer"),
        (("2", "0"), "SPEEDUP must be a positive integer"),
        (("2", "fast"), "SPEEDUP must be a positive integer"),
    ],
)
def test_launcher_refuses_bad_arguments(args: tuple[str, ...], message: str) -> None:
    result = _launch(*args, DRY_RUN="1")
    assert result.returncode == 2
    assert message in result.stderr


@needs_bash
def test_launcher_usage_bad_origin_and_missing_build(tmp_path: Path) -> None:
    assert _launch().returncode == 2
    assert _launch("--help").returncode == 2
    bad_origin = _launch("2", DRY_RUN="1", ORIGIN="1,2")
    assert bad_origin.returncode != 0
    assert "ORIGIN must be" in bad_origin.stderr
    missing = _launch("2", ARDUPILOT_DIR=str(tmp_path), RUN_DIR=str(tmp_path / "run"))
    assert missing.returncode == 1
    assert "./waf copter" in missing.stderr


@needs_bash
def test_launcher_stop_and_status_with_nothing_running(tmp_path: Path) -> None:
    run_dir = str(tmp_path / "run")
    for command in ("stop", "status"):
        result = _launch(command, RUN_DIR=run_dir)
        assert result.returncode == 0
        assert "no instances recorded" in result.stdout


@needs_bash
def test_launcher_starts_tracks_and_stops_instances(tmp_path: Path) -> None:
    """A stand-in binary proves the process handling without ArduPilot."""

    ardupilot = tmp_path / "ardupilot"
    binary = ardupilot / "build" / "sitl" / "bin" / "arducopter"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/usr/bin/env bash\necho "fake sitl $*"\nexec sleep 60\n')
    binary.chmod(0o755)
    run_dir = tmp_path / "run"
    env = {"ARDUPILOT_DIR": str(ardupilot), "RUN_DIR": str(run_dir)}

    started = _launch("2", "4", **env)
    try:
        assert started.returncode == 0, started.stderr
        assert "started instance 1 (agent 2) on TCP 5770" in started.stdout
        status = _launch("status", **env)
        assert status.stdout.count("running") == 2
        again = _launch("2", **env)
        assert again.returncode == 1 and "already recorded" in again.stderr
        log = (run_dir / "inst1" / "sitl.log").read_text()
        assert "-I 1 --sysid 2" in log and "--speedup 4" in log
    finally:
        stopped = _launch("stop", **env)
    assert stopped.returncode == 0
    assert stopped.stdout.count("stopped instance") == 2
    assert not (run_dir / "pids").exists()
