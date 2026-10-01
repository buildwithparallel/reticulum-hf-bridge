"""Build a Hermes-Lite 2 test burst; dry-run unless explicitly armed."""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass

import numpy as np

from hfbridge.fec import encode_coded
from hfbridge.frame import VERSION, VERSION_LIGHT, Frame, check_payload
from hfbridge.fsk import (
    DEFAULT_BAUD,
    DEFAULT_DEVIATION,
    POSTAMBLE_BYTES,
    PREAMBLE_BYTES,
    SYNC,
    modulate,
)
from hfbridge.hl2 import HL2Session, UdpTransport, discover
from hfbridge.hpsdr import (
    FILTER_10M,
    HPSDR_SAMPLE_RATE,
    ControlBank,
    EP2Builder,
    packetize,
)

DEFAULT_FREQUENCY = 28_124_000
MIN_CENTER_FREQUENCY = 28_121_000
MAX_CENTER_FREQUENCY = 28_188_000
DEFAULT_AMPLITUDE = 0.02
MAX_AMPLITUDE = 1.0
# Bench-era name; digital full scale with drive 255 is about 5 W.
MAX_ANTENNA_TEST_AMPLITUDE = MAX_AMPLITUDE
# C0 index 9, C1[7:0]. 0 is the hardware minimum, not "a bit quieter"; Quisk
# ships 127 as its default. Full scale output on the HL2 is roughly 5 W.
DEFAULT_DRIVE = 127
FULL_SCALE_WATTS = 5.0
DEFAULT_TONE_OFFSET = 1000.0


@dataclass(frozen=True)
class TxPlan:
    frame: Frame | None
    frequency_hz: int
    amplitude: float
    drive: int
    iq: np.ndarray
    packets: tuple[bytes, ...]
    description: str

    @property
    def airtime_seconds(self) -> float:
        return len(self.iq) / HPSDR_SAMPLE_RATE

    @property
    def estimated_watts(self) -> float:
        """Order-of-magnitude only; the real curve is not this tidy."""
        return FULL_SCALE_WATTS * (self.drive / 255.0) ** 2 * self.amplitude**2


def _destination(value: str) -> bytes:
    try:
        data = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("destination must be hexadecimal") from exc
    if len(data) != 16:
        raise ValueError("destination must be exactly 32 hex characters")
    return data


def _validate(frequency_hz: int, amplitude: float, drive: int) -> None:
    if not MIN_CENTER_FREQUENCY <= frequency_hz <= MAX_CENTER_FREQUENCY:
        raise ValueError(
            "frequency must leave the emission inside 28.120-28.189 MHz"
        )
    if not 0 < amplitude <= MAX_AMPLITUDE:
        raise ValueError(
            f"amplitude must be above 0 and at most {MAX_AMPLITUDE}"
        )
    if not 0 <= drive <= 255:
        raise ValueError("drive must be between 0 and 255")


def _finish(
    *,
    frame: Frame | None,
    iq: np.ndarray,
    frequency_hz: int,
    amplitude: float,
    drive: int,
    description: str,
) -> TxPlan:
    controls = ControlBank(
        frequency_hz=frequency_hz,
        drive=drive,
        pa_enabled=True,  # Required for the HL2 main ANT connector.
    )
    packets = tuple(packetize(iq, controls=controls, amplitude=amplitude))
    return TxPlan(
        frame=frame,
        frequency_hz=frequency_hz,
        amplitude=amplitude,
        drive=drive,
        iq=iq,
        packets=packets,
        description=description,
    )


def build_plan(
    *,
    callsign: str,
    text: str,
    destination: str,
    message_id: int,
    frequency_hz: int,
    amplitude: float,
    drive: int = DEFAULT_DRIVE,
    light: bool = False,
) -> TxPlan:
    _validate(frequency_hz, amplitude, drive)
    payload = text.encode("utf-8")
    check_payload(payload)
    frame = Frame(
        origin=callsign,
        dest=_destination(destination),
        payload=payload,
        msg_id=message_id,
        version=VERSION_LIGHT if light else VERSION,
    )
    # Generate at the native HL2 TX rate. No 6 kHz upsampling or images.
    iq = modulate(
        frame,
        sample_rate=HPSDR_SAMPLE_RATE,
        baud=DEFAULT_BAUD,
        deviation=DEFAULT_DEVIATION,
        fec=True,
    )
    return _finish(
        frame=frame,
        iq=iq,
        frequency_hz=frequency_hz,
        amplitude=amplitude,
        drive=drive,
        description=(
            f"2-CPFSK, {DEFAULT_BAUD} baud, +/-{DEFAULT_DEVIATION:g} Hz, "
            f"LDPC (128,64) rate 1/2"
        ),
    )


def build_tone_plan(
    *,
    seconds: float,
    frequency_hz: int,
    amplitude: float,
    drive: int = DEFAULT_DRIVE,
    tone_offset: float = DEFAULT_TONE_OFFSET,
) -> TxPlan:
    """A steady carrier: the easiest thing to find when nothing decodes."""
    _validate(frequency_hz, amplitude, drive)
    if not 0 < seconds <= 30:
        raise ValueError("tone length must be above 0 and at most 30 seconds")
    n = int(round(seconds * HPSDR_SAMPLE_RATE))
    t = np.arange(n, dtype=np.float64) / HPSDR_SAMPLE_RATE
    iq = np.exp(2j * np.pi * tone_offset * t).astype(np.complex64)
    return _finish(
        frame=None,
        iq=iq,
        frequency_hz=frequency_hz,
        amplitude=amplitude,
        drive=drive,
        description=f"steady carrier at {tone_offset:+g} Hz from centre",
    )


def _print_plan(plan: TxPlan, *, armed: bool, host: str | None) -> None:
    digest = hashlib.sha256(b"".join(plan.packets)).hexdigest()[:16]
    print("Hermes-Lite 2 TX preflight")
    print(f"  mode:          {'ARMED' if armed else 'DRY RUN (no socket)'}")
    print(f"  radio:         {host or 'none'}")
    print(f"  callsign:      {plan.frame.origin if plan.frame else '(tone, none sent)'}")
    print(f"  frequency:     {plan.frequency_hz} Hz")
    print(f"  modulation:    {plan.description}")
    print(f"  PA / drive:    enabled for ANT / {plan.drive} of 255")
    print(f"  TX filter:     N2ADR 10 m control 0x{FILTER_10M:02x}")
    print(f"  IQ amplitude:  {plan.amplitude:.4f} ({plan.amplitude * 100:.2f}%)")
    print(f"  est. power:    ~{plan.estimated_watts * 1000:.2f} mW (rough)")
    print(f"  airtime:       {plan.airtime_seconds:.2f} s")
    print(f"  waveform:      {len(plan.iq)} samples")
    print(f"  EP2 data:      {len(plan.packets)} packets, sha256 {digest}")
    if plan.frame is None:
        return

    encoded = plan.frame.encode()
    coded = encode_coded(encoded)
    on_air = PREAMBLE_BYTES + SYNC + coded + POSTAMBLE_BYTES
    print()
    print(
        f"Inner CRC frame {len(encoded)} bytes; "
        f"LDPC body {len(coded)} bytes; "
        f"{len(on_air)} bytes on air:"
    )
    for line in plan.frame.describe():
        print(f"  {line}")
    print()
    print(f"  preamble     {PREAMBLE_BYTES.hex(' ')}")
    print(f"  unique word  {SYNC.hex(' ')}")
    print(f"  ldpc body    {coded.hex(' ')}")
    print(f"  inner frame  {encoded.hex(' ')}")
    print(f"  postamble    {POSTAMBLE_BYTES.hex(' ')}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover", action="store_true", help="Ethernet discovery only; cannot key RF")
    parser.add_argument("--callsign", help="licensed transmitting station callsign")
    parser.add_argument("--text", default="hello hf", help="short plaintext test message")
    parser.add_argument(
        "--destination",
        default="00" * 16,
        help="16-byte Reticulum destination as 32 hex characters",
    )
    parser.add_argument("--message-id", type=int, default=1)
    parser.add_argument("--frequency", type=int, default=DEFAULT_FREQUENCY)
    parser.add_argument("--amplitude", type=float, default=DEFAULT_AMPLITUDE)
    parser.add_argument(
        "--drive",
        type=int,
        default=DEFAULT_DRIVE,
        help="HL2 hardware drive 0-255; 0 is the silent minimum, not low power",
    )
    parser.add_argument(
        "--tone",
        type=float,
        metavar="SECONDS",
        help="send a steady carrier instead of a frame, to find the signal",
    )
    parser.add_argument("--hl2-ip", help="fixed Hermes-Lite 2 IPv4 address")
    parser.add_argument(
        "--arm-tx",
        action="store_true",
        help="allow the socket-backed path to set MOX (default is dry-run)",
    )
    parser.add_argument(
        "--filter-confirmed",
        action="store_true",
        help="confirm a 10 m transmit low-pass filter is installed",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.discover:
        if args.arm_tx:
            parser.error("--discover and --arm-tx cannot be used together")
        radios = discover()
        if not radios:
            print("No Hermes-Lite 2 found")
            return 1
        for radio in radios:
            print(
                f"{radio.ip} mac={radio.mac} gateware={radio.gateware_version} "
                f"board_id={radio.board_id}"
            )
        return 0

    # A bare carrier still identifies the operator, so require the callsign
    # for tone tests too.
    if not args.callsign:
        parser.error("--callsign is required")

    try:
        if args.tone:
            plan = build_tone_plan(
                seconds=args.tone,
                frequency_hz=args.frequency,
                amplitude=args.amplitude,
                drive=args.drive,
            )
        else:
            plan = build_plan(
                callsign=args.callsign,
                text=args.text,
                destination=args.destination,
                message_id=args.message_id,
                frequency_hz=args.frequency,
                amplitude=args.amplitude,
                drive=args.drive,
            )
    except (ValueError, UnicodeError) as exc:
        parser.error(str(exc))

    _print_plan(plan, armed=args.arm_tx, host=args.hl2_ip)
    if not args.arm_tx:
        print("\nDry run complete. No network socket was opened; no RF was generated.")
        return 0

    if not args.hl2_ip:
        parser.error("--hl2-ip is required with --arm-tx")
    if not args.filter_confirmed:
        parser.error("--filter-confirmed is required with --arm-tx")

    print("\nARMED: opening the HL2 UDP session.", file=sys.stderr)
    controls = ControlBank(
        frequency_hz=plan.frequency_hz,
        drive=plan.drive,
        pa_enabled=True,
    )
    builder = EP2Builder(controls=controls, amplitude=plan.amplitude)
    session = HL2Session(
        transport=UdpTransport(args.hl2_ip),
        builder=builder,
    )
    session.transmit(plan.iq)
    print("Transmit session stopped; MOX-off and stop packets sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
