import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dataprev.mcp_server import get_cadunico_documents

RECEIPT = {"success": True, "document": "all", "errors": [], "total_ms": 83000,
           "credential": {"seconds_left": 900, "subject": "***.***.***-**"},
           "credenciamento": {"elapsed_ms": 80000, "sso_reused": False, "consent_clicked": True,
                              "identity_status": 200,
                              "phase_ms": {"captcha": 35963, "password": 7903}},
           "documents": {
               "comprovante": {"file": "comprovante.pdf", "tamanho": "808164",
                               "qr_code": True, "validacao_pdf": True},
               "consulta_completa": {"file": "consulta-completa.pdf", "tamanho": "266662",
                                     "qr_code": False, "validacao_pdf": False}}}


def fake_flow(receipt=None, files=("comprovante.pdf", "consulta-completa.pdf")):
    payload = RECEIPT if receipt is None else receipt

    def run(command, **kwargs):
        out = Path(command[command.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            (out / name).write_bytes(b"%PDF-1.5\n%%EOF\n")
        (out / "receipt.json").write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0 if payload.get("success") else 3, stdout="{}")
    return run


class CadunicoMCPTests(unittest.TestCase):
    def documents_dir(self):
        root = Path(tempfile.mkdtemp(prefix="mcp-documentos-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def test_invalid_cpf_is_refused_before_any_browser_work(self):
        with patch("dataprev.mcp_server.subprocess.run") as run:
            for cpf in ("123", "abcdefghijk", "123456789012"):
                with self.subTest(cpf=cpf), self.assertRaises(ValueError):
                    get_cadunico_documents(cpf, "secret")
        run.assert_not_called()

    def test_unknown_documento_is_refused(self):
        with self.assertRaises(ValueError):
            get_cadunico_documents("12345678901", "secret", documento="tudo")

    def test_credentials_travel_over_stdin_and_never_in_the_command(self):
        seen = {}

        def run(command, **kwargs):
            seen["command"] = command
            seen["input"] = kwargs["input"]
            return fake_flow()(command, **kwargs)

        with tempfile.TemporaryDirectory() as root, \
             patch("dataprev.mcp_server.ENV_FILE", str(Path(root) / "ausente.env")), \
             patch("dataprev.mcp_server.DOCUMENTS_DIR", Path(root)), \
             patch("dataprev.mcp_server.subprocess.run", side_effect=run):
            get_cadunico_documents("123.456.789-01", "segredo-da-senha", "sk-or-secreta")
        self.assertNotIn("--env-file", seen["command"])
        self.assertNotIn("segredo-da-senha", " ".join(seen["command"]))
        self.assertNotIn("sk-or-secreta", " ".join(seen["command"]))
        self.assertNotIn("12345678901", " ".join(seen["command"]))
        self.assertEqual(json.loads(seen["input"]),
                         {"cpf": "12345678901", "password": "segredo-da-senha",
                          "api_key": "sk-or-secreta"})
        self.assertEqual(seen["command"][seen["command"].index("--document") + 1], "all")

    def test_local_env_file_fills_empty_credentials(self):
        seen = {}

        def run(command, **kwargs):
            seen["command"] = command
            seen["input"] = kwargs["input"]
            return fake_flow()(command, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            env_file = Path(root) / ".env"
            env_file.write_text("GOVBR_PASSWORD=guardada\n", encoding="utf-8")
            with patch("dataprev.mcp_server.ENV_FILE", str(env_file)), \
                 patch("dataprev.mcp_server.DOCUMENTS_DIR", self.documents_dir()), \
                 patch("dataprev.mcp_server.subprocess.run", side_effect=run):
                result = get_cadunico_documents("12345678901", "")
        self.assertEqual(seen["command"][seen["command"].index("--env-file") + 1], str(env_file))
        self.assertEqual(json.loads(seen["input"]),
                         {"cpf": "12345678901", "password": "", "api_key": ""})
        self.assertEqual(result["cpf_mask"], "123.***.***-01")

    def test_success_persists_private_pdfs_and_masks_the_cpf(self):
        with patch("dataprev.mcp_server.DOCUMENTS_DIR", self.documents_dir()), \
             patch("dataprev.mcp_server.subprocess.run", side_effect=fake_flow()):
            result = get_cadunico_documents("12345678901", "segredo-da-senha")
        self.assertEqual(result["cpf_mask"], "123.***.***-01")
        self.assertEqual(result["token_validity"]["seconds_left"], 900)
        self.assertEqual(result["credenciamento"]["phase_ms"]["captcha"], 35963)
        self.assertTrue(result["complete"])
        for name in ("comprovante", "consulta_completa"):
            document = result["documents"][name]
            path = Path(document["file_path"])
            self.assertTrue(path.is_file())
            self.assertEqual(document["bytes"], path.stat().st_size)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertNotIn("file", document)
        self.assertEqual(result["documents"]["comprovante"]["validacao_pdf"], True)
        self.assertNotIn("segredo-da-senha", json.dumps(result))

    def test_credential_failure_returns_structured_evidence_without_documents(self):
        failure = {"success": False, "code": "identity_mismatch", "stage": "credenciamento",
                   "errors": [], "credenciamento": {"elapsed_ms": 41000, "identity_status": 403}}
        with tempfile.TemporaryDirectory() as root, \
             patch("dataprev.mcp_server.DOCUMENTS_DIR", Path(root)), \
             patch("dataprev.mcp_server.subprocess.run", side_effect=fake_flow(failure, files=())):
            result = get_cadunico_documents("12345678901", "segredo-da-senha")
        self.assertEqual(result["error_code"], "identity_mismatch")
        self.assertFalse(result["complete"])
        self.assertEqual(result["credenciamento"]["identity_status"], 403)
        self.assertNotIn("documents", result)
        self.assertNotIn("segredo-da-senha", json.dumps(result))

    def test_partial_download_keeps_the_document_that_worked(self):
        partial = dict(RECEIPT, success=False,
                       errors=[{"document": "consulta_completa", "code": "upstream_empty_response"}])
        partial["documents"] = {"comprovante": RECEIPT["documents"]["comprovante"]}
        with tempfile.TemporaryDirectory() as root, \
             patch("dataprev.mcp_server.DOCUMENTS_DIR", Path(root)), \
             patch("dataprev.mcp_server.subprocess.run",
                   side_effect=fake_flow(partial, files=("comprovante.pdf",))):
            result = get_cadunico_documents("12345678901", "segredo-da-senha")
        self.assertEqual(list(result["documents"]), ["comprovante"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"][0]["code"], "upstream_empty_response")

    def test_missing_receipt_and_timeout_raise_without_leaking_credentials(self):
        cases = (patch("dataprev.mcp_server.subprocess.run",
                       return_value=SimpleNamespace(returncode=1, stdout="boom")),
                 patch("dataprev.mcp_server.subprocess.run",
                       side_effect=__import__("subprocess").TimeoutExpired("flow", 300)))
        for case in cases:
            with self.subTest(), case, self.assertRaises(RuntimeError) as caught:
                get_cadunico_documents("12345678901", "segredo-da-senha")
            self.assertNotIn("segredo-da-senha", str(caught.exception))

    def test_only_the_requested_document_is_fetched(self):
        def run(command, **kwargs):
            self.assertEqual(command[command.index("--document") + 1], "comprovante")
            return fake_flow(files=("comprovante.pdf",))(command, **kwargs)
        with tempfile.TemporaryDirectory() as root, \
             patch("dataprev.mcp_server.DOCUMENTS_DIR", Path(root)), \
             patch("dataprev.mcp_server.subprocess.run", side_effect=run):
            result = get_cadunico_documents("12345678901", "segredo-da-senha", documento="comprovante")
        self.assertEqual(result["documento"], "comprovante")


if __name__ == "__main__":
    unittest.main()
