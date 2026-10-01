# Future enhancements

Product intro: **[README.md](../README.md)**. On-air spec: **[fcc.md](fcc.md)**.

Notes from an August 2026 review, updated after the 2026-08-24 PHY work.
Soft-decision LDPC, orthogonal 100-baud 2-CPFSK, Costas acquisition, and a
burst-gated listener are **on the air**. The items below are what comes
after that.

The optimization target does not change: **range and reliability at low SNR
for short plaintext messages**, not throughput. A closed 10-meter band cannot
be repaired in software.

## What is already true

The PoC is **100-baud 2-CPFSK, ±50 Hz** (orthogonal tone spacing), with a
7-symbol Costas preamble, a 63-bit m-sequence unique word, a (128,64)
sparse LDPC wrapper around the inner CRC-16 frame, no HF acknowledgement,
and a 30-byte inner header+CRC before payload. Transmit goes through the
Hermes-Lite 2. Receive is still an RTL-SDR path (often a Pi). Receivers
still accept CRC-only shouts and earlier 300-baud ±125 / ±250 Hz waveforms.

The listener does not decode every 6 dB blip: bursts shorter than 2.5 s
are dropped, capture is decoupled from demodulation, and a full decode
queue drops the **newest** job. The maximum decode gate is 45 s so the
documented 200-byte frame can pass; longer stuck carriers are discarded.

Weak-signal acquisition now coherently correlates all 128 known
preamble+unique-word symbols before falling back to the 7-symbol Costas
and older correlator. The receiver tries a bounded local time/frequency
grid selected by LDPC+CRC, derives a damaged block-count header from
postamble-aware burst duration, tracks timing and slow carrier drift on
long frames, and normalizes tracked symbol reliability. The RTL path also
has optional software auto-gain that rises only while quiet and immediately
backs off on ADC clipping; hardware AGC remains off.

The LXMF bridge exists (`txbridge` / `ingress`, spam filter, optional
allow list). There is still no duplicate suppression on ingress, no
fragment reassembly of multi-shout payloads in the field, and no
operational identification scheduler beyond putting ORIGIN in every shout.

Neighborhood and across-the-house paths have decoded. A mag-mount 915 MHz
whip on a moving car is antenna-limited. **Ionospheric skip is not
proven.** Higher TX power and real 10 m antennas fix path loss, not
fading. Skip still wants short fragments, a midamble or delayed repeat,
ARQ/HARQ, and a Watterson / ITU-R F.1487 bench.

UTC/NTP slotting was considered and **deferred**. Nodes without a sane
clock (Pi in a car) must keep working asynchronously.

## Order of work

Do these in this order.

1. ~~Close the current RF loop.~~ Done (2026-08-22).
2. ~~Add soft-decision LDPC.~~ Done. CRC remains the last integrity check.
3. **Use a waveform ladder, not one speed.** Fringe is now this
   constant-envelope CPFSK. Next: Codec2 `DATAC4` (250 Hz, 54-byte payload)
   and `DATAC3` (500 Hz, 126 bytes). Good paths only: `DATAC1` (1700 Hz,
   510 bytes). Keep 2-CPFSK as a control or fallback plane.
4. **Add selective-repeat ARQ, then HARQ.** Tiny, extremely robust
   acknowledgements. Retransmit only missing pieces. Later, keep soft
   information from a failed copy and combine it with the next one instead
   of starting over. This is the skip-survival layer.
5. **Adapt from observed outcomes.** Pick the mode that actually delivers
   the most payload, independently in each direction. Raw SNR is a weak
   controller on fading HF.
6. ~~Design real HF acquisition.~~ A coherent 128-symbol lock is primary;
   Costas 2-D and the older correlator remain fallbacks. Timing/drift
   tracking and fade-in tests are in. A **midamble** is still an air-format
   option if Watterson and field captures show fades longer than the current
   interleaver can absorb; a synthetic one-second mid-frame dropout already
   decodes for 30-byte frames, so do not add airtime without that evidence.
7. **Shrink repeated headers.** Bind the callsign and the 16-byte destination
   once per session. After that, use short session and sequence IDs, and
   identify on the public schedule the rules already require.
8. **Support two reliability styles.** ARQ when a receiving bridge answers.
   Systematic parity or fountain fragments when there is no return path.
9. **Schedule around propagation.** Passively watch 10-meter activity, keep
   per-peer link-quality history, listen before transmitting, and drain the
   queue when a path opens. Availability matters more than peak rate.
10. **Test in a Watterson / ITU-R F.1487 channel** before claiming skip.
    Measure packet error and delivered payload under multipath, Doppler,
    frequency error, clock error, impulsive noise, clipping, in-band
    interference, and PA distortion. `hfbridge.benchmark --fading mobile`
    now supplies a repeatable two-path Rayleigh regression profile, but it
    is explicitly Watterson-inspired rather than calibrated F.1487.

## Antenna and radio, not just DSP

These can outweigh several decibels of modem work:

- Native Hermes-Lite 2 receive, so the station node is one radio.
- Outdoor resonant 10 m antenna on **both** ends. A UHF mag-mount whip is
  not a 10 m antenna.
- Common-mode choke, and feed-point out of the house.
- SWR and power telemetry in the transmit log.
- Occupied-bandwidth and spectral-mask checks before any automatic
  operation.
- OFDM PA backoff tests. At 5 W PEP, clipped QPSK will usually beat
  high-PAPR QAM.

## Existing open stacks to bench first

Do not invent a new physical layer until these are measured on the same 5 W,
2.8 kHz, fading-channel path. Keep this project's plaintext frame and legal
policy above whatever modem wins. Do not inherit another project's
encryption.

| Project | Use | Why it matters | Caveat |
| --- | --- | --- | --- |
| [Mercury v2](https://github.com/Rhizomatica/mercury/tree/mercuryv2) | Primary modem candidate | Pi packages, Codec2 OFDM/LDPC, 200–2100 Hz ladder, ARQ, Chase HARQ, robust ACKs | Do not inherit its encryption. Prefer clipped QPSK over high-PAPR QAM at 5 W |
| [FreeDATA](https://github.com/DJ2LS/FreeDATA) 0.17.8 | Stable messaging baseline | Mature Codec2 data path with a REST/WebSocket interface | Later 0.18.x may be less stable; treat as a reference, not the product |
| [ardopcf](https://github.com/pflarue/ardop) | Interoperability baseline | FSK/PSK/QAM, adaptive ARQ, Reed-Solomon, memory combining on small ARM hardware | Useful comparison, not the long-term custom frame |
| [JS8Call-Improved](https://github.com/JS8Call-improved/JS8Call-improved) | Fringe / coordination only | Proven weak-signal messaging when the path is almost gone | Too slow and application-specific for a general Reticulum byte pipe |
| [Modem73](https://github.com/RFnexus/modem73) / [freedvtnc2](https://github.com/xssfox/freedvtnc2) | Lab experiments | Direct KISS/RNS adapters and extra OFDM/MFSK/polar variants | Less published HF evidence than Mercury. Clear test payloads only |
| [ProjectUltra](https://github.com/secup/ProjectUltra/) | Idea source | Selective-repeat ARQ, per-carrier erasures, outcome-fitted adaptation, Watterson tests, HARQ | Pre-alpha. Do not depend on it |
| [Codec2 raw data modes](https://github.com/drowe67/codec2/blob/main/README_data.md) | Shared waveform library | OFDM, LDPC, pre/postamble diversity, measured multipath operating points | The modem primitives, not a complete bridge |

Mercury measurements are especially useful: around −9 dB, DATAC16 decoded
nearly every frame that synchronized, then acquisition collapsed. Better
preamble detection may outperform another stronger code.

Reference operating point to beat, not a promise over the air: Codec2
`DATAC4` on a simulated MultiPath Poor channel (1 Hz Doppler, 2 ms delay)
targets about −4 dB SNR, 87 bits/s of payload, and 90/100 packets decoded.

## Legal gates that do not move

These constrain the enhancements above. Verify current rules before
transmitting; regulations change.

- The RF hop stays **plaintext**. A Reticulum packet is encrypted end to
  end and cannot go on amateur frequencies as-is. The bridge terminates
  encryption, anyone listening can read the air, and the far island
  re-wraps into Reticulum. Compression is allowed only if the algorithm is
  publicly documented.
- Automatic control wider than 500 Hz belongs in **28.120–28.189 MHz**.
- Occupied bandwidth stays under **2.8 kHz**.
- Station identification is still required at least every 10 minutes and at
  the end of a communication. In-band digital ID counts only while this
  protocol remains publicly documented.
- The first station that forwards a third-party message must authenticate
  the source or accept responsibility for the content.
- International third-party traffic is restricted to countries with
  agreements. A bridge that forwards everything it hears will eventually
  forward something it should not.

## Later research, not the next commit

Worth a paper or a bench campaign after the items above exist:

- Incremental-redundancy HARQ: send new parity instead of a full duplicate.
- Per-carrier erasures on OFDM: feed the decoder silence, not confident
  garbage, on a deep notch.
- Time and frequency diversity: a delayed repeat, or a second channel
  inside the automatic-control window, before spending money on a second
  antenna.
- Narrow chirp-spread and short polar codes, compared against Mercury
  DATAC15/16 and CPFSK on the same fading channel.
- Soft combining across two receiving bridges. First-valid-copy forwarding
  is enough until then.
- **Maidenhead grid in the frame.** Put the transmitting station's locator
  in the public header (4-character is enough to start; 6-character if the
  bytes can be spared). Listeners and ingress then know roughly where the
  shout came from, which is useful for 10-meter skip, the receive-side
  heard counts, and later propagation scheduling. Pack it tightly; do not
  spend payload on a free-text QTH. The dest hash still addresses the
  person. The grid is geography, not routing.
