"""A vertical, reel-format animation of one recorded mission.

Turns a ``live_mission`` evidence log into a self-contained 1080x1920 HTML
page: a big thin title, one glowing top-down map of the swarm in the middle,
and a caption underneath that tells the story as it happens -- the work being
shared, a vehicle failing and being put into a safe hold, the others noticing
the silence, the orphaned task being taken over, the mission finishing. Every
number in a caption comes from the log, never from a script.

The page plays itself in a browser. ``window.renderAt(seconds)`` draws any
instant deterministically, which is how ``scripts/sitl_swarm_reel.py`` turns it
into an MP4 frame by frame. Standard library only; nothing in the simulator
imports this module.
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .live_chart import task_points, tracks
from .live_mission import Record
from .live_report import RunSummary, summarize

#: Dark-mode categorical slots, validated on a pure black surface (adjacent
#: CVD ΔE ≥ 8.4, all ≥ 3:1). Every vehicle is also labelled by name, so colour
#: never carries identity alone. Vehicles past the fourth slot are drawn grey.
REEL_SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500")
REEL_OTHER = "#8a8984"

#: Playback: logical seconds shown per reel second, and how long the reel
#: lingers on each story beat so a viewer can read the caption.
DEFAULT_PLAYBACK_RATE = 1.0
BEAT_HOLD_S = 2.2
INTRO_HOLD_S = 2.5
OUTRO_HOLD_S = 3.5


@dataclass(frozen=True, slots=True)
class Beat:
    """One caption, shown from logical time ``t`` until the next beat."""

    t: float
    text: str
    hold_s: float


def _color(index: int) -> str:
    return REEL_SERIES[index] if index < len(REEL_SERIES) else REEL_OTHER


def _pick_run(records: Sequence[Record]) -> tuple[list[Record], RunSummary]:
    """The live run if the log has one, else the first run."""

    summaries = summarize(records)
    chosen = next((s for s in summaries if not s.deterministic), summaries[0])
    run_records = [r for r in records if str(r.get("run")) == chosen.run]
    return run_records, chosen


def _first_event(steps: Sequence[Record], kind: str) -> Record | None:
    for step in steps:
        for event in step["events"]:
            if event["kind"] == kind:
                return event
    return None


def _noun(summary: RunSummary, count: int) -> str:
    kinds = {kind for _agent, kind in summary.vehicle_types}
    if kinds == {"copter"}:
        return "copter" if count == 1 else "copters"
    if kinds == {"rover"}:
        return "rover" if count == 1 else "rovers"
    return "vehicle" if count == 1 else "vehicles"


def story(records: Sequence[Record], summary: RunSummary) -> list[Beat]:
    """The captions, derived from the run's own events and summary."""

    steps = [r for r in records if r["type"] == "step"]
    count = summary.agent_count
    live = not summary.deterministic
    source = "ArduPilot" if live else "simulated"
    beats = [
        Beat(
            0.0,
            f"{count} {source} {_noun(summary, count)} share "
            f"{summary.total_task_count} tasks. There is no coordinator: each one "
            "claims work only from what its own radio has received.",
            INTRO_HOLD_S,
        )
    ]
    if summary.failed_agent_id is not None and summary.failure_time_s is not None:
        failed = summary.failed_agent_id
        stop = next((f for f in summary.fail_stops if f.agent_id == failed), None)
        if stop is not None and stop.displacement_after_hold_m is not None:
            text = (
                f"UAV {failed} fails. It is put into {stop.mode}, once, and still "
                f"travels {stop.displacement_after_hold_m:.0f} m before it stops."
            )
        else:
            text = f"UAV {failed} fails and goes silent."
        beats.append(Beat(summary.failure_time_s, text, BEAT_HOLD_S))
        declared = _first_event(steps, "FAILURE_DECLARED")
        if declared is not None:
            beats.append(
                Beat(
                    float(declared["t"]),
                    "The others notice the silence. A quorum declares it failed, "
                    "and its claim on the work is left to expire.",
                    BEAT_HOLD_S,
                )
            )
        if summary.recovered and summary.recovery_latency_s is not None:
            recovered_at = summary.failure_time_s + summary.recovery_latency_s
            taker = _recovering_agent(steps, summary, recovered_at)
            tasks = ", ".join(str(task) for task in summary.orphaned_task_ids)
            who = f"UAV {taker}" if taker is not None else "A survivor"
            beats.append(
                Beat(
                    recovered_at,
                    f"{who} takes over task {tasks}, "
                    f"{summary.recovery_latency_s:.2f} s after the failure. "
                    "Nobody told it to.",
                    BEAT_HOLD_S,
                )
            )
    ending = (
        f"{summary.completed_task_count}/{summary.total_task_count} tasks done in "
        f"{summary.mission_time_s:.2f} s, never two owners at once."
        if summary.concurrent_ownership_task_s == 0.0
        else f"{summary.completed_task_count}/{summary.total_task_count} tasks done "
        f"in {summary.mission_time_s:.2f} s."
    )
    if live:
        ending += " Real autopilot firmware; simulated airframes."
    beats.append(Beat(summary.mission_time_s, ending, OUTRO_HOLD_S))
    beats.sort(key=lambda beat: beat.t)
    return beats


def _recovering_agent(
    steps: Sequence[Record], summary: RunSummary, at: float
) -> int | None:
    orphaned = set(summary.orphaned_task_ids)
    for step in steps:
        if abs(float(step["t"]) - at) > 1e-9:
            continue
        for receiver, rows in sorted(step["ownership"].items()):
            if int(receiver) == summary.failed_agent_id:
                continue
            for task_id, state, _owner in rows:
                if task_id in orphaned and state == "OWNED_BY_SELF":
                    return int(receiver)
    return None


def timeline(beats: Sequence[Beat], end: float, rate: float) -> list[list[float]]:
    """Keyframes ``[reel_s, logical_s]``: steady playback, holding on each beat."""

    if rate <= 0.0:
        raise ValueError("playback rate must be positive")
    keys: list[list[float]] = [[0.0, 0.0]]
    reel = 0.0
    logical = 0.0
    for beat in beats:
        target = min(max(beat.t, logical), end)
        reel += (target - logical) / rate
        keys.append([reel, target])
        reel += beat.hold_s
        keys.append([reel, target])
        logical = target
    if logical < end:
        reel += (end - logical) / rate
        keys.append([reel, end])
    return [[round(r, 4), round(t, 4)] for r, t in keys]


def reel_data(
    records: Sequence[Record], *, rate: float = DEFAULT_PLAYBACK_RATE
) -> dict[str, Any]:
    """Everything the page needs, as plain JSON."""

    run_records, summary = _pick_run(records)
    header = next(r for r in run_records if r["type"] == "header")
    steps = [r for r in run_records if r["type"] == "step"]
    paths = tracks(run_records)
    tasks = task_points(run_records)
    kinds = dict(summary.vehicle_types)
    agents = [int(agent) for agent in header["agent_ids"]]

    xs = [p[1] for path in paths.values() for p in path] + [
        p[0] for p in tasks.values()
    ]
    ys = [p[2] for path in paths.values() for p in path] + [
        p[1] for p in tasks.values()
    ]
    config = header.get("config", {})
    xs += [0.0, float(config.get("area_width", 100.0))]
    ys += [0.0, float(config.get("area_height", 100.0))]
    side = max(max(xs) - min(xs), max(ys) - min(ys)) * 1.12
    center = ((max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0)

    completions = [
        [float(event["t"]), int(event["task_id"]), int(event["agent_id"])]
        for step in steps
        for event in step["events"]
        if event["kind"] == "TASK_COMPLETED" and event["task_id"] is not None
    ]
    beats = story(run_records, summary)
    end = summary.mission_time_s
    hold = next(
        (f for f in summary.fail_stops if f.agent_id == summary.failed_agent_id), None
    )
    return {
        "view": {"cx": center[0], "cy": center[1], "side": side},
        "vehicles": [
            {
                "id": agent,
                "kind": kinds.get(agent, "copter"),
                "color": _color(index),
                "path": [
                    [round(t, 4), round(x, 3), round(y, 3)] for t, x, y in paths[agent]
                ],
            }
            for index, agent in enumerate(agents)
            if agent in paths
        ],
        "tasks": [
            [task, round(x, 3), round(y, 3)] for task, (x, y) in sorted(tasks.items())
        ],
        "completions": completions,
        "failure": None
        if summary.failed_agent_id is None
        else {
            "agent": summary.failed_agent_id,
            "t": summary.failure_time_s,
            "mode": None if hold is None else hold.mode,
        },
        "beats": [{"t": beat.t, "text": beat.text} for beat in beats],
        "keys": timeline(beats, end, rate),
        "end": end,
        "live": not summary.deterministic,
        "run": summary.run,
    }


def render_reel_html(
    records: Sequence[Record],
    *,
    title: str = "Fail-stop",
    eyebrow: str = "Swarm coordination",
    footer: str | None = None,
    rate: float = DEFAULT_PLAYBACK_RATE,
    font_css: str | None = None,
) -> str:
    """The reel page. ``font_css`` inlines @font-face rules (fully offline);
    without it the page links Space Grotesk from Google Fonts and falls back
    to a system font when offline."""

    data = reel_data(records, rate=rate)
    if footer is None:
        footer = (
            "ArduPilot SITL · live telemetry · not reproducible"
            if data["live"]
            else "deterministic point-mass simulation"
        )
    payload = json.dumps(data, separators=(",", ":"), allow_nan=False)
    duration = data["keys"][-1][0] if data["keys"] else 0.0
    if not math.isfinite(duration):  # pragma: no cover - timeline() guards this
        raise ValueError("reel duration is not finite")
    fonts = (
        f"<style>{font_css}</style>"
        if font_css
        else (
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:'
            'wght@300;400&display=swap" rel="stylesheet">'
        )
    )
    return _PAGE.format(
        fonts=fonts,
        title=html.escape(title),
        eyebrow=html.escape(eyebrow),
        footer=html.escape(footer),
        data=payload.replace("</", "<\\/"),
    )


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{fonts}
<style>
:root {{ --bg:#000; --ink:#f4f4f2; --muted:#9a9993; --faint:#3a3a37; }}
html,body {{ margin:0; height:100%; background:var(--bg); overflow:hidden; }}
body {{ display:flex; align-items:center; justify-content:center; }}
#stage {{ width:1080px; height:1920px; position:relative; flex:none;
  color:var(--ink); font-family:"Space Grotesk",system-ui,"DejaVu Sans",sans-serif;
  transform-origin:center center; }}
#eyebrow {{ position:absolute; top:250px; width:100%; text-align:center;
  font-size:34px; letter-spacing:.18em; text-transform:uppercase; color:var(--muted);
  font-weight:400; }}
#title {{ position:absolute; top:300px; width:100%; text-align:center;
  font-size:150px; font-weight:300; letter-spacing:.01em; }}
#map {{ position:absolute; top:560px; left:60px; width:960px; height:960px; }}
#caption {{ position:absolute; top:1560px; left:90px; right:90px; text-align:center;
  font-size:44px; line-height:1.3; font-weight:400; transition:opacity .45s; }}
#clock {{ position:absolute; top:205px; right:70px; font-size:30px; color:var(--muted);
  font-variant-numeric:tabular-nums; }}
#footer {{ position:absolute; bottom:70px; width:100%; text-align:center; font-size:26px;
  color:var(--muted); letter-spacing:.06em; }}
.label {{ font:400 24px "Space Grotesk",system-ui,sans-serif; fill:var(--ink); }}
.task {{ font:400 20px "Space Grotesk",system-ui,sans-serif; fill:var(--muted); }}
</style></head>
<body><div id="stage">
<div id="clock"></div>
<div id="eyebrow">{eyebrow}</div>
<div id="title">{title}</div>
<svg id="map" viewBox="0 0 960 960" aria-label="top-down map of the mission">
<defs><filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
<feGaussianBlur stdDeviation="7" result="b"/>
<feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
</filter></defs>
<g id="grid"></g><g id="tasks"></g><g id="trails"></g><g id="marks"></g><g id="dots"></g>
</svg>
<div id="caption"></div>
<div id="footer">{footer}</div>
</div>
<script id="reel-data" type="application/json">{data}</script>
<script>
(function () {{
  const D = JSON.parse(document.getElementById("reel-data").textContent);
  const NS = "http://www.w3.org/2000/svg";
  const S = 960, v = D.view, scale = S / v.side;
  const px = (x) => (x - v.cx) * scale + S / 2;
  const py = (y) => S / 2 - (y - v.cy) * scale;
  const el = (tag, attrs, parent) => {{
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    parent.appendChild(n); return n;
  }};
  const g = (id) => document.getElementById(id);
  for (let i = 0; i <= 10; i++) {{
    const p = (i / 10) * S;
    el("line", {{x1: p, y1: 0, x2: p, y2: S, stroke: "#151514", "stroke-width": 1}}, g("grid"));
    el("line", {{x1: 0, y1: p, x2: S, y2: p, stroke: "#151514", "stroke-width": 1}}, g("grid"));
  }}
  const taskEls = {{}};
  for (const [id, x, y] of D.tasks) {{
    const box = el("rect", {{x: px(x) - 11, y: py(y) - 11, width: 22, height: 22, rx: 3,
      fill: "none", stroke: "#5c5b56", "stroke-width": 2}}, g("tasks"));
    const txt = el("text", {{x: px(x), y: py(y) + 38, "text-anchor": "middle", class: "task"}}, g("tasks"));
    txt.textContent = "T" + id;
    taskEls[id] = box;
  }}
  const veh = D.vehicles.map((V) => {{
    const trail = el("polyline", {{fill: "none", stroke: V.color, "stroke-width": 4,
      "stroke-linecap": "round", "stroke-linejoin": "round", filter: "url(#glow)", opacity: 0.95}}, g("trails"));
    const dot = el("circle", {{r: 13, fill: V.color, filter: "url(#glow)"}}, g("dots"));
    const ring = el("circle", {{r: 26, fill: "none", stroke: "#f4f4f2", "stroke-width": 3, opacity: 0}}, g("dots"));
    const label = el("text", {{class: "label", "text-anchor": "middle"}}, g("dots"));
    label.textContent = (V.kind === "rover" ? "UGV " : "UAV ") + V.id;
    return {{V, trail, dot, ring, label}};
  }});
  const failMark = el("text", {{class: "label", "text-anchor": "middle", opacity: 0,
    style: "font-size:44px"}}, g("marks"));
  failMark.textContent = "×";
  const caption = g("caption"), clock = g("clock");
  const lerp = (a, b, f) => a + (b - a) * f;
  function logicalAt(s) {{
    const K = D.keys;
    if (s <= K[0][0]) return K[0][1];
    for (let i = 1; i < K.length; i++) {{
      if (s <= K[i][0]) {{
        const span = K[i][0] - K[i - 1][0];
        return span <= 0 ? K[i][1] : lerp(K[i - 1][1], K[i][1], (s - K[i - 1][0]) / span);
      }}
    }}
    return K[K.length - 1][1];
  }}
  function posAt(path, t) {{
    if (t <= path[0][0]) return [path[0][1], path[0][2], 0];
    for (let i = 1; i < path.length; i++) {{
      if (t <= path[i][0]) {{
        const f = (t - path[i - 1][0]) / (path[i][0] - path[i - 1][0] || 1);
        return [lerp(path[i - 1][1], path[i][1], f), lerp(path[i - 1][2], path[i][2], f), i];
      }}
    }}
    const last = path[path.length - 1];
    return [last[1], last[2], path.length];
  }}
  let shownBeat = -1;
  window.renderAt = function (s) {{
    const t = logicalAt(s);
    clock.textContent = "t = " + t.toFixed(2) + " s";
    for (const {{V, trail, dot, ring, label}} of veh) {{
      const [x, y, i] = posAt(V.path, t);
      const pts = V.path.slice(0, i).map((p) => px(p[1]).toFixed(1) + "," + py(p[2]).toFixed(1));
      pts.push(px(x).toFixed(1) + "," + py(y).toFixed(1));
      trail.setAttribute("points", pts.join(" "));
      dot.setAttribute("cx", px(x)); dot.setAttribute("cy", py(y));
      ring.setAttribute("cx", px(x)); ring.setAttribute("cy", py(y));
      label.setAttribute("x", px(x)); label.setAttribute("y", py(y) - 30);
      const failed = D.failure && D.failure.agent === V.id && t >= D.failure.t;
      ring.setAttribute("opacity", failed ? 1 : 0);
      dot.setAttribute("fill", failed ? "#6b6a65" : V.color);
      if (failed) {{
        label.textContent = (V.kind === "rover" ? "UGV " : "UAV ") + V.id +
          (D.failure.mode ? " · " + D.failure.mode : " · failed");
        const [fx, fy] = posAt(V.path, D.failure.t);
        failMark.setAttribute("x", px(fx)); failMark.setAttribute("y", py(fy) + 15);
        failMark.setAttribute("opacity", 1);
      }} else {{
        label.textContent = (V.kind === "rover" ? "UGV " : "UAV ") + V.id;
        if (D.failure && D.failure.agent === V.id) failMark.setAttribute("opacity", 0);
      }}
    }}
    const done = new Set(D.completions.filter((c) => c[0] <= t).map((c) => c[1]));
    for (const id in taskEls) {{
      const box = taskEls[id];
      const isDone = done.has(Number(id));
      box.setAttribute("fill", isDone ? "#f4f4f2" : "none");
      box.setAttribute("stroke", isDone ? "#f4f4f2" : "#5c5b56");
    }}
    let beat = 0;
    for (let i = 0; i < D.beats.length; i++) if (t >= D.beats[i].t - 1e-9) beat = i;
    if (beat !== shownBeat) {{ caption.textContent = D.beats[beat].text; shownBeat = beat; }}
  }};
  window.reelDuration = D.keys.length ? D.keys[D.keys.length - 1][0] : 0;
  function fit() {{
    const k = Math.min(window.innerWidth / 1080, window.innerHeight / 1920);
    g("stage").style.transform = "scale(" + k + ")";
  }}
  window.addEventListener("resize", fit); fit();
  window.renderAt(0);
  if (/[?&]capture=1/.test(location.search)) {{
    caption.style.transition = "none";
  }} else {{
    let start = null;
    const tick = (now) => {{
      if (start === null) start = now;
      const s = ((now - start) / 1000) % (window.reelDuration + 1.5);
      window.renderAt(Math.min(s, window.reelDuration));
      requestAnimationFrame(tick);
    }};
    requestAnimationFrame(tick);
  }}
}})();
</script></body></html>
"""
