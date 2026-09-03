"""Exercise the physical free-space radio link model and its integration.

Covers the ``RadioModel`` physics (path loss, SNR, BER, ``can_link`` and
``link_quality``), its wiring into ``CommunicationGraph`` behind a config flag,
and the seeded determinism of stochastic link sampling.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from eudis_swarm.communication import CommunicationGraph, RadioModel
from eudis_swarm.config import SimulationConfig
from eudis_swarm.simulation import Simulation, _parser, main

# a deliberately weak model: at the default 100-unit arena the corner UAVs sit
# roughly 90-130 units apart, where this model's link quality is interior, so a
# stochastic draw genuinely flips links up and down.
WEAK_RADIO = RadioModel(noise_dbm=-62.0, frame_bits=16)


def test_default_parameters_match_the_paper_defaults() -> None:
    model = RadioModel()
    assert model.frequency_hz == 2.4e9
    assert model.eirp_dbm == 20.0
    assert model.rx_gain_db == 3.0
    assert model.xi_los_db == 3.0
    assert model.noise_dbm == -100.0
    assert model.ber_threshold == 1e-5
    assert model.frame_bits == 1024


def test_decibel_fields_convert_to_the_expected_linear_domain() -> None:
    model = RadioModel()
    # 20 dBm -> 100 mW, -100 dBm -> 1e-10 mW, 3 dB -> ~1.995x.
    assert model._eirp_mw == pytest.approx(100.0)
    assert model._noise_mw == pytest.approx(1e-10)
    assert model._rx_gain_linear == pytest.approx(10.0**0.3)
    assert model._xi_los_linear == pytest.approx(10.0**0.3)


def test_path_loss_matches_the_closed_form_and_grows_with_distance() -> None:
    model = RadioModel()
    distance = 750.0
    free_space = (
        4.0 * math.pi * distance * model.frequency_hz / model.speed_of_light_m_s
    ) ** 2
    assert model.path_loss(distance) == pytest.approx(free_space * model._xi_los_linear)
    assert model.path_loss(0.0) == 0.0
    assert model.path_loss(10.0) < model.path_loss(100.0) < model.path_loss(1000.0)


def test_snr_and_ber_are_monotonic_in_distance() -> None:
    model = RadioModel()
    assert model.snr(0.0) == math.inf
    assert model.bit_error_rate(0.0) == 0.0
    distances = [1.0, 50.0, 500.0, 2_000.0, 5_000.0, 20_000.0]
    snrs = [model.snr(d) for d in distances]
    bers = [model.bit_error_rate(d) for d in distances]
    assert snrs == sorted(snrs, reverse=True)
    assert bers == sorted(bers)
    assert all(0.0 <= ber <= 0.5 for ber in bers)


def test_can_link_calibration_boundary_for_default_parameters() -> None:
    """The paper's hard rule puts the default link horizon near 3.3 km."""

    model = RadioModel()
    assert model.can_link(0.0) is True
    assert model.can_link(3_000.0) is True
    assert model.can_link(3_300.0) is False
    assert model.can_link(10_000.0) is False


def test_link_quality_is_a_probability_that_decays_with_distance() -> None:
    model = RadioModel()
    assert model.link_quality(0.0) == 1.0
    qualities = [model.link_quality(d) for d in (10.0, 1_000.0, 4_000.0, 6_000.0, 1e5)]
    assert all(0.0 <= quality <= 1.0 for quality in qualities)
    assert qualities == sorted(qualities, reverse=True)
    assert qualities[0] == pytest.approx(1.0)
    assert qualities[-1] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "changes",
    [
        {"frequency_hz": 0.0},
        {"frequency_hz": -1.0},
        {"frequency_hz": math.nan},
        {"speed_of_light_m_s": 0.0},
        {"eirp_dbm": math.inf},
        {"noise_dbm": math.nan},
        {"frame_bits": 0},
        {"frame_bits": -8},
        {"frame_bits": 4.0},
        {"frame_bits": True},
        {"ber_threshold": 0.0},
        {"ber_threshold": 1.0},
        {"ber_threshold": -0.1},
        {"ber_threshold": 5.0},
    ],
)
def test_radio_model_rejects_invalid_parameters(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RadioModel(**changes)  # type: ignore[arg-type]


def test_radio_model_rejects_invalid_distances() -> None:
    model = RadioModel()
    with pytest.raises(ValueError):
        model.path_loss(-1.0)
    with pytest.raises(ValueError):
        model.bit_error_rate(math.nan)
    with pytest.raises(ValueError):
        model.link_quality(math.inf)


def test_range_model_is_the_untouched_default() -> None:
    graph = CommunicationGraph(agent_ids=[1, 2], communication_range=5.0)
    assert graph.radio_model is None
    assert graph.stochastic_links is False

    graph.update({1: (0.0, 0.0), 2: (5.0, 0.0)})
    assert graph.link_between(1, 2).available is True

    far = CommunicationGraph(agent_ids=[1, 2], communication_range=3.0)
    far.update({1: (0.0, 0.0), 2: (5.0, 0.0)})
    assert far.link_between(1, 2).available is False


def test_radio_model_bypasses_communication_range() -> None:
    # a tiny range that would sever every link under the binary model.
    graph = CommunicationGraph(
        agent_ids=[1, 2, 3],
        communication_range=1.0,
        radio_model=RadioModel(),
    )
    graph.update({1: (0.0, 0.0), 2: (100.0, 0.0), 3: (6_000.0, 0.0)})

    assert graph.radio_model is not None
    # 100 units is far inside the ~3.3 km horizon; 6 km is well past it.
    assert graph.link_between(1, 2).available is True
    assert graph.link_between(1, 3).available is False
    assert graph.link_between(2, 3).available is False


def test_stochastic_links_require_a_radio_model() -> None:
    with pytest.raises(ValueError):
        CommunicationGraph(
            agent_ids=[1, 2],
            communication_range=10.0,
            stochastic_links=True,
        )


def _link_history(seed: int, distance: float, steps: int) -> list[bool]:
    graph = CommunicationGraph(
        agent_ids=[1, 2],
        communication_range=1.0,
        radio_model=WEAK_RADIO,
        stochastic_links=True,
        link_seed=seed,
    )
    positions = {1: (0.0, 0.0), 2: (distance, 0.0)}
    return [graph.update(positions).is_fully_connected for _ in range(steps)]


def test_stochastic_links_are_reproducible_and_seed_sensitive() -> None:
    # pick a separation where the weak model's quality is strictly interior.
    distance = next(
        float(d) for d in range(1, 400) if 0.2 < WEAK_RADIO.link_quality(float(d)) < 0.8
    )

    first = _link_history(seed=2026, distance=distance, steps=60)
    repeat = _link_history(seed=2026, distance=distance, steps=60)
    other = _link_history(seed=99, distance=distance, steps=60)

    # the seeded stream is fully reproducible...
    assert first == repeat
    # ...it actually flips the link up and down...
    assert len(set(first)) == 2
    # ...and a different seed diverges.
    assert first != other


def test_config_validates_link_model_fields() -> None:
    assert SimulationConfig().link_model == "range"
    assert SimulationConfig().stochastic_delivery is False
    assert isinstance(SimulationConfig().radio_model, RadioModel)

    SimulationConfig(link_model="radio")
    SimulationConfig(link_model="radio", stochastic_delivery=True)

    with pytest.raises(ValueError):
        SimulationConfig(link_model="mesh")
    with pytest.raises(ValueError):
        SimulationConfig(stochastic_delivery=True)  # requires link_model='radio'
    with pytest.raises(ValueError):
        SimulationConfig(radio_model="loud")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SimulationConfig(stochastic_delivery="yes")  # type: ignore[arg-type]


def test_cli_exposes_link_model_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = _parser().parse_args([])
    assert defaults.link_model == "range"
    assert defaults.stochastic_delivery is False

    parsed = _parser().parse_args(["--link-model", "radio", "--stochastic-delivery"])
    assert parsed.link_model == "radio"
    assert parsed.stochastic_delivery is True

    captured: dict[str, SimulationConfig] = {}

    def fake_run(config: SimulationConfig) -> object:
        captured["config"] = config
        return SimpleNamespace(metrics=SimpleNamespace(mission_completed=True))

    monkeypatch.setattr("eudis_swarm.simulation.configure_logging", lambda _level: None)
    monkeypatch.setattr("eudis_swarm.simulation.run_simulation", fake_run)

    exit_code = main(["--link-model", "radio", "--stochastic-delivery"])
    assert exit_code == 0
    assert captured["config"].link_model == "radio"
    assert captured["config"].stochastic_delivery is True


def test_default_scenario_is_unchanged_by_the_range_refactor() -> None:
    result = Simulation(SimulationConfig()).run()
    assert result.metrics.mission_completed is True
    assert result.metrics.completed_task_count == 20
    assert result.metrics.simulation_duration == 17.25


def test_radio_link_model_runs_the_default_scenario_fully_connected() -> None:
    """Default RF parameters keep the 100-unit arena inside one link horizon."""

    result = Simulation(SimulationConfig(link_model="radio")).run()
    assert result.metrics.mission_completed is True
    assert result.metrics.completed_task_count == 20
    assert result.metrics.network_ended_connected is True
    assert result.metrics.isolation_event_count == 0


def _weak_stochastic_config(seed: int) -> SimulationConfig:
    return SimulationConfig(
        task_count=6,
        random_seed=seed,
        link_model="radio",
        stochastic_delivery=True,
        radio_model=WEAK_RADIO,
        failure_time=100.0,
        max_simulation_time=15.0,
    )


def test_stochastic_delivery_trace_is_byte_identical_for_one_seed(tmp_path) -> None:
    config = _weak_stochastic_config(seed=2026)

    first = Simulation(config, capture_trace=True).run()
    second = Simulation(config, capture_trace=True).run()
    assert first.trace is not None and second.trace is not None
    assert first.trace.to_dict() == second.trace.to_dict()

    first_path = tmp_path / "first.trace.json"
    second_path = tmp_path / "second.trace.json"
    first.trace.write_json(first_path)
    second.trace.write_json(second_path)
    assert first_path.read_bytes() == second_path.read_bytes()

    # the seed is load-bearing: a different one produces a different trace.
    other = Simulation(_weak_stochastic_config(seed=99), capture_trace=True).run()
    assert other.trace is not None
    assert other.trace.to_dict() != first.trace.to_dict()
