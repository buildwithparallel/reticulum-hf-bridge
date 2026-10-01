import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from hfbridge.frame import Frame
from hfbridge.listen import (
    BurstDecoder,
    _DecodeJob,
    _Detector,
    _PREROLL_DECODE_SECONDS,
    _SystemdWatchdog,
    _demodulate_with_preroll_trim,
    _iq_for_decode,
    _lock_missed,
)
from hfbridge.fsk import DEFAULT_CHANNEL_RATE


class SystemdWatchdogTest(unittest.TestCase):
    def test_ready_notifies_systemd_when_configured(self):
        environment = {
            "NOTIFY_SOCKET": "/run/systemd/notify-test",
            "WATCHDOG_USEC": "20000000",
        }
        with patch.dict("os.environ", environment), patch(
            "hfbridge.listen.socket.socket"
        ) as socket_type:
            watchdog = _SystemdWatchdog()
            watchdog.ready()

        notify = socket_type.return_value.__enter__.return_value
        notify.sendto.assert_called_once_with(
            b"READY=1\nSTATUS=RTL capture receiving samples",
            "/run/systemd/notify-test",
        )
        self.assertGreater(watchdog.interval, 1.0)


class BurstDecoderTest(unittest.TestCase):
    def test_decode_keeps_hang_and_a_fat_pad_only(self):
        held_s = 8.1
        preroll = int(DEFAULT_CHANNEL_RATE * 5.0)
        held = int(DEFAULT_CHANNEL_RATE * held_s)
        iq = np.arange(preroll + held, dtype=np.float32).astype(np.complex64)
        trimmed = _iq_for_decode(iq, held_s)
        self.assertEqual(len(trimmed), int(DEFAULT_CHANNEL_RATE * (held_s + _PREROLL_DECODE_SECONDS)))
        np.testing.assert_array_equal(trimmed, iq[-len(trimmed) :])
        self.assertEqual(trimmed[-1], iq[-1])

    def test_short_capture_is_not_trimmed(self):
        iq = np.ones(100, dtype=np.complex64)
        np.testing.assert_array_equal(_iq_for_decode(iq, 8.1), iq)

    def test_lock_miss_retries_the_full_preroll(self):
        seen: list[int] = []

        def demod(iq, **_kwargs):
            seen.append(len(iq))
            if len(seen) == 1:
                raise ValueError("full preamble not found")
            return Frame(origin="N0CALL", dest=bytes(16), payload=b"x", msg_id=1)

        preroll = int(DEFAULT_CHANNEL_RATE * 5.0)
        held = int(DEFAULT_CHANNEL_RATE * 8.1)
        iq = np.ones(preroll + held, dtype=np.complex64)
        with patch("hfbridge.listen.demodulate", side_effect=demod):
            frame = _demodulate_with_preroll_trim(iq, 8.1)
        self.assertEqual(frame.payload, b"x")
        self.assertEqual(len(seen), 2)
        self.assertLess(seen[0], seen[1])
        self.assertEqual(seen[1], len(iq))

    def test_crc_fail_does_not_retry_the_full_preroll(self):
        seen: list[int] = []

        def demod(iq, **_kwargs):
            seen.append(len(iq))
            raise ValueError("crc mismatch: got 0000 expected ffff")

        preroll = int(DEFAULT_CHANNEL_RATE * 5.0)
        held = int(DEFAULT_CHANNEL_RATE * 8.1)
        iq = np.ones(preroll + held, dtype=np.complex64)
        with patch("hfbridge.listen.demodulate", side_effect=demod):
            with self.assertRaises(ValueError):
                _demodulate_with_preroll_trim(iq, 8.1)
        self.assertEqual(len(seen), 1)
        self.assertTrue(_lock_missed("sync word not found"))
        self.assertFalse(_lock_missed("crc mismatch"))

    def test_short_energy_excursion_is_not_decoded(self):
        decoded: list[int] = []

        def demod(iq, **_kwargs):
            decoded.append(len(iq))
            return Frame(
                origin="N0CALL",
                dest=bytes(16),
                payload=b"x",
                msg_id=1,
            )

        with TemporaryDirectory() as tmp:
            with (
                patch("hfbridge.listen.demodulate", side_effect=demod),
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
            ):
                decoder = BurstDecoder()
                decoder.submit(
                    np.ones(100, dtype=np.complex64),
                    dump_frame=False,
                    peak_db=20.0,
                    tone_hz=0.0,
                    held_s=0.7,
                )
                decoder.close(timeout=5.0)

        self.assertEqual(decoded, [])

    def test_slow_decode_does_not_block_submit(self):
        decoded: list[int] = []

        def slow_demod(iq, **_kwargs):
            time.sleep(0.15)
            decoded.append(int(np.round(iq[0].real)))
            return Frame(
                origin="N0CALL",
                dest=bytes(16),
                payload=b"x",
                msg_id=int(np.round(iq[0].real)) + 1,
            )

        with TemporaryDirectory() as tmp:
            with (
                patch("hfbridge.listen.demodulate", side_effect=slow_demod),
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
            ):
                decoder = BurstDecoder()
                t0 = time.monotonic()
                for i in range(3):
                    decoder.submit(
                        np.array([i], dtype=np.complex64),
                        dump_frame=False,
                        peak_db=20.0,
                        tone_hz=-137.0,
                        held_s=3.0,
                    )
                submit_s = time.monotonic() - t0
                decoder.close(timeout=5.0)

        self.assertLess(submit_s, 0.1)
        self.assertEqual(decoded, [0, 1, 2])

    def test_a_parked_carrier_cannot_blind_the_detector_for_long(self):
        # A continuous interferer must not hold the burst open indefinitely,
        # or a real frame arriving underneath it never gets its own window.
        detector = _Detector(chunk_seconds=0.109, margin_db=6.0)
        for _ in range(30):
            detector.update(-60.0, 0.0)

        notes = []
        for _ in range(int(round(30.0 / 0.109))):
            note = detector.update(-20.0, -980.0)
            if note:
                notes.append(note)

        rebaselined = [n for n in notes if "re-baselining" in n]
        self.assertTrue(rebaselined)
        held = float(rebaselined[0].split(" after ")[1].split(" s")[0])
        self.assertLessEqual(held, 21.0)

    def test_a_normal_frame_is_not_cut_short_by_rebaselining(self):
        detector = _Detector(chunk_seconds=0.109, margin_db=6.0)
        for _ in range(30):
            detector.update(-60.0, 0.0)

        # A 13 s frame (30 bytes) must survive intact.
        notes = []
        for _ in range(int(round(13.0 / 0.109))):
            note = detector.update(-20.0, -55.0)
            if note:
                notes.append(note)

        self.assertFalse([n for n in notes if "re-baselining" in n])

    def test_one_quiet_chunk_does_not_end_a_frame(self):
        detector = _Detector(chunk_seconds=0.109, margin_db=6.0)
        for _ in range(30):
            detector.update(-60.0, 0.0)
        self.assertIsNotNone(detector.update(-20.0, -55.0))
        self.assertIsNone(detector.update(-58.0, -55.0))
        self.assertTrue(detector.active)
        self.assertIsNone(detector.update(-20.0, -55.0))
        self.assertTrue(detector.active)

    def test_a_real_end_still_closes_after_the_hang(self):
        detector = _Detector(chunk_seconds=0.109, margin_db=6.0)
        for _ in range(30):
            detector.update(-60.0, 0.0)
        self.assertIsNotNone(detector.update(-20.0, -55.0))
        notes = []
        for _ in range(15):
            note = detector.update(-60.0, -55.0)
            if note:
                notes.append(note)
        self.assertEqual(len(notes), 1)
        self.assertIn("signal gone", notes[0])
        self.assertFalse(detector.active)

    def test_weak_below_margin_does_not_raise_the_floor(self):
        detector = _Detector(chunk_seconds=0.109, margin_db=6.0)
        for _ in range(30):
            detector.update(-60.0, 0.0)
        # 4 dB over the floor: not a burst, but not quiet either.
        for _ in range(40):
            self.assertIsNone(detector.update(-56.0, -55.0))
        self.assertAlmostEqual(detector.floor_db or 0.0, -60.0, delta=0.5)
        start = detector.update(-50.0, -55.0)
        self.assertIsNotNone(start)
        self.assertIn("SIGNAL", start or "")

    def test_off_frequency_burst_is_not_decoded(self):
        decoded: list[int] = []

        def demod(iq, **_kwargs):
            decoded.append(len(iq))
            return Frame(origin="N0CALL", dest=bytes(16), payload=b"x", msg_id=1)

        with TemporaryDirectory() as tmp:
            with (
                patch("hfbridge.listen.demodulate", side_effect=demod),
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
            ):
                decoder = BurstDecoder()
                # Car ignition noise picked a tone 944 Hz off centre.
                decoder.submit(
                    np.ones(100, dtype=np.complex64),
                    dump_frame=False,
                    peak_db=7.1,
                    tone_hz=-944.0,
                    held_s=5.8,
                )
                decoder.close(timeout=5.0)

        self.assertEqual(decoded, [])

    def test_on_frequency_burst_is_still_decoded(self):
        decoded: list[int] = []

        def demod(iq, **_kwargs):
            decoded.append(len(iq))
            return Frame(origin="N0CALL", dest=bytes(16), payload=b"x", msg_id=1)

        with TemporaryDirectory() as tmp:
            with (
                patch("hfbridge.listen.demodulate", side_effect=demod),
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
            ):
                decoder = BurstDecoder()
                decoder.submit(
                    np.ones(100, dtype=np.complex64),
                    dump_frame=False,
                    peak_db=40.0,
                    tone_hz=-55.0,
                    held_s=8.1,
                )
                decoder.close(timeout=5.0)
            self.assertEqual(list(Path(tmp).glob("burst-*.npz")), [])

        self.assertEqual(len(decoded), 1)

    def test_failed_decode_still_saves_the_burst(self):
        def demod(_iq, **_kwargs):
            raise ValueError("crc mismatch: got 0000 expected ffff")

        with TemporaryDirectory() as tmp:
            with (
                patch("hfbridge.listen.demodulate", side_effect=demod),
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
            ):
                decoder = BurstDecoder()
                decoder.submit(
                    np.ones(100, dtype=np.complex64),
                    dump_frame=False,
                    peak_db=12.0,
                    tone_hz=-55.0,
                    held_s=8.1,
                )
                decoder.close(timeout=5.0)
            saved = list(Path(tmp).glob("burst-*.npz"))
        self.assertEqual(len(saved), 1)

    def test_a_burst_that_waited_too_long_is_skipped(self):
        decoded: list[int] = []

        def demod(iq, **_kwargs):
            decoded.append(len(iq))
            return Frame(origin="N0CALL", dest=bytes(16), payload=b"x", msg_id=1)

        with TemporaryDirectory() as tmp:
            with (
                patch("hfbridge.listen.demodulate", side_effect=demod),
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
                patch("hfbridge.listen._MAX_JOB_AGE_SECONDS", 0.05),
            ):
                decoder = BurstDecoder()
                decoder._queue.put(
                    _DecodeJob(
                        iq=np.ones(100, dtype=np.complex64),
                        dump_frame=False,
                        peak_db=40.0,
                        tone_hz=-55.0,
                        held_s=8.1,
                        gain_db=None,
                        queued_at=time.monotonic() - 10.0,
                    )
                )
                decoder.close(timeout=5.0)

        self.assertEqual(decoded, [])

    def test_overlong_carrier_is_not_decoded(self):
        decoded: list[int] = []

        def demod(iq, **_kwargs):
            decoded.append(len(iq))
            return Frame(
                origin="N0CALL",
                dest=bytes(16),
                payload=b"x",
                msg_id=1,
            )

        with TemporaryDirectory() as tmp:
            with (
                patch("hfbridge.listen.demodulate", side_effect=demod),
                patch("hfbridge.listen._CAPTURE_DIR", Path(tmp)),
            ):
                decoder = BurstDecoder()
                decoder.submit(
                    np.ones(100, dtype=np.complex64),
                    dump_frame=False,
                    peak_db=26.0,
                    tone_hz=576.0,
                    held_s=60.1,
                )
                decoder.close(timeout=5.0)

        self.assertEqual(decoded, [])
