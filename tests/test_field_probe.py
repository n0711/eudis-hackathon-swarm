"""The link probe: loopback measurement, CSV round trip, RadioModel restatement."""

from __future__ import annotations

import math
import socket
import threading
from pathlib import Path

import pytest

from eudis_swarm.field_probe import (
    ProbeRow,
    format_summary,
    main,
    make_packet,
    parse_packet,
    read_csv,
    run_echo,
    run_sender,
    summarize,
    write_csv,
)


def _udp() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    return sock


def _wait(sock: socket.socket):
    import select

    def wait(timeout: float) -> None:
        if timeout > 0.0:
            select.select([sock], [], [], timeout)

    return wait


def test_packets_round_trip_and_reject_foreign_bytes() -> None:
    packet = make_packet(7, 99, 1.5, 64)
    assert len(packet) == 64
    assert parse_packet(packet) == (7, 99, 1.5)
    assert parse_packet(b"short") is None
    assert parse_packet(b"XXXX" + packet[4:]) is None
    with pytest.raises(ValueError):
        make_packet(1, 1, 0.0, 4)


def test_a_loopback_probe_measures_full_delivery(tmp_path: Path) -> None:
    echo = _udp()
    sender = _udp()
    thread = threading.Thread(
        target=run_echo, args=(echo,), kwargs={"duration": 1.5, "wait": _wait(echo)}
    )
    thread.start()
    try:
        rows = run_sender(
            sender,
            echo.getsockname(),
            count=40,
            rate=100.0,
            size=200,
            linger=0.5,
            wait=_wait(sender),
        )
    finally:
        thread.join()
        echo.close()
        sender.close()
    assert len(rows) == 40
    assert all(row.rtt_ms is not None and row.rtt_ms >= 0.0 for row in rows)
    path = tmp_path / "probe.csv"
    write_csv(rows, path)
    restored = read_csv(path)
    assert [row.seq for row in restored] == list(range(40))
    summary = summarize(restored)
    assert summary["round_trip_delivery_ratio"] == 1.0
    assert summary["radio_model"]["equivalent_ber"] == 0.0
    assert summary["loss_bursts"] == 0


def test_summary_restates_loss_in_radio_model_terms() -> None:
    rows = [
        ProbeRow(seq, float(seq) * 0.1, None if seq in (3, 4, 9) else seq * 0.1 + 0.004)
        for seq in range(20)
    ]
    summary = summarize(
        rows, frame_bits=1024, heartbeat_interval=0.1, heartbeat_timeout=0.3
    )
    assert summary["round_trip_delivery_ratio"] == 0.85
    one_way = math.sqrt(0.85)
    assert summary["one_way_delivery_ratio_est"] == round(one_way, 6)
    assert summary["radio_model"]["equivalent_ber"] == pytest.approx(
        1.0 - one_way ** (1 / 1024)
    )
    assert (summary["loss_bursts"], summary["loss_burst_max"]) == (2, 2)
    assert summary["rtt_ms_p50"] == pytest.approx(4.0, abs=0.01)
    assert summary["protocol"]["heartbeats_per_timeout"] == 3
    assert "RadioModel" in format_summary(summary)
    lost = summarize([ProbeRow(0, 0.0, None), ProbeRow(1, 0.1, None)])
    assert lost["radio_model"]["equivalent_ber"] is None
    assert lost["rtt_ms_p50"] is None
    with pytest.raises(ValueError):
        summarize([])
    with pytest.raises(ValueError):
        run_sender(_udp(), ("127.0.0.1", 9), count=0, rate=1.0, size=64)


def test_command_line_summarize(tmp_path: Path, capsys) -> None:
    path = tmp_path / "probe.csv"
    write_csv([ProbeRow(0, 0.0, 0.002), ProbeRow(1, 0.05, None)], path)
    output = tmp_path / "summary.json"
    assert main(["summarize", str(path), "--json", str(output)]) == 0
    assert "round-trip delivery ratio     0.5" in capsys.readouterr().out
    assert output.exists()
