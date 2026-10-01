"""PoC over-the-air frame: documented plaintext, CRC-16, no signature."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from hfbridge.airtext import check_air_text
from hfbridge.radix40 import pack as pack_callsign
from hfbridge.radix40 import unpack as unpack_callsign

VERSION = 1
VERSION_LIGHT = 2
TYPE_DATA = 0
MAX_PAYLOAD = 200
DEST_LEN = 16
# v1: ver/type, flags, origin, dest, msg_id, frag, len
_V1_BEFORE_PAYLOAD = 1 + 1 + 6 + DEST_LEN + 2 + 1 + 1
# light: ver/type, origin, len — no dest, msg_id, flags, or fragment
_LIGHT_BEFORE_PAYLOAD = 1 + 6 + 1

# CRC-16-CCITT, init 0xFFFF, no reflection, xorout 0.
_POLY = 0x1021


def check_payload(payload: bytes) -> bytes:
    if not payload:
        raise ValueError("payload is empty")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload longer than {MAX_PAYLOAD} bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("payload must be readable text") from exc
    check_air_text(text)
    return payload


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ _POLY) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def _hex(data: bytes, *, limit: int = 6) -> str:
    """Fixed-width hex preview; long fields elide their middle."""
    if len(data) <= limit:
        return data.hex(" ")
    return f"{data[:3].hex(' ')} .. {data[-2:].hex(' ')}"


@dataclass(frozen=True)
class Frame:
    origin: str
    dest: bytes
    payload: bytes
    msg_id: int = 1
    frag_index: int = 0
    frag_total: int = 1
    flags: int = 0
    version: int = VERSION
    frame_type: int = TYPE_DATA

    def encode(self) -> bytes:
        check_payload(self.payload)
        if self.version == VERSION_LIGHT:
            return self._encode_light()
        if not (0 <= self.msg_id <= 0xFFFF):
            raise ValueError("msg_id must fit in 2 bytes")
        if not (0 <= self.frag_index <= 15 and 1 <= self.frag_total <= 15):
            raise ValueError("frag nibble out of range")
        dest = self.dest
        if len(dest) != DEST_LEN:
            raise ValueError(f"dest must be {DEST_LEN} bytes")
        ver_type = ((self.version & 0x0F) << 4) | (self.frame_type & 0x0F)
        frag = ((self.frag_index & 0x0F) << 4) | (self.frag_total & 0x0F)
        body = b"".join(
            [
                bytes([ver_type, self.flags & 0xFF]),
                pack_callsign(self.origin),
                dest,
                struct.pack(">H", self.msg_id),
                bytes([frag, len(self.payload)]),
                self.payload,
            ]
        )
        return body + struct.pack(">H", crc16(body))

    def _encode_light(self) -> bytes:
        ver_type = ((VERSION_LIGHT & 0x0F) << 4) | (self.frame_type & 0x0F)
        body = b"".join(
            [
                bytes([ver_type]),
                pack_callsign(self.origin),
                bytes([len(self.payload)]),
                self.payload,
            ]
        )
        return body + struct.pack(">H", crc16(body))

    @staticmethod
    def wire_size(blob: bytes) -> int | None:
        """How many bytes the version nibble says this inner frame occupies."""
        if not blob:
            return None
        version = blob[0] >> 4
        if version == VERSION:
            if len(blob) < _V1_BEFORE_PAYLOAD:
                return None
            return _V1_BEFORE_PAYLOAD + blob[27] + 2
        if version == VERSION_LIGHT:
            if len(blob) < _LIGHT_BEFORE_PAYLOAD:
                return None
            return _LIGHT_BEFORE_PAYLOAD + blob[7] + 2
        return None

    @classmethod
    def inner_from_ldpc(cls, blob: bytes) -> bytes | None:
        """CRC-checked inner frame from LDPC output, even if the version nibble is junk.

        A flipped first nibble used to raise 'LDPC decoded frame truncated'
        and skip CRC even when LEN and the checksum were intact. Restore the
        nibble the layout implies, then accept the slice that CRC-decodes.
        """
        if not blob:
            return None
        candidates: list[bytes] = []
        size = cls.wire_size(blob)
        if size is not None and len(blob) >= size:
            candidates.append(blob[:size])
        if len(blob) >= _LIGHT_BEFORE_PAYLOAD + 2:
            length = blob[7]
            total = _LIGHT_BEFORE_PAYLOAD + length + 2
            if 0 <= length <= MAX_PAYLOAD and len(blob) >= total:
                raw = blob[:total]
                patched = (
                    bytes([((VERSION_LIGHT & 0x0F) << 4) | (raw[0] & 0x0F)]) + raw[1:]
                )
                candidates.extend((raw, patched))
        if len(blob) >= _V1_BEFORE_PAYLOAD + 2:
            length = blob[27]
            total = _V1_BEFORE_PAYLOAD + length + 2
            if 0 <= length <= MAX_PAYLOAD and len(blob) >= total:
                raw = blob[:total]
                patched = bytes([((VERSION & 0x0F) << 4) | (raw[0] & 0x0F)]) + raw[1:]
                candidates.extend((raw, patched))
        seen: set[bytes] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                cls.decode(candidate)
            except ValueError:
                continue
            return candidate
        return None

    def describe(self) -> list[str]:
        """Field-by-field breakdown of the encoded frame, for bench output."""
        encoded = self.encode()
        size = len(self.payload)
        if self.version == VERSION_LIGHT:
            fields = [
                (0, 1, f"version {self.version} light, type {self.frame_type}"),
                (1, 6, f"origin {self.origin} (radix-40)"),
                (7, 1, f"payload length {size}"),
                (8, size, f"payload {self.payload!r}"),
                (8 + size, 2, "crc-16-ccitt over everything above"),
            ]
        else:
            dest = (
                "all zero (unaddressed test)"
                if not any(self.dest)
                else "Reticulum hash"
            )
            fields = [
                (0, 1, f"version {self.version}, type {self.frame_type}"),
                (1, 1, f"flags 0x{self.flags:02x}"),
                (2, 6, f"origin {self.origin} (radix-40)"),
                (8, DEST_LEN, f"dest, {dest}"),
                (24, 2, f"msg_id {self.msg_id}"),
                (26, 1, f"fragment {self.frag_index + 1} of {self.frag_total}"),
                (27, 1, f"payload length {size}"),
                (28, size, f"payload {self.payload!r}"),
                (28 + size, 2, "crc-16-ccitt over everything above"),
            ]
        return [
            f"[{off:>2}] {count:>2}B  {_hex(encoded[off : off + count]):<17}  {meaning}"
            for off, count, meaning in fields
        ]

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        if not data:
            raise ValueError("frame truncated")
        if (data[0] >> 4) == VERSION_LIGHT:
            return cls._decode_light(data)
        header = _V1_BEFORE_PAYLOAD
        if len(data) < header + 2:
            raise ValueError("frame truncated")
        body, crc_bytes = data[:-2], data[-2:]
        expect = struct.unpack(">H", crc_bytes)[0]
        got = crc16(body)
        if got != expect:
            raise ValueError(f"crc mismatch: got {got:04x} expected {expect:04x}")
        ver_type = body[0]
        flags = body[1]
        origin = unpack_callsign(body[2:8])
        dest = body[8:24]
        msg_id = struct.unpack(">H", body[24:26])[0]
        frag = body[26]
        length = body[27]
        payload = body[28:]
        if length != len(payload):
            raise ValueError(f"len field {length} != payload {len(payload)}")
        return cls(
            origin=origin,
            dest=dest,
            payload=payload,
            msg_id=msg_id,
            frag_index=frag >> 4,
            frag_total=frag & 0x0F,
            flags=flags,
            version=ver_type >> 4,
            frame_type=ver_type & 0x0F,
        )

    @classmethod
    def _decode_light(cls, data: bytes) -> Frame:
        header = _LIGHT_BEFORE_PAYLOAD
        if len(data) < header + 2:
            raise ValueError("frame truncated")
        body, crc_bytes = data[:-2], data[-2:]
        expect = struct.unpack(">H", crc_bytes)[0]
        got = crc16(body)
        if got != expect:
            raise ValueError(f"crc mismatch: got {got:04x} expected {expect:04x}")
        ver_type = body[0]
        origin = unpack_callsign(body[1:7])
        length = body[7]
        payload = body[8:]
        if length != len(payload):
            raise ValueError(f"len field {length} != payload {len(payload)}")
        return cls(
            origin=origin,
            dest=bytes(DEST_LEN),
            payload=payload,
            msg_id=0,
            version=VERSION_LIGHT,
            frame_type=ver_type & 0x0F,
        )
