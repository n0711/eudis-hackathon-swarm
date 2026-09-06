"""The software-in-the-loop backend, tested without an autopilot attached.

CI has no vehicle, and it should not need one. What can be checked here is
everything that guards the rules in docs/sitl.md: that the module imports with
or without the optional extra, that a SITL run declares itself
non-deterministic, and that using it wrongly fails with something a person can
act on rather than an AttributeError three frames deep.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from eudis_swarm.sitl import _MISSING_DEPENDENCY, SitlVehicle


def test_a_sitl_run_declares_itself_non_deterministic() -> None:
    """The rule that keeps real firmware out of published numbers.

    Readable off the class, so a caller producing measured results can refuse a
    backend without first connecting to one.
    """

    assert SitlVehicle.deterministic is False


def test_the_missing_extra_names_the_command_that_fixes_it() -> None:
    assert "pymavlink" in _MISSING_DEPENDENCY
    assert '".[sitl]"' in _MISSING_DEPENDENCY


def test_the_simulator_never_reaches_the_sitl_backend() -> None:
    """The optional extra must stay optional.

    If anything in the simulator's import graph reached this module, a missing
    pymavlink would break the whole package rather than one backend. Checked in
    a fresh interpreter because this test module has itself imported it.
    """

    probe = (
        "import sys; import eudis_swarm.simulation, eudis_swarm.sweep;"
        "leaked = [m for m in ('eudis_swarm.sitl', 'pymavlink') if m in sys.modules];"
        "print(','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"the simulator pulled in {result.stdout.strip()}"
    )


@pytest.mark.parametrize("connection_string", ["", None, 5])
def test_an_unusable_connection_string_is_refused(connection_string) -> None:
    with pytest.raises(ValueError, match="connection_string"):
        SitlVehicle(connection_string)  # type: ignore[arg-type]


@pytest.mark.parametrize("rate", [0, -1])
def test_a_nonpositive_stream_rate_is_refused(rate: int) -> None:
    with pytest.raises(ValueError, match="stream_rate_hz"):
        SitlVehicle("tcp:127.0.0.1:5760", stream_rate_hz=rate)


@pytest.mark.parametrize("timeout", [0.0, -3.0])
def test_a_nonpositive_timeout_is_refused(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        SitlVehicle("tcp:127.0.0.1:5760", timeout=timeout)


def test_a_fresh_vehicle_is_not_connected() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    assert vehicle.connected is False
    assert vehicle.frame is None
    assert vehicle.position == (0.0, 0.0)


def test_the_vehicle_reports_its_own_clock_not_this_machines() -> None:
    """Timing against SITL must use the autopilot's clock.

    SITL is routinely run faster than real time. Under --speedup 10 a
    wall-clock stopwatch reports a 60 m leg taking one second, which is how the
    first version of the model-error experiment produced nonsense.
    """

    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    assert vehicle.vehicle_time_s == 0.0


def test_commanding_before_connecting_says_so() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    with pytest.raises(RuntimeError, match="not connected"):
        vehicle.command_toward((10.0, 10.0))
    with pytest.raises(RuntimeError, match="not connected"):
        vehicle.observe()
    with pytest.raises(RuntimeError, match="not connected"):
        vehicle.wait_until_within((1.0, 1.0), tolerance_m=1.0)


def test_a_nonpositive_takeoff_altitude_is_refused() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    with pytest.raises(ValueError, match="altitude_m"):
        vehicle.arm_and_takeoff(0.0)


def test_a_nonpositive_tolerance_is_refused() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    with pytest.raises(ValueError, match="tolerance_m"):
        vehicle.wait_until_within((1.0, 1.0), tolerance_m=0.0)


def test_closing_an_unconnected_vehicle_is_harmless() -> None:
    SitlVehicle("tcp:127.0.0.1:5760").close()


def test_distance_is_measured_in_local_metres() -> None:
    vehicle = SitlVehicle("tcp:127.0.0.1:5760")
    assert vehicle.distance_to((3.0, 4.0)) == pytest.approx(5.0)
