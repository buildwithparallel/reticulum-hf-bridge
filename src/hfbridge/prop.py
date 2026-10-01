"""One-shot 10 m skip check: solar indices + PSK Reporter spots.

This does not key a radio. It checks whether a selected grid-to-grid path is
open before you spend a custom CPFSK shout. FT8 SNR is in ~2.5 kHz; our
decoder wants about 8–10 dB in 300 Hz, so treat FT8 +0 dB as the floor
and +5 dB as “try it.”

  PYTHONPATH=src .venv/bin/python -m hfbridge.prop \
    --tx-grid FN31 --rx-grid EM12 --contact YOURCALL
  PYTHONPATH=src .venv/bin/python -m hfbridge.prop \
    --tx-grid FN31 --rx-grid EM12 --contact YOURCALL --hours 6 --callsign YOURCALL
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

SOLAR_URL = "https://www.hamqsl.com/solarxml.php"
PSK_URL = "https://retrieve.pskreporter.info/query"
TEN_M = "28000000-29700000"
# FT8 SNR (2.5 kHz) that maps onto our ~8–10 dB / 300 Hz cliff.
FT8_MAYBE_DB = 0.0
FT8_TRY_DB = 5.0
USER_AGENT = "reticulum-hf-bridge/0.1"


@dataclass(frozen=True)
class Solar:
    updated: str
    flux: str
    k_index: str
    a_index: str
    sunspots: str
    ten_m_day: str
    ten_m_night: str
    eskip_na: str


@dataclass(frozen=True)
class Spot:
    sender: str
    sender_grid: str
    receiver: str
    receiver_grid: str
    snr_db: float | None
    mode: str
    frequency_hz: int
    when: datetime


def _get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_solar(xml: bytes) -> Solar:
    root = ET.fromstring(xml)
    data = root.find("solardata")
    if data is None:
        raise ValueError("solar XML missing solardata")

    def text(tag: str) -> str:
        node = data.find(tag)
        return (node.text or "").strip() if node is not None else ""

    day = night = eskip = ""
    for band in data.findall("calculatedconditions/band"):
        if band.get("name") != "12m-10m":
            continue
        if band.get("time") == "day":
            day = (band.text or "").strip()
        elif band.get("time") == "night":
            night = (band.text or "").strip()
    for row in data.findall("calculatedvhfconditions/phenomenon"):
        if row.get("name") == "E-Skip" and row.get("location") == "north_america":
            eskip = (row.text or "").strip()
    return Solar(
        updated=text("updated"),
        flux=text("solarflux"),
        k_index=text("kindex"),
        a_index=text("aindex"),
        sunspots=text("sunspots"),
        ten_m_day=day,
        ten_m_night=night,
        eskip_na=eskip,
    )


def parse_spots(xml: bytes) -> list[Spot]:
    root = ET.fromstring(xml)
    spots: list[Spot] = []
    for node in root.findall("receptionReport"):
        freq = node.get("frequency") or "0"
        snr_raw = node.get("sNR")
        try:
            snr = float(snr_raw) if snr_raw not in (None, "") else None
        except ValueError:
            snr = None
        try:
            when = datetime.fromtimestamp(int(node.get("flowStartSeconds") or "0"), timezone.utc)
        except ValueError:
            when = datetime.fromtimestamp(0, timezone.utc)
        spots.append(
            Spot(
                sender=node.get("senderCallsign") or "",
                sender_grid=(node.get("senderLocator") or "").upper(),
                receiver=node.get("receiverCallsign") or "",
                receiver_grid=(node.get("receiverLocator") or "").upper(),
                snr_db=snr,
                mode=node.get("mode") or "",
                frequency_hz=int(freq),
                when=when,
            )
        )
    return spots


def grid_matches(locator: str, prefixes: tuple[str, ...]) -> bool:
    loc = locator.upper()
    return any(loc.startswith(prefix) for prefix in prefixes)


def path_spots(
    spots: list[Spot],
    tx_prefixes: tuple[str, ...],
    rx_prefixes: tuple[str, ...],
) -> list[Spot]:
    return [
        spot
        for spot in spots
        if grid_matches(spot.sender_grid, tx_prefixes)
        and grid_matches(spot.receiver_grid, rx_prefixes)
    ]


def from_grid_spots(spots: list[Spot], tx_prefixes: tuple[str, ...]) -> list[Spot]:
    return [spot for spot in spots if grid_matches(spot.sender_grid, tx_prefixes)]


def verdict(path: list[Spot], band_from_home: list[Spot], ten_m_day: str) -> tuple[str, str]:
    """Return (label, why). label is dead / wait / maybe / try."""
    best = max((s.snr_db for s in path if s.snr_db is not None), default=None)
    if path and best is not None and best >= FT8_TRY_DB:
        return "try", f"selected path FT8/FT4 at {best:+.0f} dB — enough margin for CPFSK"
    if path and best is not None and best >= FT8_MAYBE_DB:
        return "maybe", f"selected path open at {best:+.0f} dB FT8 — our cliff is ~+0 to +5"
    if path:
        return "wait", "selected-path spots exist but SNR is below the FT8 +0 dB floor"
    if band_from_home:
        loud = max((s.snr_db for s in band_from_home if s.snr_db is not None), default=None)
        extra = f", best {loud:+.0f} dB" if loud is not None else ""
        return "wait", f"10 m is open from the TX grid{extra}, not on the selected path"
    if ten_m_day.lower() == "poor":
        return "dead", "model says 10 m Poor and PSK has no 10 m spots from the TX grid"
    return "dead", "no 10 m spots from the TX grid — do not spend a skip shout"


def psk_query_url(
    *,
    hours: int,
    sender: str | None,
    as_grid: bool,
    contact: str,
) -> str:
    seconds = -max(1, min(int(hours * 3600), 24 * 3600))
    parts = [
        f"frange={TEN_M}",
        f"flowStartSeconds={seconds}",
        "rronly=1",
        "noactive=1",
        "rptlimit=200",
        f"appcontact={urllib.parse.quote(contact)}",
    ]
    if sender:
        key = "senderCallsign"
        parts.append(f"{key}={urllib.parse.quote(sender)}")
        if as_grid:
            parts.append("modify=grid")
    return PSK_URL + "?" + "&".join(parts)


def fetch_psk(url: str) -> list[Spot]:
    return parse_spots(_get(url))


def _fmt_spot(spot: Spot) -> str:
    snr = f"{spot.snr_db:+.0f} dB" if spot.snr_db is not None else "— dB"
    when = spot.when.strftime("%H:%MZ")
    return (
        f"  {when}  {spot.sender:10} {spot.sender_grid:8} → "
        f"{spot.receiver:10} {spot.receiver_grid:8}  {snr:>7}  "
        f"{spot.mode:4}  {spot.frequency_hz/1e6:.3f}"
    )


def render(
    solar: Solar,
    home: list[Spot],
    path: list[Spot],
    own: list[Spot],
    *,
    hours: int,
    tx_grid: str,
    rx_grid: str,
) -> str:
    label, why = verdict(path, home, solar.ten_m_day)
    lines = [
        f"10 m skip check  ({hours:.0f} h window, UTC {datetime.now(timezone.utc):%Y-%m-%d %H:%M})",
        f"solar  SFI {solar.flux}  K {solar.k_index}  A {solar.a_index}  "
        f"SSN {solar.sunspots}  ({solar.updated})",
        f"12/10 m model  day={solar.ten_m_day or '—'}  night={solar.ten_m_night or '—'}  "
        f"NA Es={solar.eskip_na or '—'}",
        f"verdict  {label.upper()}  —  {why}",
        "",
        f"10 m from TX grid {tx_grid} ({len(home)} spots):",
    ]
    if home:
        lines.extend(_fmt_spot(s) for s in home[:12])
        if len(home) > 12:
            lines.append(f"  … {len(home) - 12} more")
    else:
        lines.append("  none")
    lines += ["", f"10 m {tx_grid} → {rx_grid} ({len(path)} spots):"]
    if path:
        lines.extend(_fmt_spot(s) for s in path[:12])
    else:
        lines.append("  none")
    if own:
        lines += ["", f"spots of your call ({len(own)}):"]
        lines.extend(_fmt_spot(s) for s in own[:8])
    lines += [
        "",
        "FT8 +0 dB ≈ our 8–10 dB / 300 Hz cliff. +5 dB FT8 is the try line.",
        "PSK Reporter map: https://pskreporter.info/pskmap.html?preset&band=10",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=2.0, help="PSK lookback, max 24")
    parser.add_argument(
        "--callsign",
        default="",
        help="also list 10 m spots of this call (second PSK query; skip unless you need it)",
    )
    parser.add_argument("--tx-grid", required=True, help="transmit Maidenhead grid (4 or 6)")
    parser.add_argument("--rx-grid", required=True, help="receive Maidenhead grid (4 or 6)")
    parser.add_argument(
        "--contact",
        required=True,
        help="your callsign or email for PSK Reporter's required appcontact field",
    )
    args = parser.parse_args(argv)

    tx_prefixes = (args.tx_grid[:4].upper(),)
    rx_prefixes = (args.rx_grid[:4].upper(),)
    try:
        solar = parse_solar(_get(SOLAR_URL))
        home = fetch_psk(
            psk_query_url(
                hours=int(args.hours),
                sender=args.tx_grid[:4].upper(),
                as_grid=True,
                contact=args.contact,
            )
        )
        own: list[Spot] = []
        if args.callsign:
            own = fetch_psk(
                psk_query_url(
                    hours=int(args.hours),
                    sender=args.callsign,
                    as_grid=False,
                    contact=args.contact,
                )
            )
    except urllib.error.HTTPError as exc:
        print(f"prop fetch failed: HTTP {exc.code} {exc.reason}", file=sys.stderr)
        if exc.code == 429:
            print("PSK Reporter asked us to back off. Wait a few minutes.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError) as exc:
        print(f"prop fetch failed: {exc}", file=sys.stderr)
        return 1

    home_10 = from_grid_spots(home, tx_prefixes)
    path = path_spots(home, tx_prefixes, rx_prefixes)
    print(
        render(
            solar,
            home_10,
            path,
            own,
            hours=args.hours,
            tx_grid=tx_prefixes[0],
            rx_grid=rx_prefixes[0],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
