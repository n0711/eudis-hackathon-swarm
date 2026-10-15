"""Tuning logic on synthetic telemetry: metrics, limits, score, search, files."""

from __future__ import annotations

import math

import pytest

from eudis_swarm.sitl_tuning import (
    DEFAULTS,
    MAX_OVERSHOOT_M,
    SEARCH_GRID,
    STAGES,
    TUNABLES,
    BrakeMetrics,
    LegMetrics,
    TrialResult,
    best,
    brake_metrics,
    disqualifications,
    format_trial,
    leg_metrics,
    neighbours,
    parm_file,
    read_parm,
    score,
    validate_params,
)


def _step(
    *,
    overshoot: float = 0.7,
    tau: float = 2.0,
    ring: bool = False,
    length: float = 40.0,
) -> list[tuple[float, float, float, float, float, float]]:
    """A 40 m east leg: constant 8 m/s, one overshoot, then a creep or a ring."""

    samples = []
    dt = 0.05
    t = 0.0
    arrive = length / 8.0
    while t <= 20.0:
        if t < arrive:
            x, v = 8.0 * t, 8.0
        else:
            k = t - arrive
            if ring:
                x = length + overshoot * math.cos(2.0 * k) * math.exp(-0.05 * k)
                v = -2.0 * overshoot * math.sin(2.0 * k)
            else:
                x = length + overshoot * math.exp(-k / tau)
                v = -overshoot / tau * math.exp(-k / tau)
        samples.append((round(t, 3), x, 0.0, 20.0, v, 0.0))
        t += dt
    return samples


def test_a_creeping_step_is_measured_not_mistaken_for_ringing() -> None:
    leg = leg_metrics(_step(), (0.0, 0.0), (40.0, 0.0), command_t=0.0)
    assert leg.arrive_s == pytest.approx(39.0 / 8.0, abs=0.06)
    assert leg.overshoot_m == pytest.approx(0.7, abs=0.03)  # sampled at 20 Hz
    assert leg.reversals == 1  # overshoot, then creep back: one reversal, not ringing
    # Settled once 0.7 * exp(-k / 2) <= 0.3: k = 2 ln(7/3).
    assert leg.settle_s == pytest.approx(5.0 + 2.0 * math.log(7 / 3), abs=0.06)
    assert leg.settle_error_m < 0.01
    assert leg.cross_track_m == 0.0 and leg.altitude_deviation_m == 0.0


def test_ringing_counts_reversals_and_never_settles_inside_the_radius() -> None:
    leg = leg_metrics(_step(ring=True), (0.0, 0.0), (40.0, 0.0), command_t=0.0)
    assert leg.reversals > 2
    # A ring can pass through the radius; it is the reversals that disqualify.
    trial = TrialResult(dict(DEFAULTS), (leg,), brake_metrics(_brake(), command_t=1.0))
    assert any("rings" in reason for reason in disqualifications(trial))


def test_cross_track_and_altitude_excursions_are_seen() -> None:
    samples = [
        (t, x, 1.5 if x > 10 else 0.0, 20.0 + (2.0 if x > 20 else 0.0), 8.0, 0.0)
        for t, x, *_ in _step()
    ]
    leg = leg_metrics(samples, (0.0, 0.0), (40.0, 0.0), command_t=0.0)
    assert leg.cross_track_m == 1.5 and leg.altitude_deviation_m == 2.0


def test_leg_metrics_refuse_degenerate_input() -> None:
    with pytest.raises(ValueError, match="no samples"):
        leg_metrics([], (0.0, 0.0), (1.0, 0.0), command_t=0.0)
    with pytest.raises(ValueError, match="distinct"):
        leg_metrics(_step(), (1.0, 1.0), (1.0, 1.0), command_t=0.0)


def _brake(decel: float = 4.0, rebound: float = 0.0):  # type: ignore[no-untyped-def]
    samples = []
    v0, t = 8.0, 0.0
    stop_t = v0 / decel
    while t <= 6.0:
        if t < stop_t:
            x, v = v0 * t - 0.5 * decel * t * t, v0 - decel * t
        else:
            x, v = v0 * stop_t - 0.5 * decel * stop_t**2, 0.0
            x -= rebound * min(1.0, (t - stop_t))
        samples.append((round(1.0 + t, 3), x, 0.0, 20.0, v, 0.0))
        t += 0.05
    return [(0.5, -4.0, 0.0, 20.0, 8.0, 0.0), *samples]


def test_brake_distance_time_and_rebound() -> None:
    metrics = brake_metrics(_brake(decel=4.0), command_t=1.0)
    assert metrics.speed_at_command == 8.0
    assert metrics.stop_distance_m == pytest.approx(8.0, abs=0.05)  # v^2 / 2a
    assert metrics.stop_time_s == pytest.approx(1.95, abs=0.06)
    assert metrics.rebound_m == 0.0
    bouncy = brake_metrics(_brake(decel=4.0, rebound=0.8), command_t=1.0)
    assert bouncy.rebound_m == pytest.approx(0.8, abs=0.01)
    never = brake_metrics(
        [(1.0, 0.0, 0.0, 20.0, 8.0, 0.0), (2.0, 8.0, 0.0, 20.0, 8.0, 0.0)],
        command_t=1.0,
    )
    assert never.stop_distance_m is None and never.stop_time_s is None
    with pytest.raises(ValueError, match="after the brake"):
        brake_metrics(_brake(), command_t=99.0)


def _trial(**leg_changes: object) -> TrialResult:
    leg = leg_metrics(_step(), (0.0, 0.0), (40.0, 0.0), command_t=0.0)
    from dataclasses import replace

    leg = replace(leg, **leg_changes)  # type: ignore[arg-type]
    brake = brake_metrics(_brake(), command_t=1.0)
    return TrialResult(dict(DEFAULTS), (leg, leg, leg, leg), brake)


def test_the_score_rewards_settling_and_braking_and_disqualifies_misbehaviour() -> None:
    good = _trial()
    assert disqualifications(good) == []
    leg = good.legs[0]
    assert leg.settle_s is not None
    assert score(good) == pytest.approx(
        leg.settle_s + 3.0 * leg.overshoot_m + 4.0 * leg.settle_error_m + 0.25 * 8.0,
        abs=0.05,
    )
    slower = _trial(settle_s=leg.settle_s + 2.0)
    assert score(slower) > score(good)
    assert best([slower, good]) is good

    for changes, reason in (
        ({"overshoot_m": MAX_OVERSHOOT_M + 0.1}, "overshoot"),
        ({"reversals": 3}, "rings"),
        ({"altitude_deviation_m": 2.0}, "altitude"),
        ({"settle_error_m": 0.6}, "final error"),
        ({"settle_s": None}, "never settled"),
    ):
        bad = _trial(**changes)
        assert any(reason in r for r in disqualifications(bad)), reason
        assert math.isinf(score(bad))
        assert "DISQUALIFIED" in format_trial(bad)
    assert best([_trial(reversals=5)]) is None

    no_brake = TrialResult(
        dict(DEFAULTS), good.legs, BrakeMetrics(8.0, None, None, 0.0)
    )
    assert "BRAKE never stopped" in disqualifications(no_brake)[0]
    bounce = TrialResult(dict(DEFAULTS), good.legs, BrakeMetrics(8.0, 9.0, 2.0, 0.9))
    assert "rebound" in disqualifications(bounce)[0]
    crashed = TrialResult(dict(DEFAULTS), (), None, error="link lost")
    assert disqualifications(crashed) == ["flight failed: link lost"]


def test_format_names_only_what_changed() -> None:
    trial = _trial()
    assert format_trial(trial).startswith("[defaults] score")
    tuned = TrialResult({**DEFAULTS, "PSC_NE_POS_P": 1.5}, trial.legs, trial.brake)
    assert format_trial(tuned).startswith("[PSC_NE_POS_P=1.5] score")


def test_every_tunable_has_safe_bounds_and_a_grid_inside_them() -> None:
    names = {t.name for t in TUNABLES}
    assert set(SEARCH_GRID) == names == {n for stage in STAGES.values() for n in stage}
    for tunable in TUNABLES:
        assert tunable.low <= tunable.default <= tunable.high
        assert all(tunable.low <= v <= tunable.high for v in SEARCH_GRID[tunable.name])
    assert DEFAULTS["ATC_ANGLE_MAX"] == 30.0
    assert max(SEARCH_GRID["ATC_ANGLE_MAX"]) <= 35.0  # no aerobatic lean angles


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"ATC_RAT_RLL_P": 0.2}, "not a tunable"),
        ({"ATC_ANGLE_MAX": 45.0}, "outside its safe range"),
        ({"PSC_NE_VEL_D": -0.1}, "outside its safe range"),
        ({"WP_ACC": float("nan")}, "finite"),
    ],
)
def test_unsafe_or_unknown_parameters_are_refused(params, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match=message):
        validate_params(params)


def test_neighbours_change_exactly_one_parameter_of_a_stage() -> None:
    candidates = neighbours(DEFAULTS, STAGES["pid"])
    expected = sum(
        sum(1 for v in SEARCH_GRID[n] if not math.isclose(v, DEFAULTS[n]))
        for n in STAGES["pid"]
    )
    assert len(candidates) == expected
    for candidate in candidates:
        changed = [n for n in candidate if not math.isclose(candidate[n], DEFAULTS[n])]
        assert len(changed) == 1 and changed[0] in STAGES["pid"]


def test_parm_files_round_trip_and_reject_unsafe_values() -> None:
    params = {**DEFAULTS, "PSC_NE_POS_P": 1.5, "WP_JERK": 5.0}
    text = parm_file(params, ["SITL only", ""])
    assert text.startswith("# SITL only\n#\n")
    assert "PSC_NE_POS_P 1.5\n" in text
    assert read_parm(text) == params
    assert read_parm("A,1\n# c\n\nB 2 # trailing\n") == {"A": 1.0, "B": 2.0}
    with pytest.raises(ValueError, match="line 1"):
        read_parm("JUST_A_NAME\n")
    with pytest.raises(ValueError, match="line 1"):
        read_parm("NAME notanumber\n")
    with pytest.raises(ValueError, match="safe range"):
        parm_file({"ATC_ANGLE_MAX": 60.0}, [])


def test_leg_metrics_dataclass_is_frozen() -> None:
    leg = _trial().legs[0]
    assert isinstance(leg, LegMetrics)
    with pytest.raises(AttributeError):
        leg.overshoot_m = 0.0  # type: ignore[misc]


def test_confirmation_skips_harness_failures_but_not_misbehaviour() -> None:
    from eudis_swarm.sitl_tuning import confirm, format_confirmation

    good = _trial()
    failed = TrialResult(dict(DEFAULTS), (), None, error="parameter echo lost")
    summary = confirm([good, good, failed])
    assert (summary.flown, summary.attempted) == (2, 3)
    assert summary.mean_score == pytest.approx(score(good))
    assert summary.mean_brake_m == pytest.approx(8.0, abs=0.05)
    assert "over 2/3 flights" in format_confirmation("tuned", summary)

    assert math.isinf(confirm([good, failed, failed]).mean_score)  # under half flew
    assert math.isinf(confirm([]).mean_score)
    ringing = _trial(reversals=4)
    rejected = confirm([good, good, ringing])
    assert math.isinf(rejected.mean_score)  # one ringing flight rejects the tune
    assert "FAILED confirmation" in format_confirmation("tuned", rejected)


def test_the_committed_tune_is_inside_the_safety_bounds_and_says_sitl_only() -> None:
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "config" / "sitl" / "copter-tuned.parm"
    text = path.read_text(encoding="utf-8")
    params = validate_params(read_parm(text))
    assert set(params) == set(DEFAULTS)
    assert "SITL quad model ONLY" in text
    assert params["ATC_ANGLE_MAX"] <= 35.0
