"""Hermes-Lite 2 UDP session built on the pure Protocol 1 codec.

Constructing these classes does nothing. RF is possible only when
``HL2Session.transmit`` is explicitly called with a socket-backed transport.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from hfbridge.hpsdr import (
    DISCOVERY_PACKET,
    EP2Builder,
    SAMPLES_PER_PACKET,
    START_PACKET,
    STOP_PACKET,
    is_ep6_packet,
)

HPSDR_PORT = 1024
# Register 0x17 is configured for a 50 ms TX buffer. Twenty 2.625 ms packets
# preload 52.5 ms before the locally paced stream begins.
_TX_PREFETCH_PACKETS = 20


class TransmissionCancelled(RuntimeError):
    """Raised after a requested TX stop has forced MOX low."""


class Transport(Protocol):
    def send(self, data: bytes) -> None:
        """Send one UDP payload."""
        raise NotImplementedError

    def recv(self, timeout: float) -> bytes:
        """Receive one UDP payload."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the transport."""
        raise NotImplementedError


class UdpTransport:
    """Connected UDP transport. Creating it contacts no radio."""

    def __init__(self, host: str, port: int = HPSDR_PORT):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.connect((host, port))

    def send(self, data: bytes) -> None:
        self._socket.send(data)

    def recv(self, timeout: float) -> bytes:
        self._socket.settimeout(timeout)
        return self._socket.recv(2048)

    def close(self) -> None:
        self._socket.close()


@dataclass(frozen=True)
class DiscoveredHL2:
    ip: str
    mac: str
    gateware_version: int
    board_id: int


def discover(timeout: float = 1.0) -> list[DiscoveredHL2]:
    """Discover HL2 boards over Ethernet. This cannot set MOX or generate RF."""
    found: dict[str, DiscoveredHL2] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        sock.bind(("", 0))
        sock.sendto(DISCOVERY_PACKET, ("255.255.255.255", HPSDR_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, address = sock.recvfrom(2048)
            except socket.timeout:
                break
            if len(data) <= 10 or data[:2] != b"\xef\xfe":
                continue
            mac = ":".join(f"{byte:02x}" for byte in data[3:9])
            found[address[0]] = DiscoveredHL2(
                ip=address[0],
                mac=mac,
                gateware_version=data[9],
                board_id=data[10],
            )
    finally:
        sock.close()
    return list(found.values())


@dataclass
class HL2Session:
    transport: Transport
    builder: EP2Builder
    receive_timeout: float = 1.0
    pre_roll_packets: int = 10
    post_roll_packets: int = 4

    def _wait_first_ep6(self, cancel: threading.Event | None = None) -> None:
        """Block until the radio is streaming, or time out."""
        deadline = time.monotonic() + self.receive_timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise TransmissionCancelled("transmission stopped")
            remaining = min(0.1, max(0.01, deadline - time.monotonic()))
            try:
                data = self.transport.recv(remaining)
            except TimeoutError:
                continue
            if is_ep6_packet(data):
                return
        raise TimeoutError("timed out waiting for an HL2 EP6 packet")

    def _send_stream(
        self, packets: list[bytes], cancel: threading.Event | None = None
    ) -> None:
        """Preload the TX FIFO, then follow the radio's EP6 sample clock.

        A host-side sleep clock can drift away from the FPGA clock and empty
        the small Hermes TX FIFO, which drops PTT and chatters the T/R relay.
        The preload absorbs ordinary network jitter; subsequent EP6 packets
        provide the exact radio-rate clock without disabling the watchdog.
        """
        for i, packet in enumerate(packets):
            if cancel is not None and cancel.is_set():
                raise TransmissionCancelled("transmission stopped")
            if i >= _TX_PREFETCH_PACKETS:
                self._wait_first_ep6(cancel)
            self.transport.send(packet)

    def _send_stop(self) -> None:
        # Quisk sends stop twice. STOP has no MOX bit and cannot key the PA.
        self.transport.send(STOP_PACKET)
        self.transport.send(STOP_PACKET)

    def _best_effort_shutdown(self) -> None:
        """Try every safe-state action even if an earlier UDP send fails."""
        for _ in range(3):
            try:
                self.transport.send(self.builder.build(mox=False))
            except Exception:
                # The enabled hardware watchdog remains the final backstop.
                pass
        for _ in range(2):
            try:
                self.transport.send(STOP_PACKET)
            except Exception:
                pass
        try:
            self.transport.close()
        except Exception:
            pass

    def transmit(
        self, iq: np.ndarray, *, cancel: threading.Event | None = None
    ) -> None:
        """Send one finite waveform, with MOX low before and after it."""
        samples = np.asarray(iq, dtype=np.complex64)
        try:
            if cancel is not None and cancel.is_set():
                raise TransmissionCancelled("transmission stopped")

            # Construct every EP2 packet before starting the radio's EP6
            # stream.  Building a several-second burst after START lets EP6
            # packets accumulate in the socket receive queue.  Those stale
            # packets then look like live clock ticks and cause a rapid EP2
            # burst, overflowing the TX FIFO before it subsequently starves.
            prime_packets = [self.builder.build(mox=False) for _ in range(4)]

            outgoing: list[bytes] = []
            pre_roll = self.pre_roll_packets
            if pre_roll:
                outgoing.extend(
                    self.builder.build(mox=False) for _ in range(pre_roll - 1)
                )
                # N2ADR C0 clicks the filter relays. Do it once, in the last
                # idle packet, so it is not a third click a second before TX.
                outgoing.append(self.builder.build(mox=False, seat_filters=True))
            for start in range(0, len(samples), SAMPLES_PER_PACKET):
                outgoing.append(
                    self.builder.build(
                        samples[start : start + SAMPLES_PER_PACKET], mox=True
                    )
                )
            outgoing.extend(
                self.builder.build(mox=False) for _ in range(self.post_roll_packets)
            )

            self._send_stop()

            # Quisk primes EP2 before starting EP6. MOX remains low.
            for packet in prime_packets:
                self.transport.send(packet)
            # Bit 7 is deliberately clear: the HL2 watchdog remains enabled.
            self.transport.send(START_PACKET)
            self._wait_first_ep6(cancel)

            self._send_stream(outgoing, cancel)
        finally:
            # Cleanup does not depend on receiving another radio clock packet.
            # Repeating MOX-low controls makes failure halfway through a burst
            # fail toward receive; stop then halts Protocol 1 streaming.
            self._best_effort_shutdown()


def emergency_stop(host: str, builder: EP2Builder) -> None:
    """Send an out-of-band MOX-low/STOP sequence to one HL2."""
    transport = UdpTransport(host)
    try:
        for _ in range(3):
            transport.send(builder.build(mox=False))
        transport.send(STOP_PACKET)
        transport.send(STOP_PACKET)
    finally:
        transport.close()
