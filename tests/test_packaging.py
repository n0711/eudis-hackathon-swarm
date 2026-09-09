"""Guard the public-facing packaging surface of the distribution."""

from __future__ import annotations

import subprocess
import sys
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


def test_results_are_written_to_stdout_not_stderr() -> None:
    """A result that only exists on stderr cannot be redirected or piped.

    The reproducibility claim in the README is a claim about a command line, so
    `eudis-swarm --scenario ... > run.txt` has to contain the run. It did not:
    `logging.basicConfig` defaults to stderr, which left stdout empty and turned
    every result line into an error record under Windows PowerShell 5.1.

    Run out of process, because the contract being asserted is about the real
    streams a shell sees, not about handlers inside this interpreter.
    """

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "eudis_swarm.simulation",
            "--scenario",
            "demo-quorum-floor",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Mission completed:" in result.stdout
    assert result.stderr == "", f"results leaked to stderr: {result.stderr!r}"


def test_usage_errors_stay_on_stderr() -> None:
    """The other half of the contract: diagnostics must not pollute results."""

    result = subprocess.run(
        [sys.executable, "-m", "eudis_swarm.simulation", "--blocked-link", "1:1"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "blocked_links" in result.stderr
