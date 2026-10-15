"""Derive every figure table and number in the paper from committed evidence.

Reads the recorded SITL evidence logs and tuning trials under docs/data/sitl,
and runs the deterministic simulator for the point-mass reference figures.
Writes CSV files and ``numbers.tex`` (one LaTeX macro per quoted number) into
paper/data. Nothing here is typed in by hand except where the source is a
table in docs/measured_results.md, which is named next to it.

    python paper/tools/make_data.py
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from dataclasses import replace
from pathlib import Path

from eudis_swarm.config import SimulationConfig
from eudis_swarm.live_chart import task_points, tracks
from eudis_swarm.live_mission import read_jsonl, run_logged
from eudis_swarm.live_report import RunSummary, split_live_and_baseline, summarize

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "docs" / "data" / "sitl"
OUT = ROOT / "paper" / "data"

macros: dict[str, str] = {}


def macro(name: str, value: float | int | str, fmt: str = "{:.2f}") -> None:
    if not name.isalpha():
        raise ValueError(f"LaTeX macro names are letters only: {name}")
    macros[name] = value if isinstance(value, str) else fmt.format(value)


def load(path: Path) -> tuple[RunSummary, RunSummary, list]:
    with path.open(encoding="utf-8") as stream:
        records = read_jsonl(stream)
    live, baseline = split_live_and_baseline(summarize(records))
    assert live is not None and baseline is not None
    return live, baseline, records


def write_csv(name: str, header: list[str], rows: list[tuple]) -> None:
    with (OUT / name).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def span(values: list[float], fmt: str = "{:.2f}") -> str:
    low, high = min(values), max(values)
    return (
        fmt.format(low)
        if math.isclose(low, high)
        else f"{fmt.format(low)}--{fmt.format(high)}"
    )


def run_set(prefix: str, paths: list[Path]) -> list[RunSummary]:
    lives = [load(path)[0] for path in paths]
    rows = {
        "Mission": [s.mission_time_s for s in lives],
        "Recovery": [s.recovery_latency_s or math.nan for s in lives],
        "Brake": [s.fail_stops[0].displacement_after_hold_m or math.nan for s in lives],
        "Sep": [s.min_separation_m or math.nan for s in lives],
        "Drift": [max(v.max_drift_s for v in s.vehicles) for s in lives],
    }
    for key, values in rows.items():
        fmt = "{:.1f}" if key in ("Brake", "Sep") else "{:.2f}"
        macro(prefix + key, span(values, fmt))
        macro(prefix + key + "Mean", statistics.mean(values), fmt)
    macro(prefix + "Runs", len(lives), "{}")
    macro(prefix + "Pass", sum(1 for s in lives if s.mission_completed), "{}")
    return lives


def trajectories() -> None:
    _live, _baseline, records = load(DATA / "phase5" / "c5.jsonl")
    for run in ("sitl", "point-mass"):
        run_records = [r for r in records if r.get("run") == run]
        for agent, path in sorted(tracks(run_records).items()):
            tag = "live" if run == "sitl" else "pm"
            write_csv(
                f"traj_{tag}_{agent}.csv",
                ["t", "x", "y"],
                [(round(t, 3), round(x, 3), round(y, 3)) for t, x, y in path],
            )
        if run == "sitl":
            tasks = task_points(run_records)
            write_csv(
                "tasks.csv",
                ["task", "x", "y"],
                [
                    (task, round(x, 3), round(y, 3))
                    for task, (x, y) in sorted(tasks.items())
                ],
            )
            fail_t = next(
                e["t"]
                for r in run_records
                if r["type"] == "step"
                for e in r["events"]
                if e["kind"] == "FAILURE_INJECTED"
            )
            path = tracks(run_records)[2]
            at_fail = [p for p in path if p[0] <= fail_t][-1]
            rest = path[-1]
            write_csv(
                "failure.csv",
                ["label", "x", "y"],
                [
                    ("fail", round(at_fail[1], 3), round(at_fail[2], 3)),
                    ("rest", round(rest[1], 3), round(rest[2], 3)),
                ],
            )
    # Altitude bands and per-step drift of the same live run.
    for agent in (1, 2, 3, 4):
        rows = []
        for step in records:
            if step.get("run") != "sitl" or step["type"] != "step":
                continue
            for vehicle in step["vehicles"]:
                if vehicle["id"] == agent and not vehicle["held"]:
                    rows.append(
                        (
                            step["t"],
                            round(vehicle["altitude_m"], 3),
                            round(vehicle["drift_s"], 4),
                        )
                    )
        write_csv(f"alt_{agent}.csv", ["t", "alt", "drift"], rows)


def deterministic() -> None:
    """Point-mass reference numbers, recomputed now (deterministic)."""

    base = replace(
        SimulationConfig(),
        agent_count=4,
        task_count=8,
        task_control="distributed",
        max_simulation_time=240.0,
        completion_tolerance=2.0,
        failure_agent_id=2,
        failure_time=4.0,
    )
    _result, records = run_logged(base, run_label="point-mass")
    (summary,) = summarize(records)
    macro("pmMission", summary.mission_time_s)
    macro("pmRecovery", summary.recovery_latency_s or math.nan)
    macro("pmDetection", summary.failure_detection_latency_s or math.nan)
    default_result, _ = run_logged(SimulationConfig(), run_label="default")
    macro("defaultMission", default_result.metrics.simulation_duration)


def tuning() -> None:
    def confirm(stage: str) -> list[dict]:
        rows = []
        with (DATA / "tuning" / "search2-trials.jsonl").open(
            encoding="utf-8"
        ) as stream:
            for line in stream:
                record = json.loads(line)
                if record["stage"] == stage and record["error"] is None:
                    rows.append(record)
        return rows

    for prefix, stage in (("stock", "confirm-defaults"), ("tuned", "confirm-best")):
        rows = confirm(stage)
        settle = [statistics.mean(leg["settle_s"] for leg in r["legs"]) for r in rows]
        overshoot = [max(leg["overshoot_m"] for leg in r["legs"]) for r in rows]
        brake = [r["brake"]["stop_distance_m"] for r in rows]
        brake_t = [r["brake"]["stop_time_s"] for r in rows]
        macro(prefix + "TuneN", len(rows), "{}")
        macro(prefix + "Settle", statistics.mean(settle))
        macro(prefix + "Overshoot", statistics.mean(overshoot))
        macro(prefix + "BrakeDist", statistics.mean(brake), "{:.1f}")
        macro(prefix + "BrakeTime", statistics.mean(brake_t), "{:.1f}")
    trials = sum(1 for _ in (DATA / "tuning" / "search2-trials.jsonl").open()) + sum(
        1 for _ in (DATA / "tuning" / "search-trials.jsonl").open()
    )
    macro("tuneTrials", trials, "{}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    trajectories()
    deterministic()
    tuning()
    stock = run_set(
        "stock", [DATA / "phase5" / f"{r}.jsonl" for r in ("c5", "c5b", "c5c")]
    )
    run_set("tuned", [DATA / "phase5-tuned" / f"tuned{i}.jsonl" for i in (1, 2, 3)])
    mixed, _, _ = load(DATA / "phase5" / "mix5.jsonl")
    macro("mixMission", mixed.mission_time_s)
    macro("mixRecovery", mixed.recovery_latency_s or math.nan)
    macro(
        "mixBrake", mixed.fail_stops[0].displacement_after_hold_m or math.nan, "{:.1f}"
    )
    macro("mixSep", mixed.min_separation_m or math.nan, "{:.1f}")
    jam, jam_pm, _ = load(DATA / "phase5" / "jam5.jsonl")
    macro("jamMission", jam.mission_time_s)
    macro("jamDetection", jam.failure_detection_latency_s or math.nan)
    macro("jamRecovery", jam.recovery_latency_s or math.nan)
    macro("jamPmOwnership", jam_pm.concurrent_ownership_task_s)
    macro(
        "stockModelRatio",
        statistics.mean(s.mission_time_s for s in stock) / float(macros["pmMission"]),
    )
    macro(
        "tunedModelRatio",
        float(macros["tunedMissionMean"]) / float(macros["pmMission"]),
    )
    with (OUT / "numbers.tex").open("w", encoding="utf-8") as stream:
        stream.write(
            "% Generated by paper/tools/make_data.py -- do not edit by hand.\n"
        )
        for name, value in sorted(macros.items()):
            stream.write(f"\\newcommand{{\\{name}}}{{{value}}}\n")
    print(f"wrote {len(macros)} macros and the figure tables to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
