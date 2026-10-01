# Raspberry Pi 5 + Tailscale field test

This is the simplest way to range-test the radio link from a phone. A
Raspberry Pi 5 carries the RTL-SDR and optional USB GPS at the receive site.
The transmit computer runs a small private web controller for the Hermes-Lite
2. Tailscale carries status and test controls; it does **not** carry the RF
payload.

This test page is separate from Crosstalk. Use it to answer “did the Pi hear
this transmission, at this location and signal level?” Use Crosstalk's **OTA
Long Haul** extension for actual LXMF messaging.

## The three devices

```text
phone browser
    | private HTTPS over Tailscale
    v
TX computer: hfbridge.txcontrol ---- wired Ethernet ---- Hermes-Lite 2
    ^                                                        |
    | receiver reports over Tailscale                        | 10 m RF
    |                                                        v
phone hotspot/Wi-Fi ---- Raspberry Pi 5 ---- RTL-SDR + optional USB GPS
                         hfbridge.ingress
```

The Pi can lose its hotspot without losing the radio test. It continues
receiving and queues reports locally, then sends them when Tailscale reconnects.
The phone shows live results only while the network path is available.

## Before going outside

You need:

- a licensed control operator, callsign, filtered 10 m transmitter, antenna,
  and Hermes-Lite 2 on wired Ethernet;
- the repository and Python dependencies on both computers;
- an RTL-SDR on the Pi, plus a USB GPS if you want mapped distance;
- Tailscale signed into the same tailnet on the phone, TX computer, and Pi;
- a phone hotspot or other Internet connection for the Pi's live reports.

From the repository root on each computer:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-radio.txt
```

Confirm the RTL-SDR works on the Pi before involving Tailscale:

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.listen --self-test
PYTHONPATH=src .venv/bin/python -m hfbridge.listen --quiet --seconds 10
```

If Linux cannot open the dongle or GPS, fix its udev/group permissions first.
Do not run `listen` and `ingress` together; only one process can own the RTL-SDR.

## 1. Create one report token

The token authenticates reports from the Pi to the controller. Create it on
the TX computer:

```bash
openssl rand -hex 32 > ~/.hf-report-token
chmod 600 ~/.hf-report-token
```

Securely copy that exact file to `~/.hf-report-token` on the Pi and keep mode
`600`. Do not commit, paste into an issue, or include it in a test archive.

## 2. Start the transmitter controller

On the computer wired to the Hermes-Lite 2:

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.txcontrol \
  --hl2-ip <HERMES-IP> \
  --callsign <YOUR-CALLSIGN> \
  --report-token-file ~/.hf-report-token
```

Optional flags:

- Add `--tx-lat <LAT> --tx-lon <LON>` to show distance from the transmitter to
  the Pi's latest GPS fix.
- Add `--destination <32-HEX-LXMF-DELIVERY-HASH>` only when you are testing a
  real destination. The default zero destination is fine for radio testing.
- Add `--filter-installed` only when the required 10 m transmit filter is
  permanently installed. Otherwise the page asks for confirmation before
  each transmission.

The controller deliberately accepts only localhost binds. Leave it running,
then publish it privately with Tailscale Serve in another terminal:

```bash
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

`tailscale serve status` prints the private HTTPS URL to open on the phone.
Use **Serve**, not **Funnel**: Funnel would expose the controller to the public
Internet. Current command details are in the
[official Tailscale Serve documentation](https://tailscale.com/docs/reference/tailscale-cli/serve).

Opening the page does not transmit. A press of **Send** produces one frame;
**Stop** cancels generation and takes MOX low. Begin at the lowest useful power.

## 3. Start GPS on the Pi (optional)

Find the device, usually `/dev/ttyACM0` or `/dev/ttyUSB0`, then run:

```bash
mkdir -p ~/hf-logs
PYTHONPATH=src .venv/bin/python -m hfbridge.gpslog \
  --device /dev/ttyACM0 \
  --csv ~/hf-logs/field-gps.csv
```

Wait for fixes to appear in the CSV. GPS coordinates and timestamps are
sensitive field data; keep this file out of Git and public test reports.

## 4. Start the Pi receiver and reporter

Copy the private controller URL from `tailscale serve status`. On the Pi, in a
second terminal, run:

```bash
PYTHONPATH=src .venv/bin/python -m hfbridge.ingress \
  --config rns-instances/ingress \
  --quiet \
  --gain 0 \
  --max-gain 29.7 \
  --margin-db 6 \
  --report-url https://<TX-TAILSCALE-NAME>/api/report \
  --report-token-file ~/.hf-report-token \
  --report-queue ~/hf-logs/field-reports.jsonl \
  --report-gps-csv ~/hf-logs/field-gps.csv
```

Omit `--report-gps-csv` when no GPS is attached. Adaptive RTL gain is enabled
by default; the phone page can also request a gain override. The RNS config is
still required because `ingress` can forward a successfully decoded frame,
although the field dashboard itself only needs the reports.

The controller should change from **Pi offline** to a live receiver status
after the first heartbeat. If it does not, check both machines with
`tailscale status`, verify the report URL, and confirm that the token files
contain the same value.

## 5. Run the test

1. Keep the transmitter stationary and move the Pi, RTL-SDR, receive antenna,
   GPS, and hotspot together.
2. Open the private controller URL on the phone and wait for a current Pi
   heartbeat and GPS fix.
3. Enter a short, non-sensitive test message. Confirm the filter when prompted.
4. Start at low power and press **Send** once.
5. Wait for the matching message ID. The page shows decode/failure, SNR, gain,
   tone level, GPS position, and distance when available.
6. Record antenna, power, terrain, and receiver placement separately; those
   variables matter more than a pile of raw logs.

Every RF payload is public and includes the callsign and message text. Use only
readable, non-sensitive test text and obey the frequency, identification, power,
and control requirements that apply to the operator.

## Make the Pi services persistent

After the commands work interactively, adapt the templates in `deploy/`:

- `hf-gpslog.service`: set `User`, `WorkingDirectory`, GPS device, and log paths;
- `hf-ingress.service`: set those fields plus the private Tailscale report URL,
  token path, RNS config, and receiver settings.

Install the edited units on the Pi:

```bash
sudo cp deploy/hf-gpslog.service /etc/systemd/system/
sudo cp deploy/hf-ingress.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hf-gpslog hf-ingress
```

Inspect them with:

```bash
systemctl status hf-gpslog hf-ingress
journalctl -u hf-gpslog -u hf-ingress -f
```

Do not install the sample units unchanged: their usernames, paths, device, and
Tailscale hostname are placeholders for one bench.

## Stop and clean up

Stop the transmitter controller with `Ctrl-C`; its shutdown path sends MOX low.
Remove the private proxy when it is no longer needed:

```bash
tailscale serve reset
```

The controller JSONL, queued Pi reports, ingress logs, and GPS CSV can contain
message text, precise locations, hostnames, and operational details. They are
ignored runtime data, not publishable repository documentation.
