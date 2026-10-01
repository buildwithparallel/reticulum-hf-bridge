# Run it: topology and end-to-end test

How to stand up a live shout from **Crosstalk**, over a **Hermes-Lite 2**,
into an **RTL-SDR**, then to an ordinary LXMF dest on the public mesh
(Columba, another Crosstalk, NomadNet, …).

Product intro: **[README.md](../README.md)**. What is on the air:
**[fcc.md](fcc.md)**. Modem internals: **[over-the-air.md](over-the-air.md)**.

Neither radio is a Reticulum interface. Encrypted RNS packets never go on
the air. The licensed station decrypts locally, shouts plaintext 2-CPFSK
on 10 m, and the far listener starts a **new** LXMF message.

## Topology

```
Crosstalk origin (vanilla client)
        │  TCP client → 127.0.0.1:3742
        ▼
Crosstalk HF station
        │  TCP server 127.0.0.1:3742  (origin + txbridge both connect here)
        │
   hfbridge.txbridge   (child process; not a Reticulum interface)
        │  Hermes-Lite 2 over Ethernet
        │
        │  10 m plaintext 2-CPFSK  (anyone can read this)
        ▼
   hfbridge.ingress    (child process; owns the RTL-SDR)
        │  TCP client → public RNS node
        ▼
Public mesh (rns1, rmap, …)
        │
        ▼
Columba / Crosstalk / any LXMF delivery hash
```

Four processes, two islands. Do not interconnect the islands. If origin
and dest share a Reticulum path, delivery skips the radio.

| Role | What it is | Reticulum interfaces |
| --- | --- | --- |
| Origin Crosstalk | Vanilla LXMF client. Compose to the **final** dest, enable the HF toggle. | One TCP client to the HF station (`127.0.0.1:3742`). Nothing else. |
| HF station Crosstalk | Visible station node. Owns the island TCP server. Starts Hermes from **OTA Long Haul**. | One TCP server on `127.0.0.1:3742`. Not on the public mesh. |
| `hfbridge.txbridge` | Translator. Decrypts LXMF, keys the Hermes. | TCP client to the same `:3742` island. |
| `hfbridge.ingress` | Translator. Decodes HF, injects a new LXMF. | One TCP client to a public node (`rns1.buildwithparallel.com:4242` on this bench). Not a local island. |
| Dest | Ordinary LXMF app on the far mesh. | Whatever it already uses (RMAP, `rns1`, AutoInterface, …). |

The Pi (or any box with the dongle) can also run a Crosstalk UI so you can
Start/Stop RTL and watch decode logs. That Crosstalk instance is **not**
on the message path. Ingress talks to the public node itself.

Do not put a local TCP island in front of ingress and expect Crosstalk to
relay onto the backbone. Path requests from that island have not been
reliable; give ingress its own public interface.

## What a message actually does

1. Origin has no Reticulum path to the dest, sees a live `hf-txbridge`
   announce, and offers **Send over HF**.
2. Origin addresses LXMF to the **txbridge** hash. The final dest hash
   rides in the title as `hfdest:<32 hex>`. Content is short plaintext
   (200 bytes max, text only).
3. Txbridge decrypts (it is the addressed recipient), filters, and keys
   the Hermes with callsign + dest hash + body.
4. Ingress decodes, CRC/LDPC check, then `DIRECT` LXMF to that dest hash
   from the ingress identity. Dest sees ingress as the sender and
   `hfvia:CALLSIGN` in the title.
5. There is no HF ACK. Origin “delivered” means the local radio accepted
   the LXMF hop. Ingress `forwarded=1` means it handed the message to
   LXMF, not that the phone showed it. Confirmation is the dest app.

Use the dest’s **LXMF delivery** hash, not its identity hash.

## Hardware

- US Technician or higher to transmit. Listen-only needs no license.
- Hermes-Lite 2 on **wired Ethernet**, not Wi-Fi. Wi-Fi looks fine (ping,
  discovery, even MOX) then smears the FSK. On this bench Wi-Fi was ~12 ms
  and USB Ethernet ~0.7 ms. The computer that runs `txbridge` must be on
  that same LAN by Ethernet.
- Discover the IP; it is not fixed:

  ```bash
  PYTHONPATH=src .venv/bin/python -m hfbridge.transmit --discover
  ```

  Crosstalk’s Hermes page has the same lookup (**Find radio**). After the
  address changes, Stop and Start Hermes so `txbridge` uses the new IP.
- Default shout is 28.124 MHz (stay in 28.120–28.189 MHz).
- One 10 m antenna on the Hermes. The RTL can be a UHF mag-mount for a
  same-room / across-house test. Two 10 m antennas at one station will
  overload the dongle.
- Do not run `hfbridge.listen` and `hfbridge.ingress` on the same dongle.
  Ingress owns the RTL.
- `[R82XX] PLL not locked!` on start has been harmless.

### Power

Two controls in series. Estimated watts ≈
`5 × (drive/255)² × amplitude²`. Drive **0** is silent, not “low.”
Quisk’s default drive is 127. CLI defaults (drive 127, amplitude 0.02)
are about **0.5 mW**. Crosstalk’s 100% slider is about **5 W**.

When nothing decodes, send a 5 s test tone (offset 1 kHz so it is not on
the receiver’s DC bin) and watch the listener’s dB-over-floor line:

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.transmit \
  --callsign YOURCALL --tone 5 \
  --hl2-ip 169.254.x.x --filter-confirmed --arm-tx
```

## End-to-end test (Crosstalk → Columba)

You need: this repo (and a copy on the RTL box), Crosstalk, a Hermes,
an RTL-SDR, a licensed callsign, and a dest that can announce on the
same public node ingress will use.

### 1. HF station (Hermes box)

Run a Crosstalk instance that will be the station. In **Network
Interfaces** add a **TCP Server**:

- listen `127.0.0.1`
- port `3742`
- enabled

Leave transport on. Do **not** add `rns1` / RMAP here.

Open **OTA Long Haul**:

- repo path pointing at this tree
- your callsign
- Hermes IP (Discover)
- frequency 28124000
- a modest power percent for a first shout
- optional allow list: origin’s LXMF hash, if you enable it
- **Arm TX**
- Start **Hermes**

The worker must print `mode=ARMED`. The island should show two TCP
clients once origin is up (origin + txbridge).

### 2. Origin (vanilla client)

A **second** Crosstalk instance (different storage dir / port). One
interface only: **TCP Client** to `127.0.0.1:3742`. No public backbone.

Wait until it hears `hf-txbridge`. Compose a conversation to the dest’s
**delivery** hash. When there is no mesh path, enable **Send over HF**,
type a short ASCII message, send.

Do not also add `:3743` or a public node on this instance.

### 3. RTL ingress (dongle box)

Copy this repo. `rns-instances/ingress/config` should look like:

```
[[Public Backbone]]
  type = TCPClientInterface
  interface_enabled = True
  target_host = rns1.buildwithparallel.com
  target_port = 4242
```

No local island. `enable_transport = No`.

From Crosstalk on that box (or CLI), start **RTL**. Confirm:

- tuner found, listening 28.124 MHz
- a TCP session to port 4242 on the public node
- the dest can be pathed from that same public node

CLI equivalent:

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.ingress \
  --config rns-instances/ingress --quiet
```

Toggling Crosstalk interfaces restarts the backend and kills the ingress
child. Start RTL **after** the interfaces are stable.

### 4. Dest

Open Columba (or the dest Crosstalk). Let it announce on the same mesh
ingress uses. Copy the **delivery** address from the app, not the
identity hash.

### 5. Send and watch

1. Origin: short text, HF toggle on, send.
2. Station OTA page: `txbridge-stats … on_air` increments; Hermes keys.
3. RTL OTA page: a burst several seconds long, then
   `USABLE decoded … this can reach Columba` and `heard` / `forwarded`
   increment.
4. Dest: the message appears, sourced from ingress, title `hfvia:…`.

If the radio heard it and the dest did not, the RF hop succeeded and
the mesh hop failed. Check that ingress has its **own** session to the
public node and that a path to the dest exists from that node.

## CLI loop (no Crosstalk)

Same radios, four isolated RNS instances in `rns-instances/`. Origin
must not share a path with dest. Generate the local identities and print their
delivery hashes before starting the four processes:

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.rnssetup
PYTHONPATH=src .venv/bin/python -m hfbridge.hashes
```

Four terminals, dest first:

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.dest --config rns-instances/dest
PYTHONPATH=src .venv/bin/python -m hfbridge.ingress \
  --config rns-instances/ingress --quiet
PYTHONPATH=src .venv/bin/python -m hfbridge.txbridge \
  --config rns-instances/txbridge --callsign YOURCALL \
  --hl2-ip <hl2-ipv4> --filter-confirmed --arm-tx
PYTHONPATH=src .venv/bin/python -m hfbridge.origin \
  --config rns-instances/origin \
  --bridge <txbridge-hash> --to <dest-hash> \
  --text "no internet here. all ok. next check 0900"
```

`txbridge` is dry-run until `--hl2-ip … --filter-confirmed --arm-tx`.
Success is dest printing `DELIVERED`. Dest sees ingress as the source.
Do not wrap hashes in `<>` in the shell.

## Things that look like success and are not

- Ingress `forwarded=1` with an empty dest path table: identity was
  recalled from cache; LXMF never got a route.
- Origin showing delivered: only the hop to txbridge.
- `PLL not locked` on the RTL: ignore unless there is no audio at all.
- Allow list enabled but origin hash missing: txbridge `rejected`.
- Unarmed txbridge: it will receive LXMF and not key the Hermes.
- Origin and dest on one Crosstalk, or both islands plus `rns1` on
  origin: the message never needs the radio.
