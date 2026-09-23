"""Offline chart rendering: pure figure construction, no Streamlit involved.

Plotly is an optional dependency (the ``charts`` extra), so every test here
is skipped when it is absent, the same pattern ``lease_timeout_chart.py``
itself uses to guard its own import.
"""

from __future__ import annotations

import pytest

pytest.importorskip("plotly")

from eudis_swarm.lease_timeout_chart import (  # noqa: E402
    build_figure,
    main,
    read_aggregate_csv,
    render_chart,
)


def _rows() -> list[dict[str, str]]:
    return [
        {
            "lease_timeout": "1.000000",
            "mean_duplicate_work_rate": "0.100000",
            "mean_abandoned_task_time": "2.000000",
        },
        {
            "lease_timeout": "2.000000",
            "mean_duplicate_work_rate": "0.200000",
            "mean_abandoned_task_time": "1.000000",
        },
    ]


def test_build_figure_plots_both_series_against_lease_timeout() -> None:
    figure = build_figure(_rows())

    assert len(figure.data) == 2  # type: ignore[arg-type]
    assert list(figure.data[0].x) == [1.0, 2.0]  # type: ignore[attr-defined]
    assert list(figure.data[0].y) == [0.1, 0.2]  # type: ignore[attr-defined]
    assert list(figure.data[1].y) == [2.0, 1.0]  # type: ignore[attr-defined]


def test_build_figure_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="no rows"):
        build_figure([])


def test_build_figure_rejects_rows_missing_required_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        build_figure([{"lease_timeout": "1.0"}])


def test_render_chart_writes_a_fully_offline_html_file(tmp_path) -> None:
    destination = tmp_path / "chart.html"

    render_chart(_rows(), destination)

    content = destination.read_text(encoding="utf-8")
    assert "Plotly.newPlot" in content
    # Fully offline: plotly.js must be embedded, not loaded from a CDN script.
    assert '<script src="https://cdn' not in content


# --------------------------------------------------------------------------
# CSV round trip and command line
# --------------------------------------------------------------------------


def _write_csv(path, rows: list[dict[str, str]]) -> None:
    import csv as csv_module

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv_module.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_read_aggregate_csv_round_trips_rows(tmp_path) -> None:
    source = tmp_path / "aggregate.csv"
    _write_csv(source, _rows())

    rows = read_aggregate_csv(source)

    assert rows == _rows()


def test_cli_renders_a_chart_file(tmp_path) -> None:
    source = tmp_path / "aggregate.csv"
    _write_csv(source, _rows())
    destination = tmp_path / "chart.html"

    exit_code = main([str(source), "--output", str(destination)])

    assert exit_code == 0
    assert destination.exists()
    assert "Plotly.newPlot" in destination.read_text(encoding="utf-8")


def test_cli_rejects_an_empty_csv(tmp_path) -> None:
    source = tmp_path / "empty.csv"
    source.write_text("lease_timeout\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        main([str(source)])

    assert exit_info.value.code == 2
