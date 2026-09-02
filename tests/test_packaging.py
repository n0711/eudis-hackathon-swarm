"""Guard the public-facing packaging surface of the distribution."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from eudis_swarm import __version__
from eudis_swarm.simulation import _parser

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict[str, object]:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_package_version_matches_pyproject() -> None:
    """A drifting version silently mislabels every recorded trace and wheel."""

    project = _pyproject()["project"]
    assert isinstance(project, dict)
    assert project["version"] == __version__


def test_version_flag_reports_the_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        _parser().parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"eudis-swarm {__version__}"


@pytest.mark.parametrize(
    "filename",
    [
        "README.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    ],
)
def test_required_repository_documents_exist(filename: str) -> None:
    """These carry the working agreement and the reporting route for the team."""

    document = REPOSITORY_ROOT / filename
    assert document.is_file(), f"{filename} is missing"
    assert document.stat().st_size > 0, f"{filename} is empty"
