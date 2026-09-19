"""Offline chart: duplicate-work rate vs. abandoned-task time across lease timeout.

Reads a ``lease_timeout_study`` aggregate CSV and renders one self-contained,
fully offline HTML chart -- Plotly's JavaScript is embedded inline
(``include_plotlyjs=True``), so the file needs no network access to open or
view. Guarded on Plotly exactly the way ``dashboard.py`` guards Streamlit: a
missing optional dependency disables this module, it is not a hard
requirement of the package.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    import plotly.graph_objects as go

_REQUIRED_COLUMNS = frozenset(
    {"lease_timeout", "mean_duplicate_work_rate", "mean_abandoned_task_time"}
)


def read_aggregate_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def build_figure(rows: Sequence[dict[str, str]]) -> "go.Figure":
    if importlib.util.find_spec("plotly") is None:
        raise ImportError("plotly is unavailable; install the 'dashboard' extra")
    if not rows:
        raise ValueError("no rows to chart")
    missing = _REQUIRED_COLUMNS - set(rows[0])
    if missing:
        raise ValueError(f"input rows are missing columns: {sorted(missing)}")

    import plotly.graph_objects as go

    lease_timeouts = [float(row["lease_timeout"]) for row in rows]
    duplicate_work_rate = [float(row["mean_duplicate_work_rate"]) for row in rows]
    abandoned_task_time = [float(row["mean_abandoned_task_time"]) for row in rows]

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=lease_timeouts,
            y=duplicate_work_rate,
            name="Duplicate-work rate",
            mode="lines+markers",
            yaxis="y1",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=lease_timeouts,
            y=abandoned_task_time,
            name="Abandoned-task time (task-seconds)",
            mode="lines+markers",
            yaxis="y2",
        )
    )
    figure.update_layout(
        title="Duplicate-work rate vs. abandoned-task time across lease timeout",
        xaxis={"title": "Claim lease timeout (s)"},
        yaxis={
            "title": "Duplicate-work rate (concurrent-ownership / mission duration)"
        },
        yaxis2={
            "title": "Abandoned-task time (task-seconds)",
            "overlaying": "y",
            "side": "right",
        },
    )
    return figure


def render_chart(rows: Sequence[dict[str, str]], output_path: Path) -> None:
    figure = build_figure(rows)
    figure.write_html(str(output_path), include_plotlyjs=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eudis-swarm-lease-chart",
        description=(
            "Render an offline HTML chart from a lease_timeout_study aggregate CSV."
        ),
    )
    parser.add_argument(
        "input", type=Path, help="aggregate CSV produced by lease_timeout_study"
    )
    parser.add_argument("--output", type=Path, default=Path("lease_timeout_chart.html"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if importlib.util.find_spec("plotly") is None:
        _parser().error("plotly is unavailable; install the 'dashboard' extra")
    rows = read_aggregate_csv(arguments.input)
    if not rows:
        _parser().error(f"{arguments.input} has no data rows")
    render_chart(rows, arguments.output)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
