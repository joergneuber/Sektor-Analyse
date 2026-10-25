import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import send_mail
import upload_to_drive


class FakeSMTP:
    last_message = None
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def starttls(self): pass
    def login(self, *args): pass
    def send_message(self, msg): FakeSMTP.last_message = msg


class GeminiStatusDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.report = Path(self.tmp.name) / "Trading_Radar(2099-01-01).txt"
        self.env = {
            "SMTP_HOST": "smtp.invalid", "SMTP_PORT": "587", "SMTP_USER": "sender@example.invalid",
            "SMTP_PASSWORD": "test", "MAIL_EMPFAENGER": "receiver@example.invalid",
            "TRADING_RADAR_STATUS": "SUCCESS", "TRADING_RADAR_REPORT_CHECK_OUTCOME": "success",
            "TRADING_RADAR_DATEI": str(self.report),
        }

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_success_final_email(self):
        self.report.write_text("TRADING_RADAR_FACHLICH_FINAL=FINAL\n", encoding="utf-8-sig")
        with patch.dict(os.environ, self.env, clear=False), patch.object(send_mail.smtplib, "SMTP", FakeSMTP):
            send_mail.main()
        self.assertIn("final", FakeSMTP.last_message["Subject"].lower())
        self.assertNotIn("NICHT_FINAL", FakeSMTP.last_message.get_payload(0).get_content())

    def test_nicht_final_report_is_sent_with_explicit_warning(self):
        self.report.write_text("TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n", encoding="utf-8-sig")
        with patch.dict(os.environ, {**self.env, "TRADING_RADAR_FACHLICH_FINAL": "NICHT_FINAL", "TRADING_RADAR_REPORT_CHECK_OUTCOME": "failure"}, clear=False), patch.object(send_mail.smtplib, "SMTP", FakeSMTP):
            send_mail.main()
        self.assertIn("NICHT_FINAL", FakeSMTP.last_message["Subject"])
        body = FakeSMTP.last_message.get_payload(0).get_content()
        self.assertIn("NICHT ZUR HANDELSFREIGABE", body)
        self.assertIn("Google-Drive-Upload bleibt gesperrt", body)

    def test_nicht_final_email_is_blocked_when_report_check_status_is_empty(self):
        self.report.write_text("TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n", encoding="utf-8-sig")
        env = {
            **self.env,
            "TRADING_RADAR_FACHLICH_FINAL": "NICHT_FINAL",
            "TRADING_RADAR_REPORT_CHECK_OUTCOME": "",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(send_mail.smtplib, "SMTP", FakeSMTP):
            with self.assertRaisesRegex(ValueError, "fehlender oder unbekannter Report-Check-Status"):
                send_mail.main()

    def test_nicht_final_email_is_blocked_when_report_check_status_is_unknown(self):
        self.report.write_text("TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n", encoding="utf-8-sig")
        env = {
            **self.env,
            "TRADING_RADAR_FACHLICH_FINAL": "NICHT_FINAL",
            "TRADING_RADAR_REPORT_CHECK_OUTCOME": "unexpected",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(send_mail.smtplib, "SMTP", FakeSMTP):
            with self.assertRaisesRegex(ValueError, "fehlender oder unbekannter Report-Check-Status"):
                send_mail.main()

    def test_technical_failure_email_path_is_blocked(self):
        self.report.write_text("TRADING_RADAR_FACHLICH_FINAL=FINAL\n", encoding="utf-8")
        with patch.dict(os.environ, {**self.env, "TRADING_RADAR_STATUS": "TECHNISCHER_FALLBACK"}, clear=False), patch.object(send_mail.smtplib, "SMTP", FakeSMTP):
            with self.assertRaisesRegex(ValueError, "TRADING_RADAR_STATUS"):
                send_mail.main()

    def test_upload_accepts_only_exact_verified_current_report(self):
        self.report.write_text("TRADING_RADAR_FACHLICH_FINAL=NICHT_FINAL\n", encoding="utf-8")
        self.assertIsNone(
            upload_to_drive.validierter_trading_radar_report("SUCCESS", "NICHT_FINAL", str(self.report), "success")
        )
        self.assertIsNone(upload_to_drive.validierter_trading_radar_report("TECHNISCHER_FALLBACK", "FINAL", str(self.report), "success"))
        self.assertIsNone(upload_to_drive.validierter_trading_radar_report("SUCCESS", "FINAL", str(self.report), "failure"))
        self.assertIsNone(upload_to_drive.validierter_trading_radar_report("SUCCESS", "FINAL", str(self.report)+".old", "success"))


if __name__ == "__main__":
    unittest.main()
