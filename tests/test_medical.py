import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

from inss.medical import download_medical_report, list_medical_reports
from inss.session import session_benefits


def token(benefits, nbs=None):
    claims = {"nbs": nbs if nbs is not None else [str(b["numero"]) for b in benefits],
              "nbsJson": json.dumps({"listaBeneficios": benefits})}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class MedicalTests(unittest.TestCase):
    def test_general_benefit_list_includes_non_medical_benefits(self):
        mi = token([{"numero": "1234567890", "codigoEspecie": 31},
                    {"numero": "9876543210", "codigoEspecie": 21}])
        self.assertEqual([b["benefit_number"] for b in session_benefits(mi)],
                         ["1234567890", "9876543210"])
        self.assertEqual(len(list_medical_reports(mi)), 1)

    def test_lists_same_benefit_types_as_site_and_keeps_source_dates(self):
        benefit = {"numero": "1234567890", "codigoEspecie": 31, "situacao": "CESSADO",
                   "descricaoEspecie": "Auxílio", "dataInicioBeneficio": "2020-12-05T00:00:00-0300"}
        reports = list_medical_reports(token([
            benefit, benefit, {"numero": "9876543210", "codigoEspecie": 21},
            {"numero": "5555555555", "codigoEspecie": 87, "situacao": "INDEFERIDO"},
        ]))
        self.assertEqual([r["benefit_number"] for r in reports], ["1234567890", "5555555555"])
        self.assertEqual(reports[0]["benefit_start_date"], benefit["dataInicioBeneficio"])
        self.assertEqual(reports[0]["availability"], "not_checked")
        self.assertIsNone(reports[0]["decision_date"])

    def test_invalid_or_cross_session_benefit_list_is_rejected(self):
        for nb, allowed in (("../private", ["../private"]), ("1234567890", ["9876543210"])):
            with self.subTest(nb=nb), self.assertRaises(ValueError):
                list_medical_reports(token([{"numero": nb, "codigoEspecie": 31}], allowed))
        payload = base64.urlsafe_b64encode(json.dumps({"nbsJson": "erro"}).encode()).decode()
        with self.assertRaises(ValueError):
            list_medical_reports(f"header.{payload}.signature")

    def test_downloads_pdf_even_when_endpoint_labels_it_json(self):
        mi = token([{"numero": "1234567890", "codigoEspecie": 31}])
        client = MagicMock()
        client.get.return_value = Mock(status_code=200, content=b"%PDF-1.5\n%%EOF",
                                       headers={"Content-Type": "application/json"})
        headers = {"Authorization": "Bearer auth", "mitoken": mi}
        with tempfile.TemporaryDirectory() as out, \
             patch("inss.medical.api_session", return_value=(client, headers)):
            report = download_medical_report("auth", mi, "1234567890", out)
            path = Path(out) / report["file"]
            self.assertEqual(path.read_bytes(), b"%PDF-1.5\n%%EOF")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(report["http_status"], 200)
            client.get.assert_called_once_with(
                "https://vip-pmeuinss-api.inss.gov.br/apis/laudomedicoservices/pdf/1234567890",
                headers=headers, timeout=30, allow_redirects=False)

    def test_unavailable_non_pdf_redirect_and_timeout_preserve_error_without_file(self):
        mi = token([{"numero": "1234567890", "codigoEspecie": 31}])
        for response in (Mock(status_code=404), Mock(status_code=500), Mock(status_code=302),
                         Mock(status_code=200, content=b'{"error":"unavailable"}'),
                         requests.Timeout("private request details")):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as out:
                client = MagicMock()
                if isinstance(response, Exception):
                    client.get.side_effect = response
                else:
                    client.get.return_value = response
                with patch("inss.medical.api_session", return_value=(client, {})):
                    result = download_medical_report("auth", mi, "1234567890", out)
                self.assertIsNone(result["file"])
                self.assertIn("error", result)
                self.assertNotIn("private", json.dumps(result))
                self.assertEqual(list(Path(out).iterdir()), [])

    def test_unlisted_or_ineligible_nb_never_makes_request(self):
        mi = token([{"numero": "1234567890", "codigoEspecie": 21}])
        with patch("inss.medical.api_session") as connect:
            for nb in ("1234567890", "9876543210", "../private"):
                with self.subTest(nb=nb), self.assertRaises(ValueError):
                    download_medical_report("auth", mi, nb, "unused")
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
