import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from inss.session import concession_letters, token_validity


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class LetterTests(unittest.TestCase):
    def test_downloads_each_session_benefit_once_using_both_tokens(self):
        token = jwt(nbs=["1234567890", "1234567890", "9876543210"])
        pdf = b"%PDF-1.4\nexample\n%%EOF"
        def get(url, **kwargs):
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer auth")
            self.assertEqual(kwargs["headers"]["mitoken"], token)
            self.assertFalse(kwargs["allow_redirects"])
            return Mock(status_code=200, content=pdf)
        with tempfile.TemporaryDirectory() as out, patch("inss.session.requests.get", side_effect=get) as request:
            letters = concession_letters("auth", token, out)
            self.assertEqual(request.call_count, 2)
            for letter in letters:
                path = Path(out) / letter["file"]
                self.assertEqual(path.read_bytes(), pdf)
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_and_non_pdf_responses_are_reported_without_files(self):
        for response in (Mock(status_code=404), Mock(status_code=200, content=b'{"error":"unavailable"}'),
                         requests.Timeout("private request details")):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as out:
                with patch("inss.session.requests.get", side_effect=lambda *a, **k: response) as request:
                    if isinstance(response, Exception):
                        request.side_effect = response
                    letter = concession_letters("auth", jwt(nbs=["1234567890"]), out)[0]
                self.assertIsNone(letter["file"])
                self.assertIn("error", letter)
                self.assertNotIn("private", json.dumps(letter))
                self.assertEqual(list(Path(out).iterdir()), [])

    def test_rejects_path_in_benefit_number(self):
        with tempfile.TemporaryDirectory() as out, patch("inss.session.requests.get") as request:
            with self.assertRaises(ValueError):
                concession_letters("auth", jwt(nbs=["../other-account"]), out)
            request.assert_not_called()

    def test_expiry_uses_shortest_token_and_does_not_expose_claims(self):
        result = token_validity(jwt(iat=1000, exp=4600, sub="private"),
                                jwt(iat=1000, exp=87400, pessoa="private"), now=1100)
        self.assertEqual(result["authorization"]["declared_lifetime_seconds"], 3600)
        self.assertEqual(result["mi_token"]["declared_lifetime_seconds"], 86400)
        self.assertEqual(result["session_remaining_seconds"], 3500)
        self.assertNotIn("private", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
