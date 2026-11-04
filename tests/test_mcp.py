import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inss.mcp_server import get_inss_payments


class MCPTests(unittest.TestCase):
    def test_known_login_failures_return_structured_evidence_without_payments(self):
        for code in ("invalid_credentials", "two_factor_required"):
            failure = {"code": code, "success": False}
            with self.subTest(code=code), patch("inss.mcp_server.subprocess.run",
                    return_value=SimpleNamespace(returncode=3, stdout=json.dumps(failure))):
                result = get_inss_payments("12345678901", "secret")
            self.assertEqual(result["error_code"], code)
            self.assertNotIn("payments", result)
            self.assertNotIn("secret", json.dumps(result))

    def test_payments_keep_benefit_numbers_for_multiple_benefits(self):
        def fake_run(command, **kwargs):
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "query.json").write_text(json.dumps({
                "userinfo_status": 200, "windows": [],
                "creditos": [{"nb": 1234567890, "valorLiquido": 100},
                             {"nb": "9876543210", "valorLiquido": 200}],
                "benefits": [{"benefit_number": "1234567890"}, {"benefit_number": "9876543210"}],
            }))
            return SimpleNamespace(returncode=0)
        with patch("inss.mcp_server.subprocess.run", side_effect=fake_run):
            result = get_inss_payments("12345678901", "secret")
        self.assertEqual([p["benefit_number"] for p in result["payments"]],
                         [b["benefit_number"] for b in result["benefits"]])
        self.assertEqual(result["payment_count"], 2)

    def test_selected_medical_pdf_survives_cleanup_and_list_is_returned(self):
        def fake_run(command, **kwargs):
            self.assertEqual(command[command.index("--medical-report-nb") + 1], "1234567890")
            self.assertNotIn("dynamic-secret", command)
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "laudo-medico-1234567890.pdf").write_bytes(b"%PDF-1.5\n%%EOF")
            (out / "query.json").write_text(json.dumps({
                "userinfo_status": 200, "creditos": [], "windows": [],
                "medical_reports": [{"benefit_number": "1234567890", "availability": "not_checked"}],
                "medical_report": {"benefit_number": "1234567890", "http_status": 200,
                                   "file": "laudo-medico-1234567890.pdf"},
            }))
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as root, \
             patch("inss.mcp_server.DOCUMENTS_DIR", Path(root)), \
             patch("inss.mcp_server.subprocess.run", side_effect=fake_run):
            result = get_inss_payments("12345678901", "dynamic-secret", "1234567890")
            report = result["medical_report"]
            self.assertEqual(result["medical_reports"][0]["benefit_number"], "1234567890")
            self.assertTrue(Path(report["file_path"]).is_file())
            if os.name == "posix":
                self.assertEqual(Path(report["file_path"]).stat().st_mode & 0o777, 0o600)
            self.assertNotIn("file", report)
            self.assertNotIn("dynamic-secret", json.dumps(result))

    def test_invalid_medical_nb_never_starts_login(self):
        with patch("inss.mcp_server.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "NB"):
                get_inss_payments("12345678901", "secret", "../private")
            run.assert_not_called()

    def test_medical_pdf_path_cannot_escape_output_directory(self):
        def fake_run(command, **kwargs):
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "query.json").write_text(json.dumps({
                "userinfo_status": 200, "creditos": [], "windows": [],
                "medical_report": {"file": "../other-account.pdf"},
            }))
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as root, \
             patch("inss.mcp_server.DOCUMENTS_DIR", Path(root)), \
             patch("inss.mcp_server.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "Invalid document"):
                get_inss_payments("12345678901", "secret", "1234567890")

    def test_pdf_survives_temporary_workflow_cleanup(self):
        def fake_run(command, **kwargs):
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "carta-concessao-1234567890.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
            (out / "query.json").write_text(json.dumps({
                "userinfo_status": 200, "creditos": [], "windows": [],
                "concession_letters": [{"benefit_number": "1234567890", "http_status": 200,
                                        "file": "carta-concessao-1234567890.pdf"}],
                "token_validity": {"session_remaining_seconds": 3500},
            }))
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as root, \
                patch("inss.mcp_server.DOCUMENTS_DIR", Path(root)), \
                patch("inss.mcp_server.subprocess.run", side_effect=fake_run):
            result = get_inss_payments("12345678901", "secret")
            path = Path(result["concession_letters"][0]["file_path"])
            self.assertTrue(path.is_file())
            if os.name == "posix":
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(result["token_validity"]["session_remaining_seconds"], 3500)

    def test_payment_tool_uses_per_call_password_and_returns_only_summary(self):
        def fake_run(command, **kwargs):
            self.assertNotIn("dynamic-secret", command)
            self.assertNotIn("env", kwargs)
            self.assertEqual(json.loads(kwargs["input"]), {"cpf": "12345678901", "password": "dynamic-secret"})
            self.assertEqual(command[command.index("-m") + 1], "inss.query")
            self.assertNotIn("12345678901", command)
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "query.json").write_text(json.dumps({
                "userinfo_status": 200,
                "creditos": [{"dtCompetenciaCredito": 202610,
                              "dtPgtoCredito": "15/10/2026", "valorLiquido": 100,
                              "tipoStatusCredito": "PAGO", "inCreditoPago": True,
                              "inRetornoBanco": False, "dtInicioValidade": "15/10/2026",
                              "dtFimValidade": "30/11/2026", "private_field": "hidden"}],
                "windows": [{"window": "01-11-2025/14-10-2026", "status": 200}],
                "token": "hidden",
            }))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("inss.mcp_server.subprocess.run", side_effect=fake_run):
            result = get_inss_payments("123.456.789-01", "dynamic-secret")
        self.assertEqual(result["payment_count"], 1)
        self.assertEqual(result["payments"][0]["status"], "PAGO")
        self.assertNotIn("dynamic-secret", json.dumps(result))
        self.assertNotIn("hidden", json.dumps(result))

    def test_invalid_cpf_never_starts_login(self):
        with patch("inss.mcp_server.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "CPF"):
                get_inss_payments("123", "dynamic-secret")
            run.assert_not_called()

    def test_model_rejection_is_not_reported_as_captcha_failure(self):
        failure = {"success": False, "code": "solver_provider_error"}
        with patch("inss.mcp_server.subprocess.run", return_value=SimpleNamespace(
                returncode=2, stdout=json.dumps(failure), stderr="")):
            result = get_inss_payments("12345678901", "dynamic-secret")
            self.assertEqual(result["error_code"], "solver_provider_error")

    def test_selection_flags_reach_worker_and_partial_results_survive(self):
        def fake_run(command, **kwargs):
            self.assertIn("--skip-payments", command)
            self.assertIn("--include-letters", command)
            self.assertIn("--fast-interactions", command)
            self.assertIn("--batch-captcha", command)
            self.assertEqual(command[command.index("--letter-nb") + 1], "1234567890")
            self.assertEqual(command[command.index("--start-date") + 1], "2026-01-01")
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "query.json").write_text(json.dumps({
                "userinfo_status": 200, "creditos": [{"nb": "1234567890"}],
                "windows": [{"window": "fixture", "status": None, "error": "network_error"}],
                "partial": True, "complete": False,
                "errors": [{"component": "payments", "code": "network_error"}],
                "timings": {"login_ms": 10, "extraction_ms": 20, "total_ms": 30},
            }))
            return SimpleNamespace(returncode=0)
        with patch("inss.mcp_server.subprocess.run", side_effect=fake_run):
            result = get_inss_payments("12345678901", "secret", years=2,
                start_date="2026-01-01", end_date="2026-02-01", include_payments=False,
                include_letters=True, letter_benefit_numbers=["1234567890"],
                fast_interactions=True, batch_captcha=True)
        self.assertTrue(result["partial"])
        self.assertEqual(result["payment_count"], 1)
        self.assertEqual(result["windows"][0]["error"], "network_error")
        self.assertIn("mcp_total_ms", result["timings"])

    def test_invalid_selection_never_starts_login(self):
        with patch("inss.mcp_server.subprocess.run") as run:
            for options in ({"years": 0}, {"start_date": "2026-01-01"},
                            {"letter_benefit_numbers": ["../private"]}):
                with self.assertRaises(ValueError):
                    get_inss_payments("12345678901", **options)
            run.assert_not_called()

    def test_document_persistence_failure_keeps_payments_and_reports_partial(self):
        def fake_run(command, **kwargs):
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "fixture.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
            (out / "query.json").write_text(json.dumps({
                "userinfo_status": 200, "creditos": [{"nb": "1234567890"}], "windows": [],
                "complete": True, "partial": False,
                "concession_letters": [{"http_status": 200, "file": "fixture.pdf"}],
            }))
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as root:
            blocked = Path(root) / "not-a-directory"
            blocked.write_text("fixture")
            with patch("inss.mcp_server.DOCUMENTS_DIR", blocked), \
                 patch("inss.mcp_server.subprocess.run", side_effect=fake_run):
                result = get_inss_payments("12345678901", "secret", include_letters=True)
        self.assertEqual(result["payment_count"], 1)
        self.assertTrue(result["partial"])
        self.assertEqual(result["errors"][0]["code"], "local_io_error")
        self.assertNotIn("file_path", result["concession_letters"][0])


if __name__ == "__main__":
    unittest.main()
