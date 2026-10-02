"""Measure a real link: packet delivery ratio and latency between two hosts.

Run an echo on one host and a sender on the other::

    # host B
    python -m eudis_swarm.field_probe echo --bind 0.0.0.0:47100 --duration 120
    # host A
    python -m eudis_swarm.field_probe send --target 192.168.1.12:47100 \\
        --count 2000 --rate 20 --size 300 --output probe-A-B.csv
    python -m eudis_swarm.field_probe summarize probe-A-B.csv

The sender emits sequence-numbered UDP datagrams at a fixed rate; the echo
reflects each one verbatim. Every row of the CSV is one sequence number with
the sender's own monotonic send time, the echo's arrival time (blank when
lost) and the round-trip time. Only the sender's clock is used, so no clock
synchronisation between hosts is needed. One-way figures are *estimates*
(``RTT/2``; ``sqrt`` of the round-trip delivery ratio) that assume a
symmetric link with independent loss -- the summary states the assumption next
to each number it affects.

:func:`summarize` restates a measurement in the simulator's terms:
``RadioModel.link_quality`` is a per-frame delivery probability, so the
comparable figure is the estimated one-way delivery ratio, and the bit error
rate that would produce it for ``frame_bits`` bits is
``1 - p ** (1 / frame_bits)``. The simulator delivers within one tick (zero
latency), so the measured latency is reported against the heartbeat interval
and the protocol timeouts it eats into. Nothing here claims more than the CSV
contains.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import select
import socket
import struct
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .field_transport import Address, DatagramSocket, parse_address

MAGIC = b"EPRB"
_HEADER = struct.Struct(">4sIQd")
MIN_SIZE = _HEADER.size
MAX_SIZE = 1400


@dataclass(frozen=True, slots=True)
class ProbeRow:
    seq: int
    sent_s: float
    echo_s: float | None

    @property
    def rtt_ms(self) -> float | None:
        return None if self.echo_s is None else (self.echo_s - self.sent_s) * 1000.0


def make_packet(seq: int, session: int, sent_s: float, size: int) -> bytes:
    if not MIN_SIZE <= size <= MAX_SIZE:
        raise ValueError(f"size must be in [{MIN_SIZE}, {MAX_SIZE}]")
    header = _HEADER.pack(MAGIC, seq, session, sent_s)
    return header + bytes(size - len(header))


def parse_packet(data: bytes) -> tuple[int, int, float] | None:
    if len(data) < MIN_SIZE:
        return None
    magic, seq, session, sent_s = _HEADER.unpack_from(data)
    if magic != MAGIC:
        return None
    return seq, session, sent_s


def run_echo(
    sock: DatagramSocket,
    *,
    duration: float,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] | None = None,
) -> int:
    """Reflect probe datagrams until ``duration`` elapses; return echoes sent."""

    deadline = clock() + duration
    echoed = 0
    while clock() < deadline:
        if wait is not None:
            wait(min(0.05, max(0.0, deadline - clock())))
        while True:
            try:
                data, address = sock.recvfrom(MAX_SIZE + 64)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if parse_packet(data) is None:
                continue
            try:
                sock.sendto(data, address)
                echoed += 1
            except OSError:
                continue
    return echoed


def run_sender(
    sock: DatagramSocket,
    target: Address,
    *,
    count: int,
    rate: float,
    size: int,
    linger: float = 1.0,
    session: int = 1,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] | None = None,
) -> list[ProbeRow]:
    """Send ``count`` probes at ``rate`` Hz and record what came back."""

    if count < 1 or rate <= 0.0:
        raise ValueError("count must be >= 1 and rate > 0")
    sent: dict[int, float] = {}
    echoed: dict[int, float] = {}
    interval = 1.0 / rate
    start = clock()

    def drain() -> None:
        while True:
            try:
                data, _ = sock.recvfrom(MAX_SIZE + 64)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            arrived = clock()
            parsed = parse_packet(data)
            if parsed is None or parsed[1] != session:
                continue
            seq = parsed[0]
            if seq in sent and seq not in echoed:
                echoed[seq] = arrived

    for seq in range(count):
        due = start + seq * interval
        while True:
            remaining = due - clock()
            if remaining <= 0.0:
                break
            if wait is not None:
                wait(remaining)
            drain()
        now = clock()
        sent[seq] = now
        try:
            sock.sendto(make_packet(seq, session, now, size), target)
        except OSError:
            pass
        drain()
    linger_until = clock() + linger
    while clock() < linger_until and len(echoed) < len(sent):
        if wait is not None:
            wait(min(0.05, max(0.0, linger_until - clock())))
        drain()
    drain()
    return [ProbeRow(seq, sent[seq], echoed.get(seq)) for seq in sorted(sent)]


def write_csv(rows: Sequence[ProbeRow], path: str | Path) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["seq", "sent_s", "echo_s", "rtt_ms"])
        for row in rows:
            writer.writerow(
                [
                    row.seq,
                    f"{row.sent_s:.6f}",
                    "" if row.echo_s is None else f"{row.echo_s:.6f}",
                    "" if row.rtt_ms is None else f"{row.rtt_ms:.3f}",
                ]
            )


def read_csv(path: str | Path) -> list[ProbeRow]:
    rows: list[ProbeRow] = []
    with Path(path).open(newline="", encoding="utf-8") as stream:
        for item in csv.DictReader(stream):
            echo_text = (item.get("echo_s") or "").strip()
            rows.append(
                ProbeRow(
                    seq=int(item["seq"]),
                    sent_s=float(item["sent_s"]),
                    echo_s=float(echo_text) if echo_text else None,
                )
            )
    return rows


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def summarize(
    rows: Sequence[ProbeRow],
    *,
    frame_bits: int = 1024,
    heartbeat_interval: float = 0.1,
    heartbeat_timeout: float = 0.6,
) -> dict[str, Any]:
    """Restate a probe measurement in terms comparable with ``RadioModel``."""

    if not rows:
        raise ValueError("no probe rows to summarise")
    sent = len(rows)
    rtts = [row.rtt_ms for row in rows if row.rtt_ms is not None]
    received = len(rtts)
    round_trip = received / sent
    one_way = math.sqrt(round_trip)
    bursts: list[int] = []
    run = 0
    for row in sorted(rows, key=lambda item: item.seq):
        if row.echo_s is None:
            run += 1
        elif run:
            bursts.append(run)
            run = 0
    if run:
        bursts.append(run)
    duration = max(row.sent_s for row in rows) - min(row.sent_s for row in rows)
    mean_rtt = sum(rtts) / received if received else None
    jitter = (
        math.sqrt(sum((value - mean_rtt) ** 2 for value in rtts) / received)
        if received and mean_rtt is not None
        else None
    )
    equivalent_ber: float | None
    if one_way >= 1.0:
        equivalent_ber = 0.0
    elif one_way <= 0.0:
        equivalent_ber = None
    else:
        equivalent_ber = 1.0 - one_way ** (1.0 / frame_bits)
    beats_per_timeout = max(
        1, math.floor(heartbeat_timeout / heartbeat_interval + 1e-9)
    )
    false_silence = (1.0 - one_way) ** beats_per_timeout
    return {
        "probes_sent": sent,
        "echoes_received": received,
        "measured_s": round(duration, 3),
        "round_trip_delivery_ratio": round(round_trip, 6),
        "one_way_delivery_ratio_est": round(one_way, 6),
        "one_way_estimate_assumes": "symmetric link, independent loss",
        "loss_bursts": len(bursts),
        "loss_burst_mean": round(sum(bursts) / len(bursts), 3) if bursts else 0.0,
        "loss_burst_max": max(bursts) if bursts else 0,
        "rtt_ms_p50": _percentile(rtts, 0.50),
        "rtt_ms_p95": _percentile(rtts, 0.95),
        "rtt_ms_p99": _percentile(rtts, 0.99),
        "rtt_ms_max": round(max(rtts), 3) if rtts else None,
        "rtt_ms_mean": None if mean_rtt is None else round(mean_rtt, 3),
        "rtt_ms_jitter_sd": None if jitter is None else round(jitter, 3),
        "one_way_latency_ms_est": None if mean_rtt is None else round(mean_rtt / 2, 3),
        "radio_model": {
            "link_quality_equivalent": round(one_way, 6),
            "frame_bits": frame_bits,
            "equivalent_ber": equivalent_ber,
            "simulator_latency_ms": 0.0,
        },
        "protocol": {
            "heartbeat_interval_s": heartbeat_interval,
            "heartbeat_timeout_s": heartbeat_timeout,
            "heartbeats_per_timeout": beats_per_timeout,
            "p_false_silence_per_timeout_independent": false_silence,
        },
    }


def format_summary(summary: dict[str, Any]) -> str:
    radio = summary["radio_model"]
    protocol = summary["protocol"]
    return "\n".join(
        [
            f"probes sent / echoed          {summary['probes_sent']} / "
            f"{summary['echoes_received']} over {summary['measured_s']} s",
            f"round-trip delivery ratio     {summary['round_trip_delivery_ratio']}",
            f"one-way delivery (estimate)   {summary['one_way_delivery_ratio_est']}"
            f"  [{summary['one_way_estimate_assumes']}]",
            f"loss bursts (mean / max)      {summary['loss_bursts']} "
            f"({summary['loss_burst_mean']} / {summary['loss_burst_max']})",
            f"RTT ms p50 / p95 / p99 / max  {summary['rtt_ms_p50']} / "
            f"{summary['rtt_ms_p95']} / {summary['rtt_ms_p99']} / "
            f"{summary['rtt_ms_max']}",
            f"one-way latency ms (RTT/2)    {summary['one_way_latency_ms_est']}",
            f"RadioModel link_quality equiv {radio['link_quality_equivalent']} "
            f"(BER {radio['equivalent_ber']} at {radio['frame_bits']} bits/frame; "
            "simulator latency 0 ms)",
            f"P(false silence) per timeout  "
            f"{protocol['p_false_silence_per_timeout_independent']:.3g} "
            f"({protocol['heartbeats_per_timeout']} heartbeats per "
            f"{protocol['heartbeat_timeout_s']} s, independent loss)",
        ]
    )


def _select_wait(sock: socket.socket) -> Callable[[float], None]:
    def wait(timeout: float) -> None:
        if timeout > 0.0:
            select.select([sock], [], [], timeout)

    return wait


def _open(bind: Address) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(bind)
    sock.setblocking(False)
    return sock


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eudis_swarm.field_probe",
        description="Measure UDP delivery ratio and latency between two hosts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    echo = commands.add_parser("echo", help="reflect probes back to the sender")
    echo.add_argument("--bind", default="0.0.0.0:47100")
    echo.add_argument("--duration", type=float, default=300.0)
    send = commands.add_parser("send", help="send probes and record echoes")
    send.add_argument("--target", required=True, help="HOST:PORT of the echo")
    send.add_argument("--bind", default="0.0.0.0:0")
    send.add_argument("--count", type=int, default=1000)
    send.add_argument("--rate", type=float, default=20.0, help="probes per second")
    send.add_argument("--size", type=int, default=300, help="datagram bytes")
    send.add_argument("--linger", type=float, default=2.0)
    send.add_argument("--output", required=True, help="CSV path")
    summary = commands.add_parser("summarize", help="summarise a probe CSV")
    summary.add_argument("csv")
    summary.add_argument("--frame-bits", type=int, default=1024)
    summary.add_argument("--heartbeat-interval", type=float, default=0.1)
    summary.add_argument("--heartbeat-timeout", type=float, default=0.6)
    summary.add_argument("--json", help="also write the summary as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "echo":
        sock = _open(parse_address(args.bind))
        try:
            echoed = run_echo(sock, duration=args.duration, wait=_select_wait(sock))
        finally:
            sock.close()
        print(f"echoed {echoed} probes", file=sys.stderr)
        return 0
    if args.command == "send":
        sock = _open(parse_address(args.bind, allow_ephemeral=True))
        try:
            rows = run_sender(
                sock,
                parse_address(args.target),
                count=args.count,
                rate=args.rate,
                size=args.size,
                linger=args.linger,
                session=int(time.time() * 1000) & 0xFFFFFFFF,
                wait=_select_wait(sock),
            )
        finally:
            sock.close()
        write_csv(rows, args.output)
        print(format_summary(summarize(rows)))
        return 0
    result = summarize(
        read_csv(args.csv),
        frame_bits=args.frame_bits,
        heartbeat_interval=args.heartbeat_interval,
        heartbeat_timeout=args.heartbeat_timeout,
    )
    print(format_summary(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


__all__ = [
    "ProbeRow",
    "format_summary",
    "main",
    "make_packet",
    "parse_packet",
    "read_csv",
    "run_echo",
    "run_sender",
    "summarize",
    "write_csv",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
