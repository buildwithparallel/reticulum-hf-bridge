"""OpenHPSDR Protocol 1 wire encoding for Hermes-Lite 2 transmit.

This module only builds bytes. It never opens a socket or keys a radio.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

HPSDR_SAMPLE_RATE = 48_000
SAMPLES_PER_SUBFRAME = 63
SAMPLES_PER_PACKET = SAMPLES_PER_SUBFRAME * 2
EP2_PACKET_SIZE = 1032
EP6 = 0x06
# Standard N2ADR filter-board bits for 12/10 m: 3 MHz HPF + highest LPF.
FILTER_10M = 0b1100000
# HL2 register 0x17 controls the transmit FIFO. Fifty milliseconds gives the
# host and an ordinary wired switch enough jitter margin without disabling the
# hardware watchdog; PTT hang bridges a brief refill instead of chattering the
# T/R relay.
TX_BUFFER_LATENCY_MS = 50
PTT_HANG_MS = 30

SYNC = b"\x7f\x7f\x7f"
DISCOVERY_PACKET = b"\xef\xfe\x02" + bytes(60)
STOP_PACKET = b"\xef\xfe\x04\x00" + bytes(60)
START_PACKET = b"\xef\xfe\x04\x01" + bytes(60)


def is_ep6_packet(data: bytes) -> bool:
    """Return whether *data* is a valid-sized radio-to-host IQ packet."""
    return (
        len(data) == EP2_PACKET_SIZE
        and data[:3] == b"\xef\xfe\x01"
        and data[3] == EP6
    )


@dataclass
class ControlBank:
    """The 17 control words sent round-robin in Protocol 1."""

    frequency_hz: int = 28_124_000
    drive: int = 0
    pa_enabled: bool = False
    filter_bits: int = FILTER_10M
    tx_buffer_latency_ms: int = TX_BUFFER_LATENCY_MS
    ptt_hang_ms: int = PTT_HANG_MS
    _words: list[bytearray] = field(
        default_factory=lambda: [bytearray(4) for _ in range(0x18)],
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not 0 <= self.frequency_hz <= 0xFFFFFFFF:
            raise ValueError("frequency_hz must fit in 32 bits")
        if not 0 <= self.drive <= 255:
            raise ValueError("drive must be between 0 and 255")
        if not 0 <= self.filter_bits <= 0x7F:
            raise ValueError("filter_bits must fit in 7 bits")
        if not 0 <= self.tx_buffer_latency_ms <= 0x7F:
            raise ValueError("tx_buffer_latency_ms must be between 0 and 127")
        if not 0 <= self.ptt_hang_ms <= 0x1F:
            raise ValueError("ptt_hang_ms must be between 0 and 31")

        # One receiver, duplex enabled. Duplex keeps RX and TX tuning separate.
        # C2 bits 7:1 drive the standard N2ADR/Alex filter board. 0x60
        # selects the 3 MHz high-pass plus the 10/12 m low-pass path.
        self._words[0][1] = self.filter_bits << 1
        self._words[0][3] = 0x04
        encoded_frequency = self.frequency_hz.to_bytes(4, "big")
        self._words[1][:] = encoded_frequency  # TX frequency
        self._words[2][:] = encoded_frequency  # RX1 frequency

        # C0 index 9: C1 is drive; bit 19 (C2 bit 3) enables the onboard PA.
        self._words[9][0] = self.drive
        if self.pa_enabled:
            self._words[9][1] |= 0x08

        # Hermes-Lite 2 extension register 0x17. These values are sent before
        # MOX in the ordinary idle control rotation.
        self._words[0x17][2] = self.ptt_hang_ms
        self._words[0x17][3] = self.tx_buffer_latency_ms

    def control(self, index: int, *, mox: bool) -> bytes:
        if not 0 <= index < len(self._words):
            raise ValueError("control index must be between 0 and 16")
        c0 = (index << 1) | int(mox)
        return bytes([c0]) + bytes(self._words[index])


def quantize_iq(iq: np.ndarray, *, amplitude: float) -> np.ndarray:
    """Convert complex float IQ to Quisk-compatible wire I/Q int16 pairs.

    Hermes/Quisk places imag(z) in the wire I slot and real(z) in wire Q.
    """
    if not 0.0 <= amplitude <= 1.0:
        raise ValueError("amplitude must be between 0 and 1")
    samples = np.asarray(iq, dtype=np.complex64)
    scale = amplitude * 32767.0
    wire_i = np.clip(np.rint(samples.imag * scale), -32768, 32767)
    wire_q = np.clip(np.rint(samples.real * scale), -32768, 32767)
    return np.column_stack((wire_i, wire_q)).astype(">i2", copy=False)


@dataclass
class EP2Builder:
    """Build sequential host-to-radio EP2 packets."""

    controls: ControlBank
    amplitude: float
    sequence: int = 0
    control_index: int = 1

    def _next_idle_index(self) -> int:
        """Rotate non-filter controls; C0 retoggles the N2ADR relays."""
        index = self.control_index
        last = len(self.controls._words) - 1
        if not 1 <= index <= last:
            index = 1
        nxt = index + 1
        self.control_index = 1 if nxt > last else nxt
        return index

    def build(
        self,
        iq: np.ndarray | None = None,
        *,
        mox: bool = False,
        seat_filters: bool = False,
    ) -> bytes:
        if iq is None:
            samples = np.zeros(SAMPLES_PER_PACKET, dtype=np.complex64)
        else:
            samples = np.asarray(iq, dtype=np.complex64)
            if len(samples) > SAMPLES_PER_PACKET:
                raise ValueError(
                    f"EP2 packet holds at most {SAMPLES_PER_PACKET} samples"
                )
            if len(samples) < SAMPLES_PER_PACKET:
                samples = np.pad(samples, (0, SAMPLES_PER_PACKET - len(samples)))

        wire_iq = quantize_iq(samples, amplitude=self.amplitude)
        packet = bytearray(EP2_PACKET_SIZE)
        packet[:4] = b"\xef\xfe\x01\x02"
        struct.pack_into(">I", packet, 4, self.sequence & 0xFFFFFFFF)
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF

        for half, base in enumerate((8, 520)):
            packet[base : base + 3] = SYNC
            # C0 index 0 retoggles the N2ADR relays even when the bits are
            # unchanged. Seat filters once, immediately before MOX, and never
            # again in this shout. While keyed, only TX frequency is sent.
            if mox:
                index = 1
            elif seat_filters and half == 0:
                index = 0
            else:
                index = self._next_idle_index()
            packet[base + 3 : base + 8] = self.controls.control(index, mox=mox)

            sample_base = half * SAMPLES_PER_SUBFRAME
            cursor = base + 8
            for wire_i, wire_q in wire_iq[
                sample_base : sample_base + SAMPLES_PER_SUBFRAME
            ]:
                # Four audio bytes remain zero, followed by I and Q.
                struct.pack_into(">hh", packet, cursor + 4, int(wire_i), int(wire_q))
                cursor += 8

        return bytes(packet)


def packetize(
    iq: np.ndarray,
    *,
    controls: ControlBank,
    amplitude: float,
    mox: bool = True,
) -> list[bytes]:
    """Packetize a finite waveform without opening a radio connection."""
    samples = np.asarray(iq, dtype=np.complex64)
    builder = EP2Builder(controls=controls, amplitude=amplitude)
    packets = []
    for start in range(0, len(samples), SAMPLES_PER_PACKET):
        packets.append(
            builder.build(samples[start : start + SAMPLES_PER_PACKET], mox=mox)
        )
    return packets
