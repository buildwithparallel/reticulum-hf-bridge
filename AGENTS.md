# Repository guide

## Purpose

This repository is the radio bridge used by Crosstalk's optional **OTA Long
Haul** path. It translates short LXMF messages to a documented plaintext HF
frame, transmits that frame with a Hermes-Lite 2, receives it with an RTL-SDR,
and creates a new LXMF message on the far Reticulum network.

Crosstalk itself is not included here and this repository is not bundled into
Crosstalk. Crosstalk provides the conversation UI,
the per-conversation **Send over HF** choice, station controls, and process
launching. This repository provides the Python worker processes and radio
protocol those controls invoke.

This is a proof of concept for short text, not a general Reticulum interface or
a transparent packet tunnel. The HF leg has no encryption, acknowledgement,
retry protocol, file transfer, or preservation of the original Reticulum
sender identity.

## How it works with Crosstalk

The working deployment has two deliberately separate Reticulum sides. Do not
connect them through an ordinary Reticulum path, or Reticulum can deliver the
message without using HF.

```text
Origin Crosstalk
    | encrypted Reticulum/LXMF over a localhost TCP island
    v
HF-station Crosstalk + hfbridge.txbridge (transmit)
    | plaintext 2-CPFSK on 10 metres via Hermes-Lite 2
    v
hfbridge.ingress + RTL-SDR
    | a new encrypted Reticulum/LXMF message on the far mesh
    v
Destination Crosstalk, Columba, NomadNet, or another LXMF client
```

In Crosstalk, the Hermes-Lite 2 role controls the transmitting worker and the
RTL-SDR role controls the receiving worker. A complete hop needs both roles on
the two ends; the current Hermes worker does not receive bridge traffic.

### Transmitting side

1. The origin is an otherwise ordinary Crosstalk instance. Its only interface
   for this path is a TCP client to the HF station's localhost TCP server,
   conventionally `127.0.0.1:3742`.
2. The HF-station Crosstalk instance owns that TCP server and must not also be
   connected to the destination's public mesh. Its **OTA Long Haul** screen
   starts and monitors `hfbridge.txbridge` from this repository.
3. `hfbridge.txbridge` registers an LXMF delivery destination named
   `hf-txbridge` on the same local island. Crosstalk discovers its announce and
   offers **Send over HF** when the intended recipient has no normal path.
4. Crosstalk sends an LXMF message to the bridge, not directly to the final
   recipient. The message title is `hfdest:<32-hex-delivery-hash>` and its
   content is the short plaintext body.
5. Because the bridge is the addressed LXMF recipient, it decrypts that local
   hop, validates the destination and body, applies the text/length/allow-list
   gates, adds the licensed station callsign, and builds an HF frame.
6. `hfbridge.txbridge` remains dry-run unless explicitly armed. An armed worker
   uses OpenHPSDR Protocol 1 over wired Ethernet to key the Hermes-Lite 2 for
   one finite transmission.

### Receiving side

1. `hfbridge.ingress` owns the RTL-SDR, detects a burst, demodulates it, applies
   LDPC correction, and accepts it only after the inner CRC passes.
2. Ingress reads the final LXMF delivery hash and UTF-8 body from the decoded
   plaintext frame. It creates a **new** direct LXMF message whose title is
   `hfvia:<CALLSIGN>`.
3. Ingress uses its own Reticulum instance and an interface that can actually
   reach the destination, normally a direct TCP connection to a public RNS
   node. Do not place it behind the transmitting localhost island.
4. The destination sees the ingress identity as the LXMF sender. The original
   sender's Reticulum identity does not cross the radio; only the amateur
   callsign carried by the public HF frame identifies the transmitting station.

A Crosstalk instance on the RTL computer can provide Start/Stop controls and
show worker logs, but it is optional and is not part of the receive message
path. Ingress connects to the far mesh itself.

There is no HF acknowledgement. A local Crosstalk “delivered” state means the
origin reached `txbridge`; `txbridge-stats ... on_air` means the radio was
keyed; `ingress-stats ... forwarded` means ingress handed a reconstructed
message to LXMF. Only the destination application confirms end-to-end receipt.

## On-air format and safety boundary

The current transmit default is 100-baud 2-CPFSK centered on 28.124 MHz, with a
Costas acquisition sequence, preamble and unique word, rate-1/2 `(128,64)`
LDPC, and a CRC-protected inner frame. The inner frame contains the callsign,
16-byte destination hash, message ID, readable UTF-8 body, and CRC. The body is
limited to 200 bytes.

An encrypted Reticulum packet is never sent over amateur radio. Reticulum
encryption ends at `txbridge`, the HF frame is public and monitorable, and a
new encrypted LXMF message begins at `ingress`. Never put credentials, private
content, or opaque encoded data into this path. Transmission requires the
appropriate amateur licence and an accountable control operator. The public
monitor specification is in `docs/fcc.md`; modem details are in
`docs/over-the-air.md`.

## What is included

### Python package: `src/hfbridge/`

- **LXMF/Reticulum integration:** `node.py`, `lxmfconv.py`, `txbridge.py`,
  `ingress.py`, `origin.py`, `dest.py`, `rnssetup.py`, and `hashes.py` create
  isolated RNS instances, map LXMF titles/content to radio frames, and provide
  CLI endpoints for a complete non-Crosstalk test loop.
- **Frame and modem:** `frame.py`, `radix40.py`, `fec.py`, `ldpc.py`, and
  `fsk.py` implement the public frame, callsign packing, LDPC wrapper, CPFSK
  modulation, acquisition, tracking, demodulation, and CRC validation.
- **Hermes-Lite 2 transmit:** `hpsdr.py` builds OpenHPSDR Protocol 1 packets,
  `hl2.py` performs the finite UDP radio session, and `transmit.py` builds a
  dry-run or explicitly armed test transmission.
- **RTL-SDR receive:** `rtl.py` wraps `librtlsdr`, `channelize.py` filters and
  decimates IQ, `listen.py` captures and decodes bursts, and `meter.py` shows
  channel levels without decoding.
- **Transmit policy:** `airtext.py` rejects invalid, overlong, or apparently
  encoded payloads before they can be placed on amateur radio. This is a
  deterministic guard, not a substitute for the control operator.
- **Field and operations tooling:** `txcontrol.py` supplies the localhost
  phone-oriented test controller; `fieldreport.py` queues receiver reports;
  `gpslog.py` records range-test fixes; `prop.py` checks external propagation
  indicators; and `benchmark.py` runs loopback or live modem trials.
- `python -m hfbridge` starts the listener because `__main__.py` delegates to
  `hfbridge.listen`.

### Optional native acceleration: `native/hfbridge-acq/`

The Rust/PyO3 extension accelerates acquisition search and LDPC decoding. It
is optional; Python fallbacks remain available. `scripts/build-acq.sh` builds
and installs the local extension into `src/hfbridge/`.

### Supporting material

- `tests/` contains unit and signal-processing tests for the frame, modem,
  radio codecs, RNS/LXMF mapping, field reports, and controllers.
- `deploy/` contains example systemd services for unattended ingress and GPS
  logging. Treat usernames, paths, URLs, and device names there as deployment
  templates.
- `scripts/fetch-third-party.sh` fetches/builds the external RTL-SDR,
  pyrtlsdr, Quisk, and Hermes-Lite 2 sources used by bench setups.
- `requirements-radio.txt` lists the Python radio, Reticulum, LXMF, and numeric
  dependencies. This repository currently uses `PYTHONPATH=src` rather than a
  packaged installation.
- `README.md` is the product overview; `docs/end-to-end.md` is the Crosstalk
  and CLI operator runbook; `docs/fcc.md` is the public monitoring
  specification;
  `docs/pi5-tailscale-field-test.md` is the phone-controlled Pi 5 range-test
  guide;
  `docs/over-the-air.md` documents modem internals;
  `docs/future-enhancements.md` tracks proposed work; and `LICENSE` contains
  the MIT license.
- Raw dated experiment directories are local evidence, not runtime
  dependencies, and belong under the ignored `.private/` archive.

## Generated and private local data

The following are runtime or field artifacts, not source code:

- `rns-instances/` contains generated Reticulum identities, ratchets, caches,
  and local configuration.
- `.hf-report-token` is a local authentication secret.
- `.private/` holds recoverable local experiment archives that must not be
  published.
- GPS CSVs, field-report JSONL, mobile-test logs, IQ/audio captures, failed
  burst captures, compiled native extensions, native build output,
  `third_party/`, virtual environments, and Python caches are generated data.

Keep those paths ignored. Never commit identity files, ratchets, tokens,
precise GPS tracks, raw field telemetry, or private message captures. When
adding a new experiment, separate publishable summaries/manifests from raw
location and operational data before staging files.

## Development checks

Use the repository virtual environment when present:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Radio commands are dry-run unless the code requires all explicit arming
conditions. Preserve that invariant: opening a module, constructing a plan, or
running ordinary tests must never key a transmitter. Do not weaken the
`--arm-tx`, radio-address, or filter-confirmation gates.
