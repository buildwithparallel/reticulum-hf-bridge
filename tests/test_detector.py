import unittest

import numpy as np

from hfbridge.listen import (
    _AutoGain,
    _Detector,
    burst_plain_english,
    decode_plain_english,
    looks_like_test_tone,
    snr_quality,
)

CHUNK_SECONDS = 0.109
FLOOR_DBFS = -62.0


def _noise(detector: _Detector, count: int, rng: np.random.Generator) -> list[str]:
    events = []
    for _ in range(count):
        event = detector.update(FLOOR_DBFS + rng.normal(0, 0.8), rng.uniform(-2500, 2500))
        if event:
            events.append(event)
    return events


class DetectorTest(unittest.TestCase):
    def test_quiet_band_does_not_trigger(self):
        detector = _Detector(chunk_seconds=CHUNK_SECONDS)
        self.assertEqual(_noise(detector, 400, np.random.default_rng(0)), [])

    def test_needs_history_before_it_will_call_anything(self):
        detector = _Detector(chunk_seconds=CHUNK_SECONDS)
        self.assertIsNone(detector.update(0.0, 1000.0))

    def test_reports_a_burst_with_its_length_and_tone(self):
        rng = np.random.default_rng(0)
        detector = _Detector(chunk_seconds=CHUNK_SECONDS)
        _noise(detector, 200, rng)

        started = [
            e
            for _ in range(46)
            if (e := detector.update(FLOOR_DBFS + 12.0, 1000.0)) is not None
        ]
        self.assertEqual(len(started), 1)
        self.assertIn("SIGNAL", started[0])
        self.assertIn("+1000 Hz", started[0])

        ended = _noise(detector, 30, rng)
        self.assertEqual(len(ended), 1)
        # 5.0 s of tone plus the 1.2 s hang before "signal gone".
        self.assertIn("6.2 s", ended[0])
        self.assertIn("+1000 Hz", ended[0])

    def test_a_long_burst_cannot_drag_the_floor_up_behind_itself(self):
        rng = np.random.default_rng(1)
        detector = _Detector(chunk_seconds=CHUNK_SECONDS)
        _noise(detector, 200, rng)
        detector.update(FLOOR_DBFS + 12.0, 1000.0)
        # A carrier far longer than any frame must stay flagged throughout.
        for _ in range(150):
            detector.update(FLOOR_DBFS + 12.0, 1000.0)
        self.assertTrue(detector.active)


def _flat(level: float, n: int = 1000) -> np.ndarray:
    return np.full(n, level + level * 1j, dtype=np.complex64)


def _settle(gain: _AutoGain, level: float, chunks: int = 40) -> list[float]:
    changes = []
    for _ in range(chunks):
        moved = gain.update(_flat(level), signal_active=False)
        if moved is not None:
            changes.append(moved)
    return changes


class AutoGainTest(unittest.TestCase):
    def test_force_pins_a_tuner_step(self):
        gain = _AutoGain(42.1)
        self.assertEqual(gain.force(20.7), 14.4)
        self.assertEqual(gain.gain_db, 14.4)

    def test_retreats_from_sustained_clipping(self):
        gain = _AutoGain(29.7)
        rail = np.ones(1000, dtype=np.complex64)
        for _ in range(9):
            self.assertIsNone(gain.update(rail, signal_active=True))
        self.assertEqual(gain.gain_db, 29.7)

    def test_a_loud_frame_does_not_drop_gain(self):
        # 08:26: driveway TX clipped at 14.4 dB and auto-gain walked to 0 dB
        # for the rest of the drive.
        gain = _AutoGain(14.4)
        rail = np.ones(1000, dtype=np.complex64)
        for _ in range(40):
            self.assertIsNone(gain.update(rail, signal_active=True))
        self.assertEqual(gain.gain_db, 14.4)

    def test_impulsive_ignition_spikes_do_not_walk_the_gain_down(self):
        # The drive that produced this test ran the whole way at 0 dB because
        # each ignition spike was read as overload.
        gain = _AutoGain(29.7, chunk_seconds=0.1, settle_seconds=0.3)
        quiet = _flat(0.10, n=1000)
        for _ in range(30):
            spike = quiet.copy()
            spike[:3] = 1.0 + 1.0j
            gain.update(spike, signal_active=False)
        self.assertEqual(gain.gain_db, 29.7)

    def test_a_noisy_car_floor_does_not_make_the_receiver_deaf(self):
        gain = _AutoGain(29.7, chunk_seconds=0.1, settle_seconds=0.3)
        _settle(gain, 0.09, chunks=120)
        self.assertGreaterEqual(gain.gain_db, 14.4)

    def test_only_sustained_overload_may_reach_zero(self):
        gain = _AutoGain(14.4, chunk_seconds=0.1, settle_seconds=0.3)
        rail = np.ones(1000, dtype=np.complex64)
        for _ in range(9):
            gain.update(rail, signal_active=False)
        self.assertEqual(gain.gain_db, 0.0)

    def test_a_driveway_shout_eases_gain_one_step(self):
        gain = _AutoGain(42.1, chunk_seconds=0.1, settle_seconds=0.3)
        gain.note_snr(58.0)
        self.assertEqual(gain.update(_flat(0.10), signal_active=False), 36.4)
        self.assertEqual(gain.gain_db, 36.4)

    def test_one_loud_copy_does_not_keep_walking_down(self):
        # 13:08 this afternoon: snr_backoff stayed set, so 42.1 slammed
        # to 0 dB in one second after a 65 dB driveway copy.
        gain = _AutoGain(42.1, chunk_seconds=0.1, settle_seconds=0.3)
        gain.note_snr(65.0)
        self.assertEqual(gain.update(_flat(0.10), signal_active=False), 36.4)
        for _ in range(20):
            self.assertIsNone(gain.update(_flat(0.10), signal_active=False))
        self.assertEqual(gain.gain_db, 36.4)

    def test_one_weak_copy_climbs_only_one_step(self):
        gain = _AutoGain(
            14.4, chunk_seconds=0.1, settle_seconds=0.3, climb_hold_seconds=0.0
        )
        gain.note_snr(12.0)
        self.assertEqual(gain.update(_flat(0.10), signal_active=False), 29.7)
        for _ in range(10):
            gain.update(_flat(0.10), signal_active=False)
        self.assertEqual(gain.gain_db, 29.7)

    def test_overload_retreats_one_step_then_holds(self):
        gain = _AutoGain(
            42.1, chunk_seconds=0.1, settle_seconds=0.3, climb_hold_seconds=2.0
        )
        rail = np.ones(1000, dtype=np.complex64)
        for _ in range(10):
            gain.update(rail, signal_active=False)
        self.assertEqual(gain.gain_db, 36.4)

    def test_repeated_driveway_shouts_may_reach_zero(self):
        gain = _AutoGain(42.1, chunk_seconds=0.1, settle_seconds=0.3)
        for _ in range(8):
            gain.note_snr(60.0)
            gain.update(_flat(0.10), signal_active=False)
        self.assertEqual(gain.gain_db, 0.0)

    def test_drives_away_from_zero_when_the_band_goes_quiet(self):
        gain = _AutoGain(
            0.0, chunk_seconds=0.1, settle_seconds=0.3, climb_hold_seconds=0.0
        )
        _settle(gain, 0.02, chunks=80)
        self.assertGreater(gain.gain_db, 0.0)

    def test_a_weak_far_copy_asks_for_more_gain(self):
        gain = _AutoGain(
            14.4, chunk_seconds=0.1, settle_seconds=0.3, climb_hold_seconds=0.0
        )
        gain.note_snr(12.0)
        self.assertEqual(gain.update(_flat(0.10), signal_active=False), 29.7)

    def test_high_snr_does_not_move_during_the_burst(self):
        gain = _AutoGain(42.1)
        gain.note_snr(60.0)
        rail = np.ones(1000, dtype=np.complex64)
        for _ in range(20):
            self.assertIsNone(gain.update(rail, signal_active=True))
        self.assertEqual(gain.gain_db, 42.1)

    def test_does_not_pump_between_two_gain_steps(self):
        gain = _AutoGain(29.7, chunk_seconds=0.1, settle_seconds=0.3)
        self.assertEqual(_settle(gain, 0.10, chunks=80), [])
        self.assertEqual(gain.gain_db, 29.7)

    def test_climbs_when_the_floor_is_too_low(self):
        gain = _AutoGain(14.4, chunk_seconds=0.1, settle_seconds=0.3)
        _settle(gain, 0.0005)
        self.assertGreater(gain.gain_db, 14.4)

    def test_climbs_off_a_noisy_but_not_hot_whip(self):
        # 8:41 drive: gain sat at 0 dB because whip RMS ~0.01 was still
        # above the old 0.008 climb threshold, so it never left the deaf step.
        gain = _AutoGain(
            0.0, chunk_seconds=0.1, settle_seconds=0.3, climb_hold_seconds=0.0
        )
        self.assertEqual(gain.gain_db, 0.0)
        _settle(gain, 0.02, chunks=80)
        self.assertGreater(gain.gain_db, 0.0)

    def test_a_quiet_band_does_not_pin_at_the_floor(self):
        gain = _AutoGain(14.4, chunk_seconds=0.1, settle_seconds=0.3)
        _settle(gain, 0.0005, chunks=120)
        self.assertGreaterEqual(gain.gain_db, 29.7)

    def test_backs_off_when_the_floor_is_too_high(self):
        gain = _AutoGain(42.1, chunk_seconds=0.1, settle_seconds=0.3)
        _settle(gain, 0.22)
        self.assertLess(gain.gain_db, 42.1)

    def test_a_floor_already_in_range_is_left_alone(self):
        gain = _AutoGain(29.7, chunk_seconds=0.1, settle_seconds=0.3)
        self.assertEqual(_settle(gain, 0.10), [])
        self.assertEqual(gain.gain_db, 29.7)

    def test_does_not_move_during_a_burst(self):
        gain = _AutoGain(14.4, chunk_seconds=0.1, settle_seconds=0.2)
        for _ in range(20):
            self.assertIsNone(gain.update(_flat(0.0005), signal_active=True))
        self.assertEqual(gain.gain_db, 14.4)

    def test_a_stub_antenna_settles_high_and_a_whip_settles_lower(self):
        # The failure this replaces: one fixed gain cannot serve both a
        # 900 MHz stub (tiny signal) and an efficient 10 m whip (large one).
        stub = _AutoGain(20.7, chunk_seconds=0.1, settle_seconds=0.3)
        _settle(stub, 0.0004, chunks=80)
        whip = _AutoGain(20.7, chunk_seconds=0.1, settle_seconds=0.3)
        _settle(whip, 0.22, chunks=80)
        self.assertGreater(stub.gain_db, whip.gain_db)

    def test_a_frame_does_not_restart_convergence(self):
        gain = _AutoGain(14.4, chunk_seconds=0.1, settle_seconds=0.3)
        gain.update(_flat(0.0005), signal_active=False)
        gain.update(_flat(0.0005), signal_active=True)
        self.assertIsNone(gain.update(_flat(0.0005), signal_active=False))
        self.assertEqual(gain.update(_flat(0.0005), signal_active=False), 29.7)

    def test_a_pin_does_not_move_even_when_the_adc_rails(self):
        gain = _AutoGain(42.1, chunk_seconds=0.1, settle_seconds=0.3)
        rail = np.ones(1000, dtype=np.complex64)
        for _ in range(20):
            self.assertIsNone(gain.update(rail, signal_active=False, move=False))
        self.assertEqual(gain.gain_db, 42.1)
        self.assertTrue(gain.clipping)

    def test_clip_stats_are_measured_every_chunk(self):
        gain = _AutoGain(29.7)
        rail = np.ones(1000, dtype=np.complex64)
        gain.update(rail, signal_active=True)
        self.assertGreaterEqual(gain.last_clip_frac, 0.99)
        self.assertTrue(gain.clipping)


class BurstEnglishTest(unittest.TestCase):
    def test_snr_bands(self):
        self.assertEqual(snr_quality(7), "too-weak")
        self.assertEqual(snr_quality(18), "marginal")
        self.assertEqual(snr_quality(31), "usable")
        self.assertEqual(snr_quality(48), "strong")

    def test_five_second_kilohertz_carrier_is_a_test_tone(self):
        self.assertTrue(looks_like_test_tone(989.0, 5.1))
        self.assertFalse(looks_like_test_tone(-156.0, 1.7))

    def test_tone_english_says_not_a_columba_message(self):
        line = burst_plain_english(18.7, 989.0, 5.1)
        self.assertIn("MARGINAL", line)
        self.assertIn("test tone", line)
        self.assertIn("not a Columba message", line)

    def test_weak_blip_says_ignore(self):
        line = burst_plain_english(6.9, 1107.0, 0.1)
        self.assertIn("TOO-WEAK", line)
        self.assertIn("ignore", line)

    def test_sync_fail_on_a_tone_is_expected(self):
        line = decode_plain_english("sync word not found", tone_hz=989.0, held_s=5.1)
        self.assertIn("test tone", line)

    def test_crc_fail_is_not_usable(self):
        line = decode_plain_english(
            "crc mismatch: got 58cc expected c167", tone_hz=-156.0, held_s=1.7
        )
        self.assertIn("not usable", line)
        self.assertIn("CRC", line)


if __name__ == "__main__":
    unittest.main()
