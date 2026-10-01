"""Hermes TX bridge: LXMF destination that keys the radio."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from hfbridge.lxmfconv import frame_from_lxmf
from hfbridge.node import start_node
from hfbridge.transmit import build_plan, DEFAULT_AMPLITUDE, DEFAULT_DRIVE, DEFAULT_FREQUENCY


def parse_allow(values: list[str] | None) -> set[str]:
    allowed: set[str] = set()
    for raw in values or []:
        peer = "".join(
            ch for ch in str(raw).strip().lower() if ch in "0123456789abcdef"
        )
        if len(peer) == 32:
            allowed.add(peer)
    return allowed


def source_allowed(source_hash, allowed: set[str]) -> bool:
    if not allowed:
        return True
    if source_hash is None:
        return False
    if isinstance(source_hash, bytes):
        hexhash = source_hash.hex()
    else:
        hexhash = str(source_hash)
    return hexhash.lower() in allowed


def _stats_line(
    *,
    received: int,
    on_air: int,
    held: int,
    rejected: int,
    tx_failed: int,
    last_bytes: int = 0,
) -> str:
    line = (
        f"txbridge-stats received={received} on_air={on_air} "
        f"held={held} rejected={rejected} tx_failed={tx_failed}"
    )
    if last_bytes:
        line += f" last_bytes={last_bytes}"
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--callsign", required=True)
    parser.add_argument("--hl2-ip")
    parser.add_argument("--arm-tx", action="store_true")
    parser.add_argument("--filter-confirmed", action="store_true")
    parser.add_argument("--amplitude", type=float, default=DEFAULT_AMPLITUDE)
    parser.add_argument("--drive", type=int, default=DEFAULT_DRIVE)
    parser.add_argument("--frequency", type=int, default=DEFAULT_FREQUENCY)
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="HASH",
        help="LXMF source hash allowed to key this station (repeatable)",
    )
    args = parser.parse_args(argv)
    allowed = parse_allow(args.allow)
    if args.arm_tx:
        if not args.hl2_ip:
            parser.error("--hl2-ip is required with --arm-tx")
        if not args.filter_confirmed:
            parser.error("--filter-confirmed is required with --arm-tx")

    next_id = 1
    stats = {
        "received": 0,
        "on_air": 0,
        "held": 0,
        "rejected": 0,
        "tx_failed": 0,
        "last_bytes": 0,
    }

    def _report() -> None:
        print(_stats_line(**stats), flush=True)

    def on_delivery(message) -> None:
        nonlocal next_id
        title = message.title_as_string()
        content = message.content_as_string()
        stats["received"] += 1
        source = message.source_hash.hex() if message.source_hash else ""
        if not source_allowed(source, allowed):
            stats["rejected"] += 1
            _report()
            return
        try:
            frame = frame_from_lxmf(
                callsign=args.callsign,
                title=title,
                content=content,
                msg_id=next_id,
            )
        except ValueError:
            stats["rejected"] += 1
            _report()
            return
        next_id += 1
        stats["last_bytes"] = len(frame.payload)
        plan = build_plan(
            callsign=frame.origin,
            text=frame.payload.decode("utf-8"),
            destination=frame.dest.hex(),
            message_id=frame.msg_id,
            frequency_hz=args.frequency,
            amplitude=args.amplitude,
            drive=args.drive,
        )
        if not args.arm_tx:
            stats["held"] += 1
            _report()
            return
        from hfbridge.hl2 import HL2Session, UdpTransport
        from hfbridge.hpsdr import ControlBank, EP2Builder

        controls = ControlBank(
            frequency_hz=plan.frequency_hz,
            drive=plan.drive,
            pa_enabled=True,
        )
        try:
            session = HL2Session(
                transport=UdpTransport(args.hl2_ip),
                builder=EP2Builder(controls=controls, amplitude=plan.amplitude),
            )
            session.transmit(plan.iq)
        except Exception:
            stats["tx_failed"] += 1
            print("transmit failed", flush=True)
            _report()
            return
        stats["on_air"] += 1
        _report()

    node = start_node(
        args.config,
        name="txbridge",
        display_name="hf-txbridge",
        on_delivery=on_delivery,
    )
    print(f"txbridge {node.hash_hex}", flush=True)
    print(
        f"mode={'ARMED' if args.arm_tx else 'waiting, transmit off'}; "
        f"allowlist={'off' if not allowed else len(allowed)}; "
        "Ctrl-C to stop",
        flush=True,
    )
    _report()
    try:
        while True:
            node.announce()
            time.sleep(15)
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
