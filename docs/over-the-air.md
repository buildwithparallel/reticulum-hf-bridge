# How data goes over the air

Implementer notes: Hermes packets, Costas, LDPC, legacy modes, receiver
steps. The public monitor spec — frequency, tones, frame bytes, and a
worked shout — is **[fcc.md](fcc.md)**. In-band callsign ID counts under
Part 97.119 because that file is published.

Code: `src/hfbridge/frame.py` (bytes), `src/hfbridge/fsk.py` (tones).
Defaults below are on the air as of 2026-08-24. Receivers still hear
older shouts.

## The picture

```
Hermes-Lite 2  →  2-CPFSK beeps on 10 meters  →  any radio that can hear them
                      (plaintext, not secret)
```

One shout. No “got it?” back over HF. `MSG_ID` is in the frame so a later
receiver can ignore a duplicate; the PoC ingress does not yet drop repeats.

## Where we shout

| Knob | Value | Plain English |
| --- | --- | --- |
| Band | 10 meters | The only HF data band a US Technician can use |
| Center frequency | **28.124 MHz** | Inside the unattended-digital window 28.120–28.189 MHz |
| How we aim | IQ / USB, tones around that center | Two data tones sit a little above and below 28.124 MHz |
| Occupied bandwidth | about **300 Hz** on the data body | Under the 2.8 kHz HF data limit. Costas preamble briefly spans about −250…+350 Hz. |
| Power | Hermes-Lite 2, ~5 W max | Stay as low as the test allows. Same-room tests should be tiny. |

If the frequency is busy, move — stay inside 28.120–28.189 MHz. Transmit
needs Technician or higher. Listening does not.

## How we beep (2-CPFSK)

**FSK:** a `0` is one tone, a `1` is a slightly different tone. **2-CPFSK:**
only those two, continuous phase, constant envelope (easy on the cheap
Hermes PA).

| Knob | Value | Plain English |
| --- | --- | --- |
| Modulation | 2-CPFSK | Constant envelope for the HL2 PA |
| Mark (`1`) | center **+ 50 Hz** | The high tone |
| Space (`0`) | center **− 50 Hz** | The low tone |
| Shift | 100 Hz between the two tones | Equal to the baud: orthogonal mark/space |
| Baud | **100** bits per second | On-air default |
| Bits in a byte | high bit first | Same order as `numpy.unpackbits` |
| FEC | LDPC (128,64) rate 1/2 | Soft min-sum. CRC still has the last word. |
| Handshake | none | No HF ack |

A rough bandwidth check on the body: 2 × (50 Hz + 100 baud) ≈ **300 Hz**.

300 baud ±125 Hz and 300 baud ±250 Hz are **receive-only** so old captures
still decode.

### How Python feeds the Hermes-Lite 2

The modem makes complex I/Q at **48,000 samples per second**. OpenHPSDR
Protocol 1 carries them over **wired Ethernet** (Wi-Fi smears FSK):

- one 1,032-byte UDP packet holds 126 I/Q samples
- each outgoing packet follows one incoming packet from the radio
- TX frequency is set to 28.124 MHz before keying
- N2ADR filter control `0x60` (3 MHz HPF + 10/12 m LPF) before keying
- MOX off during setup, on only for the finite message, then off
- HL2 watchdog stays enabled so a dead Python process cannot keep TX up

Dry-run (no socket, no RF):

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.transmit \
  --callsign YOURCALL --text "no internet here. all ok. next check 0900"
```

Keying RF needs `--hl2-ip`, `--filter-confirmed`, and `--arm-tx`. Station
setup (Ethernet, drive vs amplitude, test tone): **[end-to-end.md](end-to-end.md)**.

### Wake-up (not in the checksum)

| Piece | What | Why |
| --- | --- | --- |
| Costas | 7 symbols, FT8 order `(3,1,4,0,6,5,2)`, offset off DC | Joint time/frequency peak. ~70 ms at 100 baud. |
| Preamble | `55 55 55 55 55 55 55 55` | `01010101` click-track for the timing loop |
| Unique word | `FD 59 BB 49 C5 E5 18 40` | 63-bit m-sequence. Correlator if Costas misses. Old sync `2E FC 37 49` still works. |

Then the packet bytes, still as the two data tones. Byte layout:
**[fcc.md](fcc.md)**.

### LDPC wrapper and airtime

After the unique word:

```
3 bytes   n_blocks, repeated three times (majority vote)
n×16 B    n_blocks of a (128,64) rate-1/2 LDPC codeword, 16-way interleaved
```

Each 64 information bits become 128 bits on the air. Soft min-sum, then
CRC. Older CRC-only shouts (no repeated n_blocks) still decode.

At **100 baud with LDPC and Costas**:

| Text | About |
| --- | --- |
| a short line (~50 bytes) | ~16 s |
| 100 bytes | ~25 s |
| max 200 bytes | ~40 s |

A one-line message is fine. A file is not.

## What the receiver does

1. Tune to 28.124 MHz (RTL path: 20 kHz below, mix in software, so the
   dongle’s LO spike is not sitting on the two tones).
2. Decimate 1.2 MS/s → 6 kS/s with real anti-alias filters, not a block
   average (averaging costs ~21 dB and folds the LO spike onto the channel).
3. Detect an energy burst; ignore excursions shorter than 2.5 s.
4. Costas lock. If that fails, correlate preamble + unique word on a
   ±400 Hz grid. Track carrier and symbol timing. Try both polarities.
   Also try legacy 300 baud ±125 Hz and ±250 Hz.
5. If the next three bytes agree on a block count, LDPC-decode; else
   treat as CRC-only.
6. Read LEN, payload, CRC. Fail → drop. No decrypt.

Capture never waits on demodulation: a worker thread decodes queued IQ;
if the queue fills, the **new** job is dropped. Failed bursts go to
`captures/` for replay. Use `read_async`, not `read_sync` (sync reads
insert a phase jump every 131072 samples).

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.listen --quiet
```

## Not in this proof of concept

Signatures, ARQ, UTC slotting, and a true weak-signal ladder below 100
baud. Skip/fading survival is **[future-enhancements.md](future-enhancements.md)**.
CRC is still the last word on whether a shout is forwarded.
