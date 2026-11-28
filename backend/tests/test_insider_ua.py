"""The SEC User-Agent takes its contact from config and never from a hard-coded address."""
import os
import types
import unittest

from tests._loader import load

APP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")


def ua_for(email):
    cfg = types.ModuleType("app.config")
    cfg.settings = types.SimpleNamespace(sec_contact_email=email)
    mod = load("insider_checker", {"httpx": types.ModuleType("httpx"), "app.config": cfg})
    return mod._UA


class UserAgentTests(unittest.TestCase):
    def test_uses_the_configured_contact(self):
        self.assertEqual(ua_for("ops@example.org"), "sepa-trader/1.0 (contact: ops@example.org)")

    def test_blank_contact_does_not_leak_or_crash(self):
        for blank in ("", "   ", None):
            ua = ua_for(blank)
            self.assertNotIn("@", ua)
            self.assertTrue(ua.startswith("sepa-trader/1.0"))

    def test_no_personal_address_in_the_source(self):
        src = open(os.path.join(APP_DIR, "insider_checker.py")).read()
        self.assertNotIn("@gmail.com", src)


if __name__ == "__main__":
    unittest.main()
