"""Measure and tune ArduCopter's horizontal position control in SITL.

Pure logic, no autopilot: the parameter space with hard safety bounds, the
metrics a flight's recorded telemetry yields (step response and braking), a
score with stability limits that disqualify a candidate outright, a staged
coordinate search, and the ``.parm`` file the result is written to.
``scripts/sitl_tune.py`` flies the test pattern on live SITL instances and
feeds the recordings through here.

What is tuned, and why only this:

* the horizontal position and velocity controllers -- ``PSC_NE_POS_P`` and
  ``PSC_NE_VEL_P/I/D``, the PIDs that decide overshoot, settling and
  steady-state error at a GUIDED target;
* the motion shaping that bounds them -- ``WP_ACC`` and ``WP_JERK`` (how hard
  a GUIDED leg accelerates and stops), ``PSC_NE_JERK`` (how fast deceleration
  builds up in BRAKE, the fail-stop mode) and ``ATC_ANGLE_MAX`` (the lean
  angle that caps every horizontal acceleration).

The inner attitude-rate loops (``ATC_RAT_*``) are left alone: they are matched
to the simulated airframe. Every result here is a tune **for ArduPilot's SITL
quad model**; a real airframe needs its own (AutoTune for the rate loops,
then these, conservatively) and must never be flown on these numbers.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

#: One telemetry report: (vehicle t, east, north, altitude, v_east, v_north).
Sample = tuple[float, float, float, float, float, float]


@dataclass(frozen=True, slots=True)
class Tunable:
    name: str
    default: float
    low: float
    high: float
    group: str
    meaning: str


#: Bounds are deliberately inside what ArduPilot accepts: a tune may only move
#: within a range that keeps the vehicle well-behaved, and a lean angle above
#: 35 degrees is not something a coordination demo needs.
TUNABLES: tuple[Tunable, ...] = (
    Tunable("PSC_NE_POS_P", 1.0, 0.5, 2.5, "pid", "position error -> velocity gain"),
    Tunable("PSC_NE_VEL_P", 2.0, 1.0, 4.0, "pid", "velocity error -> acceleration P"),
    Tunable("PSC_NE_VEL_I", 1.0, 0.2, 2.0, "pid", "velocity error integral"),
    Tunable("PSC_NE_VEL_D", 0.25, 0.0, 0.8, "pid", "velocity error derivative"),
    Tunable("WP_ACC", 2.5, 1.0, 5.0, "shaping", "GUIDED leg acceleration, m/s/s"),
    Tunable("WP_JERK", 1.0, 1.0, 10.0, "shaping", "GUIDED leg jerk, m/s/s/s"),
    Tunable(
        "PSC_NE_JERK", 5.0, 2.0, 20.0, "shaping", "position-controller jerk (BRAKE)"
    ),
    Tunable("ATC_ANGLE_MAX", 30.0, 20.0, 35.0, "shaping", "maximum lean angle, deg"),
)
TUNABLES_BY_NAME = {tunable.name: tunable for tunable in TUNABLES}
DEFAULTS: dict[str, float] = {t.name: t.default for t in TUNABLES}

#: A candidate is disqualified, whatever its score, if it breaks any of these.
#: They catch misbehaviour, not mediocrity: the SITL defaults overshoot a
#: 40 m step by about 0.7 m once and then creep back without ringing.
MAX_OVERSHOOT_M = 1.5
MAX_REVERSALS = 2
MAX_ALTITUDE_DEVIATION_M = 1.5
MAX_BRAKE_REBOUND_M = 0.5
MAX_FINAL_ERROR_M = 0.5

#: "Settled" means within this distance of the target and staying there.
SETTLE_RADIUS_M = 0.3

#: Score weights (lower total is better): one second of settle time is worth
#: 1/3 m of overshoot, 4 m of braking distance, or 0.25 m of final error.
W_SETTLE_TIME = 1.0
W_OVERSHOOT = 3.0
W_BRAKE = 0.25
W_FINAL_ERROR = 4.0


def validate_params(params: Mapping[str, float]) -> dict[str, float]:
    """Refuse an unknown name or a value outside its safety bounds."""

    checked: dict[str, float] = {}
    for name, value in params.items():
        tunable = TUNABLES_BY_NAME.get(name)
        if tunable is None:
            raise ValueError(f"{name} is not a tunable parameter")
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        if not tunable.low <= value <= tunable.high:
            raise ValueError(
                f"{name}={value:g} is outside its safe range "
                f"[{tunable.low:g}, {tunable.high:g}]"
            )
        checked[name] = float(value)
    return checked


@dataclass(frozen=True, slots=True)
class LegMetrics:
    """One GUIDED step from ``start`` to ``target``."""

    #: Vehicle seconds from the command to first coming within the radius.
    arrive_s: float | None
    #: Vehicle seconds from the command until it stays within SETTLE_RADIUS_M.
    settle_s: float | None
    #: Furthest the vehicle went past the target, along the leg direction.
    overshoot_m: float
    #: Largest sideways distance from the straight start-target line.
    cross_track_m: float
    #: Mean distance from the target over the final second.
    settle_error_m: float
    #: Along-track velocity sign changes after arrival: ringing, not creep.
    reversals: int
    #: Largest altitude excursion from the leg's starting altitude.
    altitude_deviation_m: float


@dataclass(frozen=True, slots=True)
class BrakeMetrics:
    """BRAKE commanded at speed: how far and how long until stopped."""

    speed_at_command: float
    stop_distance_m: float | None
    stop_time_s: float | None
    #: How far it came back toward the command point after stopping.
    rebound_m: float


def leg_metrics(
    samples: Sequence[Sample],
    start: tuple[float, float],
    target: tuple[float, float],
    *,
    command_t: float,
    arrive_radius_m: float = 1.0,
    hold_s: float = 4.0,
) -> LegMetrics:
    if not samples:
        raise ValueError("no samples for this leg")
    dx, dy = target[0] - start[0], target[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 0.0:
        raise ValueError("a leg needs distinct start and target")
    ux, uy = dx / length, dy / length
    arrive: float | None = None
    overshoot = 0.0
    cross = 0.0
    base_altitude = samples[0][3]
    altitude_dev = 0.0
    for t, x, y, z, _ve, _vn in samples:
        along = (x - start[0]) * ux + (y - start[1]) * uy
        side = -(x - start[0]) * uy + (y - start[1]) * ux
        overshoot = max(overshoot, along - length)
        cross = max(cross, abs(side))
        altitude_dev = max(altitude_dev, abs(z - base_altitude))
        if (
            arrive is None
            and math.hypot(x - target[0], y - target[1]) <= arrive_radius_m
        ):
            arrive = t - command_t
    end_t = samples[-1][0]
    window = [s for s in samples if s[0] >= end_t - 1.0]
    errors = [math.hypot(s[1] - target[0], s[2] - target[1]) for s in window]
    settle: float | None = None
    for t, x, y, _z, _ve, _vn in samples:
        inside = math.hypot(x - target[0], y - target[1]) <= SETTLE_RADIUS_M
        if inside and settle is None:
            settle = t - command_t
        elif not inside:
            settle = None
    reversals = 0
    if arrive is not None:
        sign = 0
        for t, _x, _y, _z, ve, vn in samples:
            if t - command_t < arrive:
                continue
            along_v = ve * ux + vn * uy
            if abs(along_v) < 0.1:
                continue
            current = 1 if along_v > 0 else -1
            if sign and current != sign:
                reversals += 1
            sign = current
    return LegMetrics(
        arrive_s=arrive,
        settle_s=settle,
        overshoot_m=overshoot,
        cross_track_m=cross,
        settle_error_m=sum(errors) / len(errors),
        reversals=reversals,
        altitude_deviation_m=altitude_dev,
    )


def brake_metrics(
    samples: Sequence[Sample],
    *,
    command_t: float,
    stopped_below_mps: float = 0.2,
) -> BrakeMetrics:
    after = [s for s in samples if s[0] >= command_t]
    if not after:
        raise ValueError("no samples after the brake command")
    first = after[0]
    speed0 = math.hypot(first[4], first[5])
    stop: Sample | None = None
    for sample in after:
        if math.hypot(sample[4], sample[5]) < stopped_below_mps:
            stop = sample
            break
    if stop is None:
        return BrakeMetrics(speed0, None, None, 0.0)
    distance = math.hypot(stop[1] - first[1], stop[2] - first[2])
    furthest = max(math.hypot(s[1] - first[1], s[2] - first[2]) for s in after)
    final = math.hypot(after[-1][1] - first[1], after[-1][2] - first[2])
    return BrakeMetrics(
        speed_at_command=speed0,
        stop_distance_m=furthest,
        stop_time_s=stop[0] - first[0],
        rebound_m=max(0.0, furthest - final) if distance > 0.0 else 0.0,
    )


@dataclass(frozen=True, slots=True)
class TrialResult:
    params: dict[str, float]
    legs: tuple[LegMetrics, ...]
    brake: BrakeMetrics | None
    error: str | None = None
    notes: tuple[str, ...] = field(default=())


def disqualifications(result: TrialResult) -> list[str]:
    """Every stability or completeness limit this trial broke."""

    if result.error is not None:
        return [f"flight failed: {result.error}"]
    reasons: list[str] = []
    if not result.legs or any(leg.settle_s is None for leg in result.legs):
        reasons.append(f"a leg never settled within {SETTLE_RADIUS_M:g} m")
    for index, leg in enumerate(result.legs, start=1):
        if leg.overshoot_m > MAX_OVERSHOOT_M:
            reasons.append(f"leg {index} overshoot {leg.overshoot_m:.2f} m")
        if leg.reversals > MAX_REVERSALS:
            reasons.append(f"leg {index} rings ({leg.reversals} reversals)")
        if leg.settle_error_m > MAX_FINAL_ERROR_M:
            reasons.append(f"leg {index} final error {leg.settle_error_m:.2f} m")
        if leg.altitude_deviation_m > MAX_ALTITUDE_DEVIATION_M:
            reasons.append(
                f"leg {index} altitude off by {leg.altitude_deviation_m:.2f} m"
            )
    if result.brake is None or result.brake.stop_distance_m is None:
        reasons.append("BRAKE never stopped the vehicle")
    elif result.brake.rebound_m > MAX_BRAKE_REBOUND_M:
        reasons.append(f"BRAKE rebound {result.brake.rebound_m:.2f} m")
    return reasons


def score(result: TrialResult) -> float:
    """Lower is better; ``inf`` for a disqualified trial."""

    if disqualifications(result):
        return math.inf
    assert result.brake is not None and result.brake.stop_distance_m is not None
    settle = [leg.settle_s for leg in result.legs if leg.settle_s is not None]
    return (
        W_SETTLE_TIME * (sum(settle) / len(settle))
        + W_OVERSHOOT * max(leg.overshoot_m for leg in result.legs)
        + W_FINAL_ERROR * max(leg.settle_error_m for leg in result.legs)
        + W_BRAKE * result.brake.stop_distance_m
    )


#: The values the coordinate search tries for each parameter, per stage.
SEARCH_GRID: dict[str, tuple[float, ...]] = {
    "PSC_NE_POS_P": (0.75, 1.0, 1.5, 2.0),
    "PSC_NE_VEL_P": (1.5, 2.0, 3.0, 4.0),
    "PSC_NE_VEL_I": (0.5, 1.0, 1.5),
    "PSC_NE_VEL_D": (0.0, 0.25, 0.5),
    "WP_ACC": (2.5, 3.5, 5.0),
    "WP_JERK": (1.0, 2.5, 5.0, 10.0),
    "PSC_NE_JERK": (5.0, 10.0, 20.0),
    "ATC_ANGLE_MAX": (30.0, 35.0),
}
STAGES: dict[str, tuple[str, ...]] = {
    "pid": ("PSC_NE_POS_P", "PSC_NE_VEL_P", "PSC_NE_VEL_I", "PSC_NE_VEL_D"),
    "shaping": ("WP_ACC", "WP_JERK", "PSC_NE_JERK", "ATC_ANGLE_MAX"),
}


def neighbours(
    base: Mapping[str, float], names: Iterable[str]
) -> list[dict[str, float]]:
    """Every candidate that changes exactly one of ``names`` from ``base``."""

    candidates: list[dict[str, float]] = []
    for name in names:
        for value in SEARCH_GRID[name]:
            if math.isclose(value, base[name]):
                continue
            candidate = dict(base)
            candidate[name] = value
            candidates.append(validate_params(candidate))
    return candidates


def best(results: Iterable[TrialResult]) -> TrialResult | None:
    scored = [(score(result), index, result) for index, result in enumerate(results)]
    finite = [item for item in scored if math.isfinite(item[0])]
    if not finite:
        return None
    return min(finite)[2]


def format_trial(result: TrialResult) -> str:
    changed = ", ".join(
        f"{name}={value:g}"
        for name, value in sorted(result.params.items())
        if not math.isclose(value, DEFAULTS[name])
    )
    reasons = disqualifications(result)
    if reasons:
        return f"[{changed or 'defaults'}] DISQUALIFIED: {'; '.join(reasons)}"
    assert result.brake is not None and result.brake.stop_distance_m is not None
    arrive = [leg.arrive_s for leg in result.legs if leg.arrive_s is not None]
    settle = [leg.settle_s for leg in result.legs if leg.settle_s is not None]
    return (
        f"[{changed or 'defaults'}] score {score(result):.2f}: arrive "
        f"{sum(arrive) / len(arrive):.2f} s, settled "
        f"{sum(settle) / len(settle):.2f} s, final "
        f"{max(leg.settle_error_m for leg in result.legs):.2f} m, overshoot "
        f"{max(leg.overshoot_m for leg in result.legs):.2f} m, brake "
        f"{result.brake.stop_distance_m:.1f} m from "
        f"{result.brake.speed_at_command:.1f} m/s in {result.brake.stop_time_s:.1f} s"
    )


def parm_file(params: Mapping[str, float], header: Sequence[str]) -> str:
    """An ArduPilot ``.parm`` file: ``NAME value`` lines, ``#`` comments."""

    validate_params(params)
    lines = [f"# {line}" if line else "#" for line in header]
    lines.extend(f"{name} {params[name]:g}" for name in sorted(params))
    return "\n".join(lines) + "\n"


def read_parm(text: str) -> dict[str, float]:
    """Parse a ``.parm`` file this module wrote (or any ``NAME value`` list)."""

    params: dict[str, float] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(",", " ").split()
        if len(parts) != 2:
            raise ValueError(f"line {number}: expected NAME VALUE")
        try:
            params[parts[0]] = float(parts[1])
        except ValueError as error:
            raise ValueError(f"line {number}: {error}") from error
    return params


@dataclass(frozen=True, slots=True)
class Confirmation:
    """Repeat flights of one parameter set, summarised for a decision."""

    flown: int
    attempted: int
    #: Mean score over the flights that completed; ``inf`` if any completed
    #: flight was disqualified or fewer than half completed.
    mean_score: float
    mean_settle_s: float | None
    mean_overshoot_m: float | None
    mean_brake_m: float | None


def confirm(results: Sequence[TrialResult]) -> Confirmation:
    """Summarise repeat flights.

    A flight that failed for a harness reason (a lost parameter echo, a link
    hiccup) is left out rather than counted as a bad tune, but at least half
    must complete; a completed flight that breaks a stability limit makes the
    whole set fail -- one ringing flight is enough to reject a tune.
    """

    flown = [r for r in results if r.error is None]
    if not flown or 2 * len(flown) < len(results):
        return Confirmation(len(flown), len(results), math.inf, None, None, None)
    scores = [score(r) for r in flown]
    if any(math.isinf(value) for value in scores):
        return Confirmation(len(flown), len(results), math.inf, None, None, None)

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    return Confirmation(
        flown=len(flown),
        attempted=len(results),
        mean_score=mean(scores),
        mean_settle_s=mean(
            [
                mean([leg.settle_s for leg in r.legs if leg.settle_s is not None])
                for r in flown
            ]
        ),
        mean_overshoot_m=mean([max(leg.overshoot_m for leg in r.legs) for r in flown]),
        mean_brake_m=mean(
            [
                r.brake.stop_distance_m
                for r in flown
                if r.brake and r.brake.stop_distance_m
            ]
        ),
    )


def format_confirmation(label: str, summary: Confirmation) -> str:
    if math.isinf(summary.mean_score):
        return (
            f"{label}: FAILED confirmation ({summary.flown}/{summary.attempted} flights "
            "completed, or a completed flight broke a limit)"
        )
    return (
        f"{label}: score {summary.mean_score:.2f} over {summary.flown}/{summary.attempted} "
        f"flights; settle {summary.mean_settle_s:.2f} s, overshoot "
        f"{summary.mean_overshoot_m:.2f} m, brake {summary.mean_brake_m:.1f} m"
    )
