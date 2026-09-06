"""Evaluator-side wasted-work measurement.

These tests pin two things: the arithmetic of the integral, and the fact that
the measurement is taken from outside the swarm. An agent must never be able to
compute these numbers, because computing them requires reading every replica's
private store at one instant.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from eudis_swarm.config import SimulationConfig
from eudis_swarm.evaluation import (
    EMPTY_SAMPLE,
    OwnershipEvaluator,
    OwnershipSample,
    sample_ownership,
)
from eudis_swarm.metrics import SimulationMetrics
from eudis_swarm.simulation import Simulation
from eudis_swarm.task import Task
from eudis_swarm.task_claims import TaskClaimStore

AGENT_IDS = (1, 2, 3)
TASK_IDS = (1, 2, 3)


def _stores() -> dict[int, TaskClaimStore]:
    return {
        owner: TaskClaimStore(owner, AGENT_IDS, TASK_IDS, lease_duration=10.0)
        for owner in AGENT_IDS
    }


def _tasks() -> dict[int, Task]:
    return {
        task_id: Task(task_id=task_id, position=(float(task_id), 0.0))
        for task_id in TASK_IDS
    }


def _metrics() -> SimulationMetrics:
    return SimulationMetrics(
        total_task_count=len(TASK_IDS), agents_started=len(AGENT_IDS)
    )


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def test_two_replicas_claiming_one_task_is_concurrent_ownership() -> None:
    """Neither UAV can see this; it exists only in the comparison."""

    stores = _stores()
    stores[1].create_claim(1, 0.0)
    stores[2].create_claim(1, 0.0)

    sample = sample_ownership(stores, _tasks(), 0.0)

    assert sample.concurrently_owned_task_ids == (1,)
    assert sample.concurrent_ownership_count == 1
    # a concurrently owned task is not also counted as abandoned
    assert 1 not in sample.abandoned_task_ids


def test_unclaimed_incomplete_work_is_abandoned() -> None:
    stores = _stores()
    stores[1].create_claim(1, 0.0)

    sample = sample_ownership(stores, _tasks(), 0.0)

    assert sample.concurrently_owned_task_ids == ()
    assert sample.abandoned_task_ids == (2, 3)


def test_completed_work_is_not_abandoned() -> None:
    stores = _stores()
    tasks = _tasks()
    tasks[2].assign(1)
    tasks[2].complete(1)

    sample = sample_ownership(stores, tasks, 0.0)

    assert 2 not in sample.abandoned_task_ids
    assert sample.abandoned_task_ids == (1, 3)


def test_dropped_is_the_discriminating_subset_of_abandoned() -> None:
    """Abandoned includes work never started; dropped only work let go of.

    With a deep backlog, abandoned time is dominated by queue depth and says
    little about coordination. Dropped time counts only tasks somebody owned
    and then stopped owning, which is the part a protocol can be blamed for.
    """

    stores = _stores()
    tasks = _tasks()
    stores[1].create_claim(1, 0.0)

    # task 1 was owned; tasks 2 and 3 never were
    started = sample_ownership(stores, tasks, 0.0)
    assert started.abandoned_task_ids == (2, 3)
    assert started.dropped_task_ids == ()

    stores[1].release_claim(1, 1.0)
    after = sample_ownership(stores, tasks, 1.0, previously_owned_task_ids={1})

    assert after.abandoned_task_ids == (1, 2, 3)
    assert after.dropped_task_ids == (1,)


# --------------------------------------------------------------------------
# integration arithmetic
# --------------------------------------------------------------------------


def test_the_integral_holds_each_sample_until_the_next() -> None:
    """Left Riemann sum: state sampled at t counts for [t, t_next)."""

    metrics = _metrics()
    evaluator = OwnershipEvaluator(metrics)
    stores = _stores()
    tasks = _tasks()

    stores[1].create_claim(1, 0.0)
    stores[2].create_claim(1, 0.0)
    evaluator.observe(0.0, stores, tasks)  # 1 concurrent task from here
    evaluator.observe(2.0, stores, tasks)  # ...held for 2.0 s

    assert metrics.concurrent_ownership_duration == pytest.approx(2.0)
    assert metrics.concurrent_ownership_by_task[1] == pytest.approx(2.0)

    # two tasks abandoned over the same interval contributes 2 x 2.0
    assert metrics.abandoned_task_time == pytest.approx(4.0)


def test_the_first_sample_accumulates_nothing() -> None:
    metrics = _metrics()
    evaluator = OwnershipEvaluator(metrics)
    stores = _stores()
    stores[1].create_claim(1, 0.0)
    stores[2].create_claim(1, 0.0)

    evaluator.observe(0.0, stores, _tasks())

    assert metrics.concurrent_ownership_duration == 0.0


def test_finish_closes_the_final_interval() -> None:
    metrics = _metrics()
    evaluator = OwnershipEvaluator(metrics)
    stores = _stores()
    tasks = _tasks()
    stores[1].create_claim(1, 0.0)
    stores[2].create_claim(1, 0.0)

    evaluator.observe(1.0, stores, tasks)
    evaluator.finish(4.0)

    assert metrics.concurrent_ownership_duration == pytest.approx(3.0)
    assert evaluator.last_sample == EMPTY_SAMPLE


def test_repeated_timestamps_add_nothing() -> None:
    metrics = _metrics()
    evaluator = OwnershipEvaluator(metrics)
    stores = _stores()
    tasks = _tasks()
    stores[1].create_claim(1, 0.0)
    stores[2].create_claim(1, 0.0)

    evaluator.observe(0.0, stores, tasks)
    evaluator.observe(0.0, stores, tasks)

    assert metrics.concurrent_ownership_duration == 0.0


def test_time_must_not_move_backwards() -> None:
    evaluator = OwnershipEvaluator(_metrics())
    stores = _stores()
    tasks = _tasks()
    evaluator.observe(2.0, stores, tasks)

    with pytest.raises(ValueError):
        evaluator.observe(1.0, stores, tasks)


def test_an_empty_sample_reports_zero_counts() -> None:
    sample = OwnershipSample((), (), (), ())

    assert sample.concurrent_ownership_count == 0
    assert sample.abandoned_count == 0
    assert sample.dropped_count == 0
    assert sample.contested_count == 0


# --------------------------------------------------------------------------
# end-to-end behaviour
# --------------------------------------------------------------------------


def test_scarce_work_produces_real_concurrent_ownership() -> None:
    """Four UAVs and two tasks force genuine contention.

    With twenty tasks each UAV simply flies to its own nearest objective and
    contention never arises, so this is the configuration that exercises the
    measurement rather than the default one.
    """

    metrics = (
        Simulation(
            replace(
                SimulationConfig(),
                communication_range=50.0,
                task_count=2,
            )
        )
        .run()
        .metrics
    )

    assert metrics.concurrent_ownership_duration > 0.0
    assert metrics.concurrent_ownership_by_task
    assert metrics.completed_task_count == 2


def test_measurement_does_not_perturb_the_simulation() -> None:
    """The evaluator only reads, so results stay reproducible."""

    config = replace(SimulationConfig(), communication_range=50.0, task_count=3)
    first = Simulation(config).run().metrics
    second = Simulation(config).run().metrics

    assert first.simulation_duration == second.simulation_duration
    assert first.concurrent_ownership_duration == second.concurrent_ownership_duration
    assert first.abandoned_task_time == second.abandoned_task_time
    assert first.dropped_ownership_time == second.dropped_ownership_time
    assert first.contested_duration == second.contested_duration


def test_summary_labels_the_numbers_as_evaluator_computed() -> None:
    """Nobody should read these as something the swarm knows."""

    summary = (
        Simulation(replace(SimulationConfig(), communication_range=50.0, task_count=3))
        .run()
        .metrics.format_summary()
    )

    assert "evaluator-computed ground truth" in summary
    assert "no UAV can observe these" in summary
    assert "Concurrent ownership:" in summary
    assert "Abandoned task time:" in summary
    assert "Contested duration:" in summary
