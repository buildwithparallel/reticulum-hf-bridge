# FCC / public monitoring

This is the on-air notice for anyone listening on 10 meters — including
the FCC. Product intro: **[README.md](../README.md)**.

This is the on-air notice. Digital station ID counts under Part 97.119
because this recipe is public. The RF hop is **not encoded to obscure
meaning** (Part 97.113(a)(4)). Anyone with a receiver can read the text.

Reticulum encryption stops at the transmitting bridge and starts again
after a receiving bridge decodes the shout. **Nothing on 10 meters is a
Reticulum packet.**

Code: `src/hfbridge/frame.py` (bytes), `src/hfbridge/fsk.py` (tones).
Implementer notes: [over-the-air.md](over-the-air.md).

## Modulation

| Item | Value |
| --- | --- |
| Band | 10 m amateur |
| Center | **28.124 MHz** (move if busy; stay in 28.120–28.189 MHz) |
| Mode | USB / IQ, **2-CPFSK** around that center |
| Mark (`1`) | +50 Hz |
| Space (`0`) | −50 Hz |
| Rate | **100 baud**, high bit first |
| Occupied bandwidth | ~300 Hz on the data body (limit 2.8 kHz) |
| FEC | LDPC (128,64) rate 1/2; CRC-16 last |
| Encryption | **none** |
| Station ID | amateur callsign in ORIGIN, every shout |

Tune a receiver to 28.124 MHz USB. A `1` is a tone 50 Hz above that
center; a `0` is 50 Hz below. 100 of those flips per second.

New shouts, in order, still as those two tones:

1. 7-symbol Costas wake-up (finds time and frequency)
2. Preamble `55 55 55 55 55 55 55 55` (`01010101` click-track)
3. Unique word `FD 59 BB 49 C5 E5 18 40`
4. Three copies of the LDPC block count, then the coded payload
5. 16-byte `55` tail (timing pad, not text)

Receivers still accept older 300-baud shouts (±125 Hz and ±250 Hz) and
the older 32-bit sync `2E FC 37 49`.

## Frame (plaintext)

| Offset | Bytes | Field | Meaning |
| --- | --- | --- | --- |
| 0 | 1 | VER/TYPE | version 1, type 0 = data |
| 1 | 1 | FLAGS | unused, 0 |
| 2 | 6 | ORIGIN | sending station callsign (radix-40) |
| 8 | 16 | DEST | recipient LXMF hash, not a name |
| 24 | 2 | MSG_ID | sender sequence number |
| 26 | 1 | FRAG | piece index/count (`01` = whole message) |
| 27 | 1 | LEN | payload length |
| 28 | n | PAYLOAD | UTF-8 text, max 200 bytes |
| 28+n | 2 | CRC | CRC-16-CCITT, poly `0x1021`, init `0xFFFF` |

CRC covers everything above it. No match → drop. No decrypt step.

## Example shout

This is the traffic this station actually carries: a status note from a
Reticulum client with no internet path, addressed to an ordinary LXMF
inbox on the public mesh. The text is the whole message. There is no
second, hidden layer.

- **Origin:** N0CALL (an example callsign; transmitters use their own)
- **Dest:** `00112233445566778899aabbccddeeff` — an example Reticulum
  delivery address, not a name or location
- **Message:** `no internet here. all ok. next check 0900` (41 characters)

Inner packet, **71 bytes**:

```
10 00 57 7b 2a 46 f8 00 00 11 22 33 44 55 66 77
88 99 aa bb cc dd ee ff 00 2a 01 29 6e 6f 20 69
6e 74 65 72 6e 65 74 20 68 65 72 65 2e 20 61 6c
6c 20 6f 6b 2e 20 6e 65 78 74 20 63 68 65 63 6b
20 30 39 30 30 d3 a1
```

| Offset | Hex | Meaning |
| --- | --- | --- |
| 0 | `10` | version 1, data |
| 1 | `00` | flags off |
| 2 | `57 7b 2a 46 f8 00` | ORIGIN `N0CALL` (radix-40) |
| 8 | `00 11 22 … ee ff` | DEST, example 16-byte Reticulum address |
| 24 | `00 2a` | MSG_ID 42 |
| 26 | `01` | fragment 1 of 1 |
| 27 | `29` | 41 bytes of text follow |
| 28 | `6e 6f 20 69 … 30 30` | `no internet here. all ok. next check 0900` |
| 69 | `d3 a1` | CRC-16 |

On the air that packet is LDPC-wrapped (9 blocks, header `09 09 09`) plus
the Costas wake-up, click-track, unique word, and tail: **179 bytes** of
FSK plus 7 Costas symbols, about **14 s** at 100 baud.

Most of the emission is addressing and error correction, not typed
characters. 41 characters of text cost ~14 seconds; the transmitter
refuses anything over 200 characters.

## How to decode a shout

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.listen --quiet
```

No amateur license is required to listen. Transmitting requires Technician
or higher and a callsign in ORIGIN.

## Why this frequency and this band

Verify current rules before transmitting; they change.

**Technician data.** A Technician (or higher) may use data on 10 meters
from **28.000–28.300 MHz** at up to 200 W PEP. The Hermes-Lite 2 is about
5 W. Technicians do not have general HF data privileges on the lower
bands, which is why this project stays on 10 m.

**Automatic control.** An unattended bridge is an automatically controlled
digital station. Part 97.221(b) on 10 m is **28.120–28.189 MHz**. That is
the only HF window where a Technician can legally run unattended digital.

**28.124 MHz** sits 4 kHz above the bottom of that window (so a narrow FSK
does not hang off the edge) and well above the usual 10 m FT8/JS8 spots
near 28.074 MHz. It is a working center, not a band plan. Stay inside
28.120–28.189 MHz, keep occupied bandwidth under **2.8 kHz**, and move if
the frequency is busy.

**Station identification.** Part 97.119 requires ID at least every 10
minutes and at the end of a communication. In-band digital ID counts
**only while this recipe stays public**. The callsign is in ORIGIN on
every shout.

**Third-party traffic.** Relaying for non-licensed parties is generally
permitted domestically and restricted internationally to countries with
agreements. A bridge that forwards everything it hears will eventually
forward something it should not. The transmitting station filters before
keying (readable text only, length cap, optional allow list). The control
operator is still responsible for every emission.
