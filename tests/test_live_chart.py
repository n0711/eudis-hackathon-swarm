"""The offline mission chart, rendered from logs without a vehicle."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("plotly")

from fake_fleet import FakeAutopilot, FakeWorld  # noqa: E402

from eudis_swarm.config import SimulationConfig  # noqa: E402
from eudis_swarm.live_chart import (  # noqa: E402
    OTHER,
    SERIES,
    altitudes,
    build_figure,
    main,
    render_html,
    separations,
    series_color,
    task_points,
    tracks,
)
from eudis_swarm.live_mission import Record, read_jsonl, run_logged  # noqa: E402
from eudis_swarm.mavlink_backend import MavlinkBackend  # noqa: E402
from eudis_swarm.simulation import build_scenario  # noqa: E402

RECORDED = Path(__file__).resolve().parents[1] / "docs" / "data" / "sitl" / "phase5"


def _config() -> SimulationConfig:
    return replace(
        SimulationConfig(),
        agent_count=4,
        task_count=8,
        task_control="distributed",
        max_simulation_time=120.0,
        completion_tolerance=2.0,
        failure_agent_id=2,
        failure_time=4.0,
    )


def _both_runs() -> list[Record]:
    config = _config()
    world = FakeWorld()
    agents, tasks = build_scenario(config)
    vehicles = {a.agent_id: FakeAutopilot(world, a.position) for a in agents}
    for index, vehicle in enumerate(vehicles.values()):
        vehicle.altitude_m = 20.0 + 5.0 * index
    backend = MavlinkBackend(dict(vehicles))
    _r, live = run_logged(config, run_label="sitl", vehicle_backend=backend)
    _r, baseline = run_logged(config, run_label="point-mass")
    return [*live, *baseline]


def _traces(figure: Any, name_part: str) -> list[Any]:
    return [t for t in figure.data if t.name and name_part in t.name]


def test_the_figure_draws_both_runs_altitude_and_separation() -> None:
    records = _both_runs()
    figure = build_figure(records)

    tracks_per_uav = _traces(figure, "UAV 1 (")
    assert len(tracks_per_uav) == 2  # one track per map: live and point-mass
    assert tracks_per_uav[0].line.color == SERIES[0]
    assert {t.line.color for t in _traces(figure, "UAV 4 (")} == {SERIES[3]}
    failure = _traces(figure, "failure / rest")
    assert len(failure) == 2
    live_marks = list(failure[0].text)
    assert live_marks == ["UAV 2 fails", "UAV 2 at rest"]  # fail-stop seen live
    assert list(failure[1].text) == ["UAV 2 fails"]  # point-mass has no hold
    tasks = _traces(figure, "task")
    assert sorted(tasks[0].text) == sorted(f"T{n}" for n in range(1, 9))
    titles = [a.text for a in figure.layout.annotations]
    assert "Live SITL run (non-deterministic)" in titles
    assert "Minimum 3-D separation between live vehicles" in titles


def test_track_altitude_and_separation_series_come_from_the_log() -> None:
    records = _both_runs()
    live = [r for r in records if r["run"] == "sitl"]
    paths = tracks(live)
    assert sorted(paths) == [1, 2, 3, 4]
    assert paths[1][0][0] == 0.0
    heights = altitudes(live)
    assert {agent: series[0][1] for agent, series in heights.items()} == {
        1: 20.0,
        2: 25.0,
        3: 30.0,
        4: 35.0,
    }
    gaps = separations(live)
    assert gaps and min(distance for _t, distance in gaps) >= 5.0
    header_tasks = task_points(live)
    assert sorted(header_tasks) == list(range(1, 9))
    # Without positions in the header, tasks fall back to where they were done.
    old = [dict(r) for r in live]
    old[0] = {k: v for k, v in old[0].items() if k != "task_positions"}
    derived = task_points(old)
    assert sorted(derived) == list(range(1, 9))
    for task, (x, y) in derived.items():
        hx, hy = header_tasks[task]
        assert abs(x - hx) <= 2.0 + 1e-9 and abs(y - hy) <= 2.0 + 1e-9


def test_vehicles_past_the_palette_are_neutral_not_a_generated_hue() -> None:
    assert [series_color(i) for i in range(8)] == list(SERIES)
    assert series_color(8) == series_color(15) == OTHER


def test_a_point_mass_only_log_still_renders() -> None:
    _r, records = run_logged(_config(), run_label="point-mass")
    figure = build_figure(records)
    assert _traces(figure, "UAV 1 (")
    with pytest.raises(ValueError, match="no runs"):
        build_figure([])


def test_the_page_is_offline_and_carries_the_table_and_checklist() -> None:
    page = render_html(_both_runs(), title="Mission <1>")
    assert page.startswith("<!doctype html>")
    assert "<title>Mission &lt;1&gt;</title>" in page
    assert '<script src="https://cdn.plot.ly' not in page  # embedded, no network
    assert "plotly" in page.lower()
    assert "| Tasks completed | 8/8 | 8/8 |" in page
    assert "sitl: LT3 test card" in page


def test_the_recorded_mixed_flight_renders(tmp_path: Path, capsys) -> None:
    out = tmp_path / "mix5.html"
    assert main([str(RECORDED / "mix5.jsonl"), "--output", str(out)]) == 0
    assert f"wrote {out}" in capsys.readouterr().out
    page = out.read_text(encoding="utf-8")
    assert "UAV 4 at rest" in page and "1:copter, 2:copter, 3:copter, 4:rover" in page
    with (RECORDED / "mix5.jsonl").open(encoding="utf-8") as stream:
        assert build_figure(read_jsonl(stream))


def test_the_default_output_path_and_a_bad_log(tmp_path: Path, capsys) -> None:
    log = tmp_path / "run.jsonl"
    log.write_text((RECORDED / "c5.jsonl").read_text(encoding="utf-8"))
    assert main([str(log)]) == 0
    assert (tmp_path / "run.html").exists()

    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert main([str(empty)]) == 2
    assert "no runs" in capsys.readouterr().err
