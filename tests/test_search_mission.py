"""Coordinated sector search: geometry, coverage measurement, pinned outcomes."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from eudis_swarm.communication import RegionJammer
from eudis_swarm.search_mission import (
    NO_FAILURE_TIME,
    main,
    run_search,
    search_config,
    sector_tasks,
    study_rows,
    summarise,
)

CENTRE_JAMMER = RegionJammer(50.0, 50.0, 25.0, 3.0, 15.0)


def test_sectors_sweep_the_area_in_lawnmower_order() -> None:
    tasks = sector_tasks(100.0, 100.0, 20.0)

    assert [task.task_id for task in tasks] == list(range(1, 26))
    assert [task.position for task in tasks[:5]] == [
        (10.0, 10.0),
        (30.0, 10.0),
        (50.0, 10.0),
        (70.0, 10.0),
        (90.0, 10.0),
    ]
    # The second row runs back the other way.
    assert [task.position for task in tasks[5:10]] == [
        (90.0, 30.0),
        (70.0, 30.0),
        (50.0, 30.0),
        (30.0, 30.0),
        (10.0, 30.0),
    ]


def test_edge_sectors_are_clipped_to_the_area() -> None:
    tasks = sector_tasks(50.0, 30.0, 20.0)

    assert [task.position for task in tasks] == [
        (10.0, 10.0),
        (30.0, 10.0),
        (45.0, 10.0),
        (45.0, 25.0),
        (30.0, 25.0),
        (10.0, 25.0),
    ]


@pytest.mark.parametrize(
    ("width", "height", "size"),
    [(0.0, 10.0, 5.0), (10.0, -1.0, 5.0), (10.0, 10.0, 0.0), (float("nan"), 1.0, 1.0)],
)
def test_degenerate_geometry_is_refused(
    width: float, height: float, size: float
) -> None:
    with pytest.raises(ValueError):
        sector_tasks(width, height, size)


def test_sector_count_must_match_the_configuration() -> None:
    config = search_config(sector_count=24)
    with pytest.raises(ValueError, match="task_count"):
        run_search(config, 20.0)


def test_a_lost_uav_is_covered_by_lease_expiry_without_redundant_search() -> None:
    """Pinned: 25 sectors, 4 UAVs, UAV 2 physically lost at 8 s, links clean."""

    distributed = run_search(
        search_config(sector_count=25, failure_agent=2, failure_time=8.0), 20.0
    )
    centralized = run_search(
        search_config(
            sector_count=25,
            failure_agent=2,
            failure_time=8.0,
            task_control="centralized",
        ),
        20.0,
    )

    assert distributed.searched_count == 25
    assert distributed.redundant_searches == 0
    assert distributed.time_to_coverage(0.5) == 6.75
    assert distributed.time_to_coverage(0.9) == 12.75
    assert distributed.time_to_coverage(1.0) == 17.25
    # With clean links the coordinator reassigns as soon as it declares the
    # loss, so it finishes sooner: the distributed mode waits for leases.
    assert centralized.time_to_coverage(1.0) == 15.75
    assert centralized.redundant_searches == 0


def test_under_jamming_only_the_distributed_search_finishes() -> None:
    """UAV 1 lost at 12 s while a central jammer partitions the swarm."""

    distributed = run_search(
        search_config(
            sector_count=25, failure_agent=1, failure_time=12.0, jammer=CENTRE_JAMMER
        ),
        20.0,
    )
    centralized = run_search(
        search_config(
            sector_count=25,
            failure_agent=1,
            failure_time=12.0,
            jammer=CENTRE_JAMMER,
            task_control="centralized",
        ),
        20.0,
    )

    assert distributed.mission_completed is True
    assert distributed.coverage == 1.0
    assert centralized.mission_completed is False
    assert centralized.searched_count == 24  # one sector is never searched
    assert centralized.time_to_coverage(1.0) is None
    # Availability is paid for in duplicate effort across the partition.
    assert distributed.redundant_searches > centralized.redundant_searches


def test_coverage_curve_is_monotonic_and_ends_at_full_coverage() -> None:
    report = run_search(search_config(sector_count=25), 20.0)

    curve = report.curve(1.0)
    fractions = [fraction for _, fraction in curve]
    assert fractions == sorted(fractions)
    assert curve[0] == (0.0, 0.0)
    assert fractions[-1] == 1.0
    with pytest.raises(ValueError):
        report.curve(0.0)
    with pytest.raises(ValueError):
        report.time_to_coverage(0.0)


def test_study_pairs_every_case_under_both_modes() -> None:
    rows = study_rows(
        sector_size=20.0,
        agents=4,
        area=40.0,
        failure_times=(2.0,),
        jammer=RegionJammer(20.0, 20.0, 10.0, 1.0, 3.0),
    )

    assert len(rows) == 2 * 4 * 1 * 2
    for jammed in (False, True):
        for agent in range(1, 5):
            modes = {
                row["task_control"]
                for row in rows
                if row["jammer"] is jammed and row["failure_agent"] == agent
            }
            assert modes == {"distributed", "centralized"}
    lines = summarise(rows)
    assert len(lines) == 4
    assert all("full coverage" in line for line in lines)


def test_cli_single_run_and_study(tmp_path: Path, capsys) -> None:
    assert main(["--area", "40", "--failure-time", "-1"]) == 0
    single = capsys.readouterr().out
    assert "4 sectors" in single
    assert "never" in single
    assert "distributed" in single and "centralized" in single

    output = tmp_path / "study.csv"
    assert main(["--area", "40", "--study", "--csv", str(output)]) == 0
    with output.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2 * 4 * 4 * 2
    assert "jammer=on" in capsys.readouterr().out


def test_cli_rejects_a_malformed_jammer() -> None:
    with pytest.raises(SystemExit):
        main(["--jammer", "1,2,3"])


def test_no_failure_time_disables_the_fault() -> None:
    config = search_config(sector_count=25)
    assert config.failure_time == NO_FAILURE_TIME
