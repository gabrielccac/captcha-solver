import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inss.mcp_server import get_inss_payments


class MCPTests(unittest.TestCase):
    def test_pdf_survives_temporary_workflow_cleanup(self):
        def fake_run(command, **kwargs):
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "carta-concessao-1234567890.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
            (out / "extratos-12345678901.json").write_text(json.dumps({
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
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(result["token_validity"]["session_remaining_seconds"], 3500)

    def test_payment_tool_uses_per_call_password_and_returns_only_summary(self):
        def fake_run(command, **kwargs):
            self.assertNotIn("dynamic-secret", command)
            self.assertEqual(kwargs["env"]["GOVBR_PASSWORD"], "dynamic-secret")
            out = Path(command[command.index("--out") + 1])
            out.mkdir()
            (out / "extratos-12345678901.json").write_text(json.dumps({
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
        failure = {"fatal": "captcha unsolved", "rounds": [{
            "solve_error": "403 Client Error: Forbidden for url: https://openrouter.ai"}]}
        with patch("inss.mcp_server.subprocess.run", return_value=SimpleNamespace(
                returncode=2, stdout=json.dumps(failure), stderr="")):
            with self.assertRaisesRegex(RuntimeError, "OpenRouter rejected inference"):
                get_inss_payments("12345678901", "dynamic-secret")


if __name__ == "__main__":
    unittest.main()
