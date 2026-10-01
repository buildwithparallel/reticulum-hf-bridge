"""LXMF ↔ over-the-air frame mapping. No Reticulum import.

The origin cannot address the final dest over RNS (that would skip HF), so
the dest hash rides in the message the origin sends to the TX bridge.

    title:   hfdest:<32 hex>
    content: plaintext

The ingress rebuilds a new LXMF message to that hash. The origin's RNS
identity does not survive the air; the dest sees the ingress as source
and the amateur callsign in the title.
"""

from __future__ import annotations

from hfbridge.frame import DEST_LEN, Frame, check_payload

TITLE_DEST_PREFIX = "hfdest:"
TITLE_VIA_PREFIX = "hfvia:"


def parse_dest_hash(value: str) -> bytes:
    try:
        data = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("destination must be hexadecimal") from exc
    if len(data) != DEST_LEN:
        raise ValueError("destination must be exactly 32 hex characters")
    return data


def bridge_title(dest: bytes) -> str:
    if len(dest) != DEST_LEN:
        raise ValueError("dest must be 16 bytes")
    return TITLE_DEST_PREFIX + dest.hex()


def via_title(callsign: str) -> str:
    return TITLE_VIA_PREFIX + callsign.strip()


def parse_bridge_title(title: str) -> bytes:
    text = title.strip()
    if not text.startswith(TITLE_DEST_PREFIX):
        raise ValueError(
            f"bridge message title must start with {TITLE_DEST_PREFIX}"
        )
    return parse_dest_hash(text[len(TITLE_DEST_PREFIX) :])


def frame_from_lxmf(
    *,
    callsign: str,
    title: str,
    content: bytes | str,
    msg_id: int,
) -> Frame:
    dest = parse_bridge_title(title)
    if isinstance(content, str):
        payload = content.encode("utf-8")
    else:
        payload = content
    check_payload(payload)
    return Frame(origin=callsign, dest=dest, payload=payload, msg_id=msg_id)
