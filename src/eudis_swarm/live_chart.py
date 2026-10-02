"""Offline chart of a multi-vehicle mission evidence log.

Renders one self-contained HTML page from a ``live_mission`` JSON-lines log:
top-down tracks of the live run beside the point-mass run (same axes, one line
per vehicle, every track labelled), where each task was completed, where the
failed vehicle was when it failed and where it actually came to rest; below
them every live vehicle's observed altitude over time (the deconfliction
layering) and the minimum pairwise 3-D separation per step against the
threshold. The comparison table and the LT3 checklist are embedded as text, so
nothing on the page depends on colour alone.

Guarded on Plotly exactly like ``lease_timeout_chart``: Plotly's JavaScript is
embedded inline, so the file opens with no network, and a missing ``charts``
extra disables this module without touching the rest of the package. Nothing in
the simulator imports it.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .live_mission import Record, read_jsonl
from .live_report import (
    DEFAULT_SEPARATION_THRESHOLD_M,
    check_test_card,
    format_checklist,
    format_comparison,
    split_live_and_baseline,
    step_separation,
    summarize,
)

if TYPE_CHECKING:
    import plotly.graph_objects as go

#: Categorical slots in fixed order (validated: adjacent CVD ΔE ≥ 9.1 on the
#: light surface). Slots 3 and 4 sit below 3:1 contrast, so every track is
#: direct-labelled and the page carries the table. Vehicles past the eighth
#: are drawn in the neutral "other" ink rather than in a generated hue.
SERIES = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
)
OTHER = "#8a8984"
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"


def series_color(index: int) -> str:
    return SERIES[index] if index < len(SERIES) else OTHER


def _runs(records: Sequence[Record]) -> dict[str, list[Record]]:
    runs: dict[str, list[Record]] = {}
    for record in records:
        runs.setdefault(str(record.get("run")), []).append(record)
    return runs


def _header(records: Sequence[Record]) -> Record:
    return next(record for record in records if record["type"] == "header")


def tracks(records: Sequence[Record]) -> dict[int, list[tuple[float, float, float]]]:
    """``agent -> [(t, east, north)]``: observed for live vehicles, truth otherwise.

    A live vehicle's track comes from what its autopilot reported (including
    after a fail-stop, so the braking distance is visible); every other agent's
    from the simulator's own position.
    """

    result: dict[int, list[tuple[float, float, float]]] = {}
    for step in records:
        if step["type"] != "step":
            continue
        t = float(step["t"])
        observed: dict[int, list[float]] = {}
        for vehicle in step["vehicles"]:
            observed[int(vehicle["id"])] = vehicle["position"]
        for agent in step["agents"]:
            agent_id = int(agent["id"])
            east, north = observed.get(agent_id) or agent["position"]
            result.setdefault(agent_id, []).append((t, float(east), float(north)))
    return result


def altitudes(records: Sequence[Record]) -> dict[int, list[tuple[float, float]]]:
    result: dict[int, list[tuple[float, float]]] = {}
    for step in records:
        if step["type"] != "step":
            continue
        for vehicle in step["vehicles"]:
            if vehicle.get("altitude_m") is not None:
                result.setdefault(int(vehicle["id"]), []).append(
                    (float(step["t"]), float(vehicle["altitude_m"]))
                )
    return result


def separations(records: Sequence[Record]) -> list[tuple[float, float]]:
    points = []
    for step in records:
        if step["type"] == "step":
            measured = step_separation(step)
            if measured is not None:
                points.append((float(step["t"]), measured[0]))
    return points


def task_points(records: Sequence[Record]) -> dict[int, tuple[float, float]]:
    """Task positions from the header, else where each task was completed."""

    header = _header(records)
    known = header.get("task_positions")
    if known:
        return {int(task): (float(x), float(y)) for task, (x, y) in known.items()}
    positions: dict[int, tuple[float, float]] = {}
    paths = tracks(records)
    for step in records:
        if step["type"] != "step":
            continue
        for event in step["events"]:
            if event["kind"] == "TASK_COMPLETED" and event["task_id"] is not None:
                path = paths.get(int(event["agent_id"]), [])
                at = [p for p in path if p[0] == float(step["t"])]
                if at:
                    positions.setdefault(int(event["task_id"]), (at[-1][1], at[-1][2]))
    return positions


def _failure_marks(records: Sequence[Record]) -> list[tuple[str, float, float, str]]:
    """``(label, east, north, hover)`` for the failure point and the resting point."""

    marks: list[tuple[str, float, float, str]] = []
    paths = tracks(records)
    for step in records:
        if step["type"] != "step":
            continue
        for event in step["events"]:
            agent_id = event.get("agent_id")
            if event["kind"] == "FAILURE_INJECTED" and agent_id is not None:
                path = [
                    p for p in paths.get(int(agent_id), []) if p[0] <= float(event["t"])
                ]
                if path:
                    marks.append(
                        (
                            f"UAV {agent_id} fails",
                            path[-1][1],
                            path[-1][2],
                            f"physical failure at t={float(event['t']):.2f} s",
                        )
                    )
            if event["kind"] == "FAIL_STOP" and agent_id is not None:
                rest = paths.get(int(agent_id), [])
                if rest:
                    marks.append(
                        (
                            f"UAV {agent_id} at rest",
                            rest[-1][1],
                            rest[-1][2],
                            f"{event['mode']} sent at t={float(event['t']):.2f} s; "
                            "last observed position",
                        )
                    )
    return marks


def build_figure(
    records: Sequence[Record],
    *,
    separation_threshold_m: float = DEFAULT_SEPARATION_THRESHOLD_M,
) -> "go.Figure":
    if importlib.util.find_spec("plotly") is None:
        raise ImportError("plotly is unavailable; install the 'charts' extra")
    runs = _runs(records)
    if not runs:
        raise ValueError("no runs in the log")
    live_label = next(
        (label for label, items in runs.items() if not _header(items)["deterministic"]),
        None,
    )
    baseline_label = next(
        (label for label, items in runs.items() if _header(items)["deterministic"]),
        None,
    )

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    map_titles = [
        "Live SITL run (non-deterministic)"
        if label == live_label
        else "Point-mass run, same configuration (deterministic)"
        for label in (live_label, baseline_label)
        if label is not None
    ]
    figure = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=(
            *(map_titles + [""] * (2 - len(map_titles))),
            "Observed altitude (live)",
            "Minimum 3-D separation between live vehicles",
        ),
        vertical_spacing=0.12,
        horizontal_spacing=0.08,
    )
    header = _header(next(iter(runs.values())))
    agent_ids = [int(agent) for agent in header["agent_ids"]]
    colors = {agent: series_color(index) for index, agent in enumerate(agent_ids)}
    kinds = {int(k): v for k, v in header.get("vehicle_types", {}).items()}

    for column, label in enumerate((live_label, baseline_label), start=1):
        if label is None:
            continue
        items = runs[label]
        tasks = task_points(items)
        figure.add_trace(
            go.Scatter(
                x=[p[0] for p in tasks.values()],
                y=[p[1] for p in tasks.values()],
                mode="markers+text",
                text=[f"T{task}" for task in tasks],
                textposition="bottom center",
                textfont={"color": TEXT_SECONDARY, "size": 10},
                marker={"symbol": "square-open", "size": 9, "color": TEXT_SECONDARY},
                name="task",
                legendgroup="task",
                showlegend=column == 1,
                hovertemplate="task %{text}<br>(%{x:.1f}, %{y:.1f}) m<extra></extra>",
            ),
            row=1,
            col=column,
        )
        for agent, path in sorted(tracks(items).items()):
            kind = kinds.get(agent, "vehicle")
            figure.add_trace(
                go.Scatter(
                    x=[p[1] for p in path],
                    y=[p[2] for p in path],
                    customdata=[p[0] for p in path],
                    mode="lines",
                    line={"width": 2, "color": colors.get(agent, OTHER)},
                    name=f"UAV {agent} ({kind})",
                    legendgroup=f"uav{agent}",
                    showlegend=column == 1,
                    hovertemplate=(
                        f"UAV {agent}<br>t=%{{customdata:.2f}} s<br>"
                        "(%{x:.1f}, %{y:.1f}) m<extra></extra>"
                    ),
                ),
                row=1,
                col=column,
            )
            figure.add_trace(
                go.Scatter(
                    x=[path[0][1]],
                    y=[path[0][2]],
                    mode="markers+text",
                    text=[f"UAV {agent}"],
                    textposition="top center",
                    textfont={"color": TEXT_PRIMARY, "size": 11},
                    marker={"size": 8, "color": colors.get(agent, OTHER)},
                    showlegend=False,
                    legendgroup=f"uav{agent}",
                    hovertemplate=f"UAV {agent} start<extra></extra>",
                ),
                row=1,
                col=column,
            )
        marks = _failure_marks(items)
        if marks:
            figure.add_trace(
                go.Scatter(
                    x=[m[1] for m in marks],
                    y=[m[2] for m in marks],
                    mode="markers+text",
                    text=[m[0] for m in marks],
                    customdata=[m[3] for m in marks],
                    textposition="middle right",
                    textfont={"color": TEXT_PRIMARY, "size": 11},
                    marker={
                        "symbol": [
                            "x" if "fails" in m[0] else "circle-open" for m in marks
                        ],
                        "size": 12,
                        "color": TEXT_PRIMARY,
                        "line": {"width": 2},
                    },
                    name="failure / rest",
                    legendgroup="failure",
                    showlegend=column == 1,
                    hovertemplate="%{text}<br>%{customdata}<extra></extra>",
                ),
                row=1,
                col=column,
            )

    if live_label is not None:
        live = runs[live_label]
        for agent, series in sorted(altitudes(live).items()):
            figure.add_trace(
                go.Scatter(
                    x=[p[0] for p in series],
                    y=[p[1] for p in series],
                    mode="lines",
                    line={"width": 2, "color": colors.get(agent, OTHER)},
                    name=f"UAV {agent}",
                    legendgroup=f"uav{agent}",
                    showlegend=False,
                    hovertemplate=(
                        f"UAV {agent}<br>t=%{{x:.2f}} s<br>%{{y:.1f}} m<extra></extra>"
                    ),
                ),
                row=2,
                col=1,
            )
            figure.add_annotation(
                x=series[-1][0],
                y=series[-1][1],
                text=f"UAV {agent}",
                showarrow=False,
                xanchor="left",
                font={"color": TEXT_PRIMARY, "size": 10},
                row=2,
                col=1,
            )
        gaps = separations(live)
        if gaps:
            figure.add_trace(
                go.Scatter(
                    x=[p[0] for p in gaps],
                    y=[p[1] for p in gaps],
                    mode="lines",
                    line={"width": 2, "color": TEXT_PRIMARY},
                    name="min separation",
                    showlegend=False,
                    hovertemplate="t=%{x:.2f} s<br>%{y:.1f} m<extra></extra>",
                ),
                row=2,
                col=2,
            )
            span = [gaps[0][0], gaps[-1][0]]
            figure.add_trace(
                go.Scatter(
                    x=span,
                    y=[separation_threshold_m] * 2,
                    mode="lines+text",
                    text=["", f"threshold {separation_threshold_m:g} m"],
                    textposition="top left",
                    textfont={"color": TEXT_SECONDARY, "size": 10},
                    line={"width": 1, "dash": "dash", "color": TEXT_SECONDARY},
                    showlegend=False,
                    hoverinfo="skip",
                ),
                row=2,
                col=2,
            )

    figure.update_xaxes(title_text="east (m)", row=1, col=1)
    figure.update_xaxes(title_text="east (m)", row=1, col=2)
    figure.update_yaxes(title_text="north (m)", row=1, col=1)
    figure.update_yaxes(
        title_text="north (m)", row=1, col=2, scaleanchor="x2", scaleratio=1
    )
    figure.update_yaxes(scaleanchor="x", scaleratio=1, row=1, col=1)
    figure.update_xaxes(title_text="logical time (s)", row=2, col=1)
    figure.update_xaxes(title_text="logical time (s)", row=2, col=2)
    figure.update_yaxes(
        title_text="altitude above home (m)", rangemode="tozero", row=2, col=1
    )
    figure.update_yaxes(title_text="3-D distance (m)", rangemode="tozero", row=2, col=2)
    figure.update_xaxes(gridcolor=GRID, zeroline=False, linecolor=GRID)
    figure.update_yaxes(gridcolor=GRID, zeroline=False, linecolor=GRID)
    figure.update_layout(
        height=950,
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font={"color": TEXT_PRIMARY, "family": "system-ui, sans-serif"},
        legend={"orientation": "h", "y": 1.06, "x": 0.0},
        margin={"t": 90, "l": 60, "r": 30, "b": 60},
        hovermode="closest",
    )
    return figure


def render_html(
    records: Sequence[Record],
    *,
    title: str,
    separation_threshold_m: float = DEFAULT_SEPARATION_THRESHOLD_M,
) -> str:
    figure = build_figure(records, separation_threshold_m=separation_threshold_m)
    summaries = summarize(records, separation_threshold_m=separation_threshold_m)
    text = format_comparison(summaries)
    live, baseline = split_live_and_baseline(summaries)
    if live is not None:
        text += "\n\n" + format_checklist(live.run, check_test_card(live, baseline))
    plot = figure.to_html(full_html=False, include_plotlyjs=True)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f"<style>body{{margin:0;padding:16px;background:{SURFACE};color:{TEXT_PRIMARY};"
        "font-family:system-ui,sans-serif}h1{font-size:20px;margin:0 0 4px}"
        f"p{{color:{TEXT_SECONDARY};margin:0 0 12px}}pre{{white-space:pre-wrap;"
        "font-size:13px;overflow-x:auto}</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        "<p>Live tracks are what each autopilot reported, including after the "
        "fail-stop. A live run is not reproducible; its numbers belong in "
        "docs/sitl.md, never in docs/measured_results.md.</p>"
        f"{plot}<h2>Comparison and test card</h2><pre>{html.escape(text)}</pre>"
        "</body></html>\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a live_mission evidence log as one offline HTML page."
    )
    parser.add_argument("log", help="JSON-lines evidence log")
    parser.add_argument(
        "--output", help="HTML file to write (default: the log path with .html)"
    )
    parser.add_argument(
        "--separation-threshold", type=float, default=DEFAULT_SEPARATION_THRESHOLD_M
    )
    arguments = parser.parse_args(argv)
    log = Path(arguments.log)
    output = Path(arguments.output) if arguments.output else log.with_suffix(".html")
    with log.open(encoding="utf-8") as stream:
        records: list[Any] = read_jsonl(stream)
    try:
        page = render_html(
            records,
            title=f"Multi-vehicle SITL mission: {log.name}",
            separation_threshold_m=arguments.separation_threshold,
        )
    except (ImportError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    output.write_text(page, encoding="utf-8")
    print(f"wrote {output}")
    return 0
