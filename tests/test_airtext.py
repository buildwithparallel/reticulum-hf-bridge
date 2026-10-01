import base64
import unittest

from hfbridge.airtext import ENCODED_ERROR, LANGUAGE_ERROR, air_text_error, check_air_text
from hfbridge.frame import check_payload


class AirTextTest(unittest.TestCase):
    def test_allows_ordinary_ham_text(self):
        for text in (
            "hello hf",
            "hello from the other sideeeeeeee",
            "QSY 28.124 need help QTH EM78",
            "ayuda por favor",
            "aa" * 16,
            "classic assessment compass",
            "bleeding from the chest, pregnant, need help",
        ):
            self.assertIsNone(air_text_error(text), text)
            check_payload(text.encode("utf-8"))

    def test_rejects_encoded_payloads(self):
        hidden = base64.b64encode(b"this is hidden text 12345").decode("ascii")
        self.assertEqual(air_text_error(hidden), ENCODED_ERROR)
        self.assertEqual(air_text_error("-----BEGIN PGP MESSAGE-----\nww"), ENCODED_ERROR)
        self.assertEqual(air_text_error("a" * 40), ENCODED_ERROR)
        self.assertEqual(air_text_error("\x00secret"), ENCODED_ERROR)
        with self.assertRaises(ValueError):
            check_payload(b"\x00secret")
        with self.assertRaises(ValueError):
            check_payload(b"\xff\xfe not utf-8")

    def test_rejects_vulgarity_without_echoing_it(self):
        self.assertEqual(air_text_error("what the fuck"), LANGUAGE_ERROR)
        self.assertEqual(air_text_error("fuuuck this"), LANGUAGE_ERROR)
        self.assertEqual(air_text_error("f u c k"), LANGUAGE_ERROR)
        self.assertNotIn("fuck", LANGUAGE_ERROR)
        with self.assertRaises(ValueError) as raised:
            check_air_text("shit")
        self.assertEqual(str(raised.exception), LANGUAGE_ERROR)


if __name__ == "__main__":
    unittest.main()
