# reticulum-hf-bridge

This project lets a licensed amateur relay **Reticulum / LXMF** messages
over the **28 MHz (10 meter) HF** band.

It is a **slow, low-data, long-haul** path: a short plaintext note takes
seconds to tens of seconds on the air, a few hundred characters at most,
in exchange for reach of hundreds or thousands of miles when there is no
internet, no LoRa neighbor, and no satellite.

It is meant to work with **[Crosstalk](https://github.com/buildwithparallel/crosstalk)**
(Parallel's Reticulum client). HF long haul is an optional, off-by-default
conversation path — not a separate messenger.

This repo is two station roles, not one radio:

| Role | What it is | License | Hardware |
| --- | --- | --- | --- |
| **TX station** | Licensed transmit: unwraps Reticulum, keys HF, identifies with a callsign | **Technician or higher** | Hermes-Lite 2 (~5 W) on Ethernet + 10 m antenna |
| **RX station** | Unlicensed receive: hears the shout, injects a new LXMF message | **None** | Inexpensive **RTL-SDR** (a same-room whip is enough to try) |

You can run both roles; they stay separate processes so encrypted Reticulum never goes on the air. Listening does not require a license. Keying the Hermes does. This is not a Part 15 gadget. Verify current rules before you transmit; they change.

## What it does

Reticulum routes well when a path exists. When none does, there is nothing
to route over. This bridge is that missing hop.

```
Crosstalk (or any LXMF client)
        │  ordinary Reticulum  (encrypted)
        ▼
  TX station                   licensed; Hermes-Lite 2
        │
        │  10 m plaintext      anyone with a radio can read this
        ▼
  RX station                   unlicensed; inexpensive RTL-SDR
        │  ordinary Reticulum  (encrypted again)
        ▼
far inbox  (Crosstalk, Columba, NomadNet, …)
```

Neither radio is a Reticulum interface. Encrypted Reticulum packets
**never** go on the air. The station decrypts locally, shouts callsign +
destination + text, and the far side injects a **new** message into
whatever mesh it already has.

A typical note looks like:

```
no internet here. all ok. next check 0900
```

That is the whole payload. There is no second, hidden layer.

## Crosstalk and this repository

The two projects are installed side by side and have different jobs:

| Crosstalk contains | `reticulum-hf-bridge` contains |
| --- | --- |
| Conversations, contacts, LXMF inboxes, and **Send over HF** | The `txbridge` and `ingress` worker programs Crosstalk starts |
| The **Bridge Extensions → OTA Long Haul** setup screens | Hermes-Lite 2 control and RTL-SDR capture |
| Settings, Start/Stop buttons, process status, and bridge logs | The public HF frame, CPFSK modem, LDPC/CRC, and text checks |
| Normal encrypted Reticulum before and after the radio hop | CLI tools, tests, benchmark tools, and the public radio specification |

Crosstalk does not bundle the radio source tree. Clone this repository on each
computer that operates a radio, install its dependencies, then set
**Radio software folder** in Crosstalk to this repository's root—the directory
that contains `src/hfbridge/` and `requirements-radio.txt`. Do not point it at
`Crosstalk.app`, an executable, or `src/hfbridge/` itself.

Choose **Hermes-Lite 2** in Crosstalk for the transmitting station. The current
Hermes worker transmits; it is not the receiver for this bridge. Choose
**RTL-SDR** for a receive-only station. A complete over-the-air path needs a
Hermes transmitter somewhere and an RTL-SDR receiver somewhere, although one
operator may run both roles on separate processes and hardware.

The step-by-step Crosstalk setup is in the
**[OTA Long Haul guide](https://github.com/buildwithparallel/crosstalk/blob/master/docs/ota_long_haul.md)**.
The tested topology and CLI equivalent are in
**[docs/end-to-end.md](docs/end-to-end.md)**.

## The catch

Amateur radio forbids messages encoded to hide their meaning. A Reticulum
packet is encrypted end to end, so it cannot go on HF as-is.

**The bridge terminates encryption.** The RF hop is public radio. The
station operator, and anyone listening, can read it. The far mesh wraps
it in Reticulum again. Sensitive traffic does not belong here. The path
stays opt-in per conversation.

How to listen, what frequency, and a worked frame:
**[docs/fcc.md](docs/fcc.md)**.

## Status

Working proof of concept, not a skip-proven service.

| | |
| --- | --- |
| On the air | 100 baud 2-CPFSK at **28.124 MHz**, ~300 Hz wide, LDPC + CRC |
| Hardware | **TX:** Hermes-Lite 2 on Ethernet. **RX:** inexpensive RTL-SDR |
| Proven | Same-room and across-the-house decode; Crosstalk → HF → Columba; 1/8/30-byte OTA frames at ~11 mW |
| Not proven | Ionospheric skip, neighborhood range with a real 10 m receive antenna |
| Not in yet | HF acknowledgements, retries, files, interactive chat |

Keep messages to a line. A file transfer is the wrong tool.

## What you need

**TX (transmit):** Technician or higher, a callsign in every shout, a
Hermes-Lite 2 on **wired Ethernet**, a 10 m antenna, and Crosstalk (or
the CLI) talking to the TX bridge.

**RX (receive):** an inexpensive RTL-SDR. No license. Wrong antenna is
enough in the same room; range wants a real 10 m receive antenna.

## Try it

Listen (no license, no transmit):

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.listen --quiet
```

For an unattended RTL station, software gain control can climb while the
channel is quiet and retreat immediately from ADC overload without enabling
the RTL hardware AGC:

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.listen \
  --quiet --gain 0 --auto-gain --max-gain 29.7
```

Dry-run a shout (builds the waveform, does not key the radio):

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.transmit \
  --callsign YOURCALL --text "no internet here. all ok. next check 0900"
```

Phone-first field controller (starts idle and binds only to localhost):

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.txcontrol \
  --hl2-ip <HERMES-IP> --callsign YOURCALL
tailscale serve --bg http://127.0.0.1:8765
```

Open the private Tailscale Serve URL on the phone. Each press of **Send**
keys exactly one frame; **Stop** cancels packet generation and sends MOX low.
There is no separate arm control in this one-shot test page. The page
correlates a Pi field report by message ID and shows
decode success/failure, SNR, and GPS/distance when available. The Pi needs
an Internet uplink (for example, the phone hotspot) and Tailscale for live
results; RF reception itself does not depend on that uplink. The complete,
copy-and-paste setup is **[Raspberry Pi 5 + Tailscale field testing](docs/pi5-tailscale-field-test.md)**.

Live Crosstalk → Hermes → RTL → Columba:
**[docs/end-to-end.md](docs/end-to-end.md)**.

## Docs

| File | Who it's for |
| --- | --- |
| **[docs/fcc.md](docs/fcc.md)** | Monitors and Part 97: frequency, tones, frame, worked example |
| **[docs/end-to-end.md](docs/end-to-end.md)** | Operators: topology, hardware, Crosstalk and CLI test |
| **[docs/pi5-tailscale-field-test.md](docs/pi5-tailscale-field-test.md)** | Field testers: phone control, Pi 5 receiver, GPS, and private live reports |
| **[docs/over-the-air.md](docs/over-the-air.md)** | Implementers: modem, Hermes packets, LDPC, receiver |
| **[docs/future-enhancements.md](docs/future-enhancements.md)** | What's next (skip, ARQ, better antennas) |
| **[LICENSE](LICENSE)** | MIT terms for using and redistributing this project |

The station still filters before keying: readable UTF-8 only, length cap,
rate limits, optional allow list. The licensee is responsible for
everything the radio emits.
