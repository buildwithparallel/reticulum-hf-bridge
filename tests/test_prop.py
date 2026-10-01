import unittest

from hfbridge.prop import (
    Spot,
    parse_solar,
    parse_spots,
    path_spots,
    from_grid_spots,
    verdict,
)
from datetime import datetime, timezone


SOLAR_XML = b"""<?xml version="1.0"?>
<solar>
  <solardata>
    <updated>03 Sep 2026 2124 GMT</updated>
    <solarflux>102</solarflux>
    <aindex>6</aindex>
    <kindex>1</kindex>
    <sunspots>36</sunspots>
    <calculatedconditions>
      <band name="12m-10m" time="day">Poor</band>
      <band name="12m-10m" time="night">Poor</band>
    </calculatedconditions>
    <calculatedvhfconditions>
      <phenomenon name="E-Skip" location="north_america">Band Closed</phenomenon>
    </calculatedvhfconditions>
  </solardata>
</solar>
"""

PSK_XML = b"""<?xml version="1.0"?>
<receptionReports>
  <receptionReport receiverCallsign="W5ABC" receiverLocator="EM12aa"
    senderCallsign="W1XYZ" senderLocator="FN31tg" frequency="28074400"
    flowStartSeconds="1788470000" mode="FT8" sNR="7" />
  <receptionReport receiverCallsign="N0FAR" receiverLocator="EN34"
    senderCallsign="W1XYZ" senderLocator="FN31" frequency="28074400"
    flowStartSeconds="1788470001" mode="FT8" sNR="-12" />
</receptionReports>
"""


def _spot(snr, rx_grid="EM12aa", tx_grid="FN31"):
    return Spot(
        sender="W4",
        sender_grid=tx_grid,
        receiver="K7",
        receiver_grid=rx_grid,
        snr_db=snr,
        mode="FT8",
        frequency_hz=28_074_400,
        when=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )


class PropParseTest(unittest.TestCase):
    def test_solar_reads_10m_and_eskip(self):
        solar = parse_solar(SOLAR_XML)
        self.assertEqual(solar.flux, "102")
        self.assertEqual(solar.ten_m_day, "Poor")
        self.assertEqual(solar.eskip_na, "Band Closed")

    def test_spots_and_path_filter(self):
        spots = parse_spots(PSK_XML)
        self.assertEqual(len(spots), 2)
        path = path_spots(spots, ("FN31",), ("EM12", "EM13"))
        self.assertEqual(len(path), 1)
        self.assertEqual(path[0].snr_db, 7.0)
        home = from_grid_spots(spots, ("FN31",))
        self.assertEqual(len(home), 2)


class PropVerdictTest(unittest.TestCase):
    def test_try_when_selected_path_is_loud(self):
        label, _ = verdict([_spot(7)], [_spot(7)], "Fair")
        self.assertEqual(label, "try")

    def test_maybe_at_the_ft8_zero_floor(self):
        label, _ = verdict([_spot(1)], [_spot(1)], "Fair")
        self.assertEqual(label, "maybe")

    def test_wait_when_tx_grid_is_open_but_selected_path_is_not(self):
        label, why = verdict([], [_spot(-5, rx_grid="EN34")], "Fair")
        self.assertEqual(label, "wait")
        self.assertIn("not on the selected path", why)

    def test_dead_when_model_is_poor_and_psk_is_empty(self):
        label, _ = verdict([], [], "Poor")
        self.assertEqual(label, "dead")


if __name__ == "__main__":
    unittest.main()
