"""Pack amateur callsigns into 6 bytes (9 radix-40 symbols)."""

ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-."
_INDEX = {ch: i for i, ch in enumerate(ALPHABET)}
SYMBOLS = 9
WIDTH = 6


def pack(callsign: str) -> bytes:
    text = callsign.strip().upper()
    if not text:
        raise ValueError("callsign is empty")
    if len(text) > SYMBOLS:
        raise ValueError(f"callsign longer than {SYMBOLS} characters: {callsign!r}")
    padded = text.ljust(SYMBOLS)
    value = 0
    for ch in padded:
        if ch not in _INDEX:
            raise ValueError(f"unsupported callsign character {ch!r}")
        value = value * 40 + _INDEX[ch]
    return value.to_bytes(WIDTH, "big")


def unpack(data: bytes) -> str:
    if len(data) != WIDTH:
        raise ValueError(f"origin field must be {WIDTH} bytes")
    value = int.from_bytes(data, "big")
    chars = []
    for _ in range(SYMBOLS):
        value, rem = divmod(value, 40)
        chars.append(ALPHABET[rem])
    if value:
        raise ValueError("origin field out of radix-40 range")
    return "".join(reversed(chars)).rstrip()
