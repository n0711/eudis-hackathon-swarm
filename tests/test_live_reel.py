"""The reel-format animation: story, timeline and page, from logs alone."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.live_mission import read_jsonl, run_logged
from eudis_swarm.live_reel import (
    INTRO_HOLD_S,
    OUTRO_HOLD_S,
    REEL_OTHER,
    REEL_SERIES,
    Beat,
    reel_data,
    render_reel_html,
    timeline,
)

RECORDED = Path(__file__).resolve().parents[1] / "docs" / "data" / "sitl" / "phase5"


def _recorded(run: str) -> list:
    with (RECORDED / f"{run}.jsonl").open(encoding="utf-8") as stream:
        return read_jsonl(stream)


def _embedded(page: str) -> dict:
    match = re.search(
        r'<script id="reel-data" type="application/json">(.*?)</script>', page, re.S
    )
    assert match is not None
    return json.loads(match.group(1))


def test_the_story_of_a_recorded_flight_comes_from_its_own_numbers() -> None:
    data = reel_data(_recorded("c5"))
    texts = [beat["text"] for beat in data["beats"]]
    assert [beat["t"] for beat in data["beats"]] == [0.0, 4.0, 5.75, 11.25, 14.25]
    assert texts[0].startswith("4 ArduPilot copters share 8 tasks.")
    assert texts[1] == (
        "UAV 2 fails. It is put into BRAKE, once, and still travels 17 m before it stops."
    )
    assert "quorum declares it failed" in texts[2]
    assert (
        texts[3]
        == "UAV 4 takes over task 5, 7.25 s after the failure. Nobody told it to."
    )
    assert texts[4] == (
        "8/8 tasks done in 14.25 s, never two owners at once. "
        "Real autopilot firmware; simulated airframes."
    )
    assert data["live"] is True and data["run"] == "sitl"
    assert data["failure"] == {"agent": 2, "t": 4.0, "mode": "BRAKE"}
    assert [v["id"] for v in data["vehicles"]] == [1, 2, 3, 4]
    assert [v["color"] for v in data["vehicles"]] == list(REEL_SERIES)
    assert len(data["completions"]) == 8


def test_a_mixed_swarm_says_vehicles_and_names_the_rover_hold() -> None:
    data = reel_data(_recorded("mix5"))
    assert data["beats"][0]["text"].startswith("4 ArduPilot vehicles share 8 tasks.")
    assert data["failure"]["mode"] == "HOLD"
    assert [v["kind"] for v in data["vehicles"]] == ["copter"] * 3 + ["rover"]


def test_a_point_mass_log_tells_a_simulated_story() -> None:
    config = replace(
        SimulationConfig(),
        agent_count=4,
        task_count=8,
        task_control="distributed",
        completion_tolerance=2.0,
        failure_agent_id=2,
        failure_time=4.0,
    )
    _result, records = run_logged(config, run_label="point-mass")
    data = reel_data(records)
    texts = [beat["text"] for beat in data["beats"]]
    assert data["live"] is False
    assert texts[0].startswith("4 simulated vehicles share 8 tasks.")
    assert texts[1] == "UAV 2 fails and goes silent."  # no hold without a vehicle
    assert not texts[-1].endswith("simulated airframes.")
    page = render_reel_html(records)
    assert "deterministic point-mass simulation" in page

    _quiet, calm = run_logged(replace(config, failure_time=1000.0), run_label="pm")
    assert len(reel_data(calm)["beats"]) == 2  # intro and ending only


def test_the_timeline_plays_steadily_and_holds_on_each_beat() -> None:
    beats = [
        Beat(0.0, "a", INTRO_HOLD_S),
        Beat(4.0, "b", 2.0),
        Beat(10.0, "c", OUTRO_HOLD_S),
    ]
    keys = timeline(beats, 10.0, rate=2.0)
    assert keys == [
        [0.0, 0.0],
        [0.0, 0.0],
        [INTRO_HOLD_S, 0.0],
        [INTRO_HOLD_S + 2.0, 4.0],
        [INTRO_HOLD_S + 4.0, 4.0],
        [INTRO_HOLD_S + 7.0, 10.0],
        [INTRO_HOLD_S + 7.0 + OUTRO_HOLD_S, 10.0],
    ]
    assert all(b[0] >= a[0] and b[1] >= a[1] for a, b in zip(keys, keys[1:]))
    # A beat past the end is clamped; playback never runs backwards.
    late = timeline([Beat(0.0, "a", 1.0), Beat(20.0, "z", 1.0)], 10.0, rate=1.0)
    assert late[-1] == [12.0, 10.0]  # 1 s hold + 10 s played + 1 s hold
    with pytest.raises(ValueError, match="rate"):
        timeline(beats, 10.0, rate=0.0)


def test_the_page_is_self_contained_and_escapes_its_text() -> None:
    page = render_reel_html(
        _recorded("c5"), title="Fail<stop>", eyebrow="a & b", font_css="@font-face{}"
    )
    assert page.startswith("<!doctype html>")
    assert "<title>Fail&lt;stop&gt;</title>" in page and "a &amp; b" in page
    assert "<style>@font-face{}</style>" in page
    assert "fonts.googleapis.com" not in page  # inlined font: fully offline
    assert "window.renderAt" in page and "window.reelDuration" in page
    assert "ArduPilot SITL · live telemetry · not reproducible" in page
    data = _embedded(page)
    assert data["keys"][-1][1] == data["end"] == 14.25
    linked = render_reel_html(_recorded("c5"))
    assert "fonts.googleapis.com/css2?family=Space+Grotesk" in linked


def test_vehicles_past_four_are_drawn_in_neutral_grey() -> None:
    config = replace(
        SimulationConfig(),
        agent_count=6,
        task_count=6,
        task_control="distributed",
        failure_time=1000.0,
    )
    _result, records = run_logged(config, run_label="pm")
    colors = [v["color"] for v in reel_data(records)["vehicles"]]
    assert colors == [*REEL_SERIES, REEL_OTHER, REEL_OTHER]
