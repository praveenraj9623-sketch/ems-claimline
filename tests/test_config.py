import tempfile
import unittest
from pathlib import Path

from claimline.config import Settings, valid_phone
from claimline.twilio_api import compute_signature, valid_signature


class Config(unittest.TestCase):
    def test_runtime_port_and_explicit_origin_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings.load(root, env={}, ui_port=8502)
            self.assertEqual(settings.ui_origins, ("http://127.0.0.1:8502", "http://localhost:8502"))
            settings = Settings.load(root, env={"STREAMLIT_PORT": "8600"}, ui_port=8502)
            self.assertEqual(settings.ui_origins, ("http://127.0.0.1:8600", "http://localhost:8600"))
            settings = Settings.load(root, env={"UI_ORIGINS": "https://example.com/"}, ui_port=8502)
            self.assertEqual(settings.ui_origins, ("https://example.com",))

    def test_reads_existing_env_names_and_bom(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".env").write_bytes(("\ufeffGROQ_API_KEY=abc\nVOICE_PUBLIC_URL=https://x.ngrok.app/\n"
                                            "CALL_TO_NUMBER=\"+919876543210\"\n# c\nTWILIO_FROM_NUMBER=+1555 # note\n").encode())
            s = Settings.load(Path(d), env={})
        self.assertEqual(s.groq_api_key, "abc")
        self.assertEqual(s.public_base_url, "https://x.ngrok.app")
        self.assertEqual(s.call_to_number, "+919876543210")
        self.assertEqual(s.twilio_from_number, "+1555")
        self.assertIn("TWILIO_AUTH_TOKEN", s.phone_ready)

    def test_phone_validation(self):
        self.assertTrue(valid_phone("+919876543210"))
        self.assertFalse(valid_phone("9876543210"))

    def test_trial_settings_require_a_webhook_token(self):
        with tempfile.TemporaryDirectory() as d:
            settings = Settings.load(Path(d), env={"TWILIO_TRIAL_MODE": "true"})
            self.assertTrue(settings.twilio_trial_mode)
            self.assertIn("TWILIO_TRIAL_WEBHOOK_TOKEN", settings.phone_ready)
            settings = Settings.load(Path(d), env={"TWILIO_TRIAL_MODE": "true",
                                                  "TWILIO_TRIAL_WEBHOOK_TOKEN": "private-token"})
        self.assertEqual(settings.twilio_trial_webhook_token, "private-token")
        self.assertNotIn("TWILIO_TRIAL_WEBHOOK_TOKEN", settings.phone_ready)
        self.assertFalse(Settings().twilio_trial_mode)

    def test_signature_matches_twilio_documented_vector(self):
        # Example from Twilio's security documentation.
        url = "https://mycompany.com/myapp.php?foo=1&bar=2"
        params = {"CallSid": ["CA1234567890ABCDE"], "Caller": ["+14158675310"], "Digits": ["1234"],
                  "From": ["+14158675310"], "To": ["+18005551212"]}
        sig = compute_signature("12345", url, params)
        self.assertEqual(sig, "GvWf1cFY/Q7PnoempGyD5oXAezc=")
        self.assertTrue(valid_signature("12345", url, params, sig))
        self.assertFalse(valid_signature("12345", url, params, "bad"))


if __name__ == "__main__":
    unittest.main()
