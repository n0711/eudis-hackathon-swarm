"""The vehicle motion model, and the assumption SITL will break.

Motion enters the system twice: once to move an aircraft, and once to predict
where one would be so the connectivity-aware allocator can evaluate a future
link graph. Today both are the same arithmetic, so prediction cannot be wrong.

Under a real autopilot they stop being the same thing. These tests state the
equivalence explicitly, so that when it ends it ends visibly rather than
quietly. See docs/sitl.md.
"""

from __future__ import annotations

import pytest

from eudis_swarm.agent import Agent, advance_point_mass


def test_prediction_and_movement_are_the_same_model() -> None:
    """The invariant a MAVLink backend removes: projecting equals moving."""

    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)
    target = (100.0, 0.0)
    elapsed = 1.25

    predicted = advance_point_mass(agent.position, target, agent.speed, elapsed)
    agent.move_toward(target, elapsed)

    assert agent.position == predicted


def test_a_vehicle_never_overshoots_its_target() -> None:
    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)
    target = (5.0, 0.0)

    agent.move_toward(target, 100.0)

    assert agent.position == target
    assert advance_point_mass((0.0, 0.0), target, 10.0, 100.0) == target


@pytest.mark.parametrize("elapsed", [0.0, 0.5, 3.0])
def test_a_vehicle_already_at_its_target_does_not_drift(elapsed: float) -> None:
    target = (12.5, -7.25)
    assert advance_point_mass(target, target, 10.0, elapsed) == target


def test_no_elapsed_time_means_no_movement() -> None:
    origin = (3.0, 4.0)
    assert advance_point_mass(origin, (100.0, 100.0), 10.0, 0.0) == origin


def test_movement_is_proportional_along_the_straight_line() -> None:
    """A point mass flies the direct line, which is the modelling assumption."""

    reached = advance_point_mass((0.0, 0.0), (30.0, 40.0), 10.0, 2.5)
    # 25 units along a 50-unit diagonal is exactly halfway.
    assert reached == pytest.approx((15.0, 20.0))


def test_a_physically_stopped_aircraft_does_not_move() -> None:
    """`responsive` is physical truth, and a stopped aircraft stays put."""

    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)
    agent.move_toward((100.0, 0.0), 1.0)
    reached = agent.position

    agent.inject_failure(1.0)
    agent.move_toward((100.0, 0.0), 1.0)

    assert agent.position == reached


def test_an_aircraft_wrongly_declared_failed_keeps_flying() -> None:
    """The distinction a SITL backend must preserve.

    A declaration is the swarm's belief. It is not a kill switch, so the
    vehicle keeps flying the task it does not know it lost -- which is the
    whole reason a wrong declaration costs anything.
    """

    agent = Agent(agent_id=1, position=(0.0, 0.0), speed=10.0)
    agent.declare_failed()

    agent.move_toward((100.0, 0.0), 1.0)

    assert agent.responsive is True
    assert agent.position == pytest.approx((10.0, 0.0))
