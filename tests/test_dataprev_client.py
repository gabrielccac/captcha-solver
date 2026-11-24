import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from dataprev.client import (AUDIENCE, CNAS_VERSION, DataprevClient, ServiceError, export, mask,
                             save, session_for)

PDF = b"%PDF-1.5\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
CPF = "12345678901"
NUMERO_FAMILIAR = 1234567890
IDENTIFICADOR = 987654


def jwt(**overrides):
    claims = {"sub": CPF, "aud": AUDIENCE, "iss": "https://sso.acesso.gov.br/",
              "exp": int(time.time()) + 3600, "iat": int(time.time())}
    claims.update(overrides)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"fixture.{payload}.fixture"


def envelope(pdf=PDF, **extra):
    body = {"base64PDF": base64.b64encode(pdf).decode(), "tamanhoDoArquivo": len(pdf)}
    body.update(extra)
    return body


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}
        self.closed = False

    @property
    def content(self):
        return self._content

    def iter_content(self, size):
        for start in range(0, len(self.content), size):
            yield self.content[start:start + size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, routes, headers=None):
        self.routes = routes
        self.headers = {"Authorization": "Bearer " + jwt()} if headers is None else headers
        self.cookies = {}
        self.requests = []
        self.closed = 0

    def close(self):
        self.closed += 1

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=None, stream=None):
        path = url.split("/transacional-api/v1", 1)[1]
        self.requests.append({"path": path, "params": params, "headers": headers or {}})
        response = self.routes.get(path)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(404, b"") if response is None else response


def profile_body(**overrides):
    item = {"cpf": CPF, "numeroFamiliar": NUMERO_FAMILIAR, "identificador": IDENTIFICADOR,
            "tipoPerfil": {"codigo": 1, "nome": "Responsável Familiar"}}
    item.update(overrides)
    return [item]


def client(routes):
    return DataprevClient(jwt(), session=FakeSession(routes), min_interval=0)


class ClientTests(unittest.TestCase):
    def test_credential_comes_from_the_session_header(self):
        session = FakeSession({"/suporte/configuracao/token": FakeResponse(200, int(CPF))})
        built = DataprevClient(session=session)
        self.assertTrue(built.token)
        self.assertEqual(built.identity(), CPF)

    def test_credential_must_come_from_govbr_for_this_client(self):
        for overrides, code in (({"aud": "outro.servico.gov.br"}, "credential_wrong_audience"),
                                ({"iss": "https://exemplo.gov.br/"}, "credential_wrong_issuer")):
            with self.subTest(code=code), self.assertRaises(ServiceError) as error:
                DataprevClient(jwt(**overrides), session=FakeSession({}))
            self.assertEqual(error.exception.code, code)

    def test_missing_credential_is_rejected_before_any_request(self):
        with self.assertRaises(ServiceError) as error:
            DataprevClient(session=FakeSession({}, headers={}))
        self.assertEqual(error.exception.code, "credential_missing")

    def test_declared_validity_comes_from_the_token(self):
        self.assertGreater(client({}).seconds_left(), 3000)
        self.assertAlmostEqual(
            DataprevClient(jwt(exp=int(time.time()) + 600), session=FakeSession({})).seconds_left(),
            600, delta=3)

    def test_identity_is_the_authenticated_cpf(self):
        self.assertEqual(client({"/suporte/configuracao/token": FakeResponse(200, int(CPF))}).identity(), CPF)

    def test_identity_rejects_anything_but_eleven_digits(self):
        for body in ({"cpf": CPF}, "1234567890", 1234, None):
            with self.subTest(body=body), self.assertRaises(ServiceError) as error:
                client({"/suporte/configuracao/token": FakeResponse(200, body)}).identity()
            self.assertEqual(error.exception.code, "identity_unparsed")

    def test_unauthorized_without_body_means_expired_session(self):
        with self.assertRaises(ServiceError) as error:
            client({"/suporte/configuracao/token": FakeResponse(401, b"")}).identity()
        self.assertEqual(error.exception.code, "session_expired")

    def test_unauthorized_with_api_error_means_other_account(self):
        body = {"mensagemErro": {"descricao": "Acesso negado.", "codigo": "401"}}
        with self.assertRaises(ServiceError) as error:
            client({"/suporte/configuracao/token": FakeResponse(401, body)}).identity()
        self.assertEqual(error.exception.code, "not_authorized")

    def test_network_failure_uses_sanitized_code(self):
        with self.assertRaises(ServiceError) as error:
            client({"/suporte/configuracao/token": requests.exceptions.ConnectionError()}).identity()
        self.assertEqual(error.exception.code, "connection_error")

    def test_comprovante_resolves_parameters_from_the_profile(self):
        built = client({
            f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body()),
            f"/pdf/{NUMERO_FAMILIAR}/comprovante-cadastro/{IDENTIFICADOR}/{CPF}": FakeResponse(200, envelope(
                numeroFamiliar=NUMERO_FAMILIAR, chaveSeguranca="fixture-key", municipioUF="FIXTURE/UF",
                urlValidacaoComprovante="https://cadunico.dataprev.gov.br/valida/fixture",
                integrantes=[{"nome": "Fixture"}],
                validacaoComprovante={"codigoValidacao": None, "solicitante": None, "dataEmissao": None,
                                      "bytes": base64.b64encode(PDF).decode()},
                base64QRCode=base64.b64encode(b"png").decode())),
        })
        result = built.comprovante(CPF)
        self.assertEqual(result["document"], "comprovante")
        self.assertEqual(result["pdf"], PDF)
        self.assertEqual(result["size_bytes"], len(PDF))
        self.assertEqual(result["numero_familiar"], str(NUMERO_FAMILIAR))
        self.assertEqual(result["chave_seguranca"], "fixture-key")
        self.assertEqual(result["validacao"]["pdf"], PDF)
        self.assertEqual(result["qr_code"], b"png")
        self.assertIsNone(built.session.requests[-1]["params"])

    def test_comprovante_uses_identity_when_cpf_is_omitted(self):
        built = client({
            "/suporte/configuracao/token": FakeResponse(200, int(CPF)),
            f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body()),
            f"/pdf/{NUMERO_FAMILIAR}/comprovante-cadastro/{IDENTIFICADOR}/{CPF}": FakeResponse(200, envelope()),
        })
        self.assertEqual(built.comprovante()["pdf"], PDF)

    def test_representante_legal_flag_reaches_the_query(self):
        built = client({
            f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body()),
            f"/pdf/{NUMERO_FAMILIAR}/comprovante-cadastro/{IDENTIFICADOR}/{CPF}": FakeResponse(200, envelope()),
        })
        built.comprovante(CPF, representante_legal=True)
        self.assertEqual(built.session.requests[-1]["params"], {"representanteLegal": "true"})

    def test_document_without_pdf_signature_is_rejected(self):
        built = client({
            f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body()),
            f"/pdf/{NUMERO_FAMILIAR}/comprovante-cadastro/{IDENTIFICADOR}/{CPF}": FakeResponse(
                200, envelope(pdf="<html>sessão expirada</html>".encode())),
        })
        with self.assertRaises(ServiceError) as error:
            built.comprovante(CPF)
        self.assertEqual(error.exception.code, "document_not_pdf")

    def test_declared_size_must_match_the_decoded_document(self):
        built = client({
            f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body()),
            f"/pdf/{NUMERO_FAMILIAR}/comprovante-cadastro/{IDENTIFICADOR}/{CPF}": FakeResponse(
                200, envelope(tamanhoDoArquivo=len(PDF) + 9000)),
        })
        with self.assertRaises(ServiceError) as error:
            built.comprovante(CPF)
        self.assertEqual(error.exception.code, "document_size_mismatch")

    def test_ambiguous_profile_is_not_guessed(self):
        twice = profile_body() + profile_body(identificador=IDENTIFICADOR + 1)
        with self.assertRaises(ServiceError) as error:
            client({f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, twice)}).profile(CPF)
        self.assertEqual(error.exception.code, "profile_ambiguous")

    def test_profile_of_another_cpf_is_not_used(self):
        with self.assertRaises(ServiceError) as error:
            client({f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body(cpf="99999999999"))}).profile(CPF)
        self.assertEqual(error.exception.code, "profile_not_found")

    def test_consulta_completa_accepts_declared_size_as_text(self):
        built = client({
            f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body()),
            f"/pdf/{CPF}/{NUMERO_FAMILIAR}/exportar-consulta-completa": FakeResponse(
                200, {"base64PDF": base64.b64encode(PDF).decode(),
                      "tamanhoDoArquivo": str(len(PDF)), "familiaCadastrada": False}),
        })
        result = built.consulta_completa(CPF)
        self.assertEqual(result["document"], "consulta_completa")
        self.assertEqual(result["pdf"], PDF)
        self.assertIs(result["familia_cadastrada"], False)

    def test_missing_export_is_a_document_error_not_a_login_error(self):
        built = client({
            f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body()),
            f"/pdf/{CPF}/{NUMERO_FAMILIAR}/exportar-consulta-completa": FakeResponse(404, b""),
        })
        with self.assertRaises(ServiceError) as error:
            built.consulta_completa(CPF)
        self.assertEqual(error.exception.code, "document_unavailable")
        self.assertEqual(error.exception.status, 404)

    def test_empty_success_body_is_retried_once_after_a_pause(self):
        built = client({"/suporte/configuracao/token": FakeResponse(200, b"")})
        with patch("dataprev.client.time.sleep") as pause, self.assertRaises(ServiceError) as error:
            built.identity()
        self.assertEqual(error.exception.code, "upstream_empty_response")
        self.assertEqual(len(built.session.requests), 2)
        pause.assert_called_once_with(0)

    def test_calls_are_paced_so_the_service_does_not_answer_empty(self):
        built = DataprevClient(jwt(), session=FakeSession(
            {"/suporte/configuracao/token": FakeResponse(200, int(CPF))}), min_interval=1.2)
        with patch("dataprev.client.time.sleep") as pause:
            built.identity()
            built.identity()
        causes = [round(call.args[0], 2) for call in pause.call_args_list]
        self.assertTrue(any(0.5 < value <= 1.2 for value in causes), causes)

    def test_response_above_the_limit_is_aborted(self):
        built = client({"/suporte/configuracao/token": FakeResponse(200, int(CPF))})
        with patch("dataprev.client.MAX_BODY", 4), self.assertRaises(ServiceError) as error:
            built.identity()
        self.assertEqual(error.exception.code, "upstream_response_limit")

    def test_headers_carry_no_cookie_and_match_the_client_version(self):
        built = client({"/suporte/configuracao/token": FakeResponse(200, int(CPF))})
        built.identity()
        headers = built.session.requests[-1]["headers"]
        self.assertEqual(headers["CnasVersao"], CNAS_VERSION)
        self.assertEqual(headers["Accept"], "application/json")
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("X-XSRF-TOKEN", headers)
        self.assertEqual(built.session.cookies, {})

    def test_check_reports_identity_without_pii(self):
        built = client({
            "/suporte/configuracao/token": FakeResponse(200, int(CPF)),
            f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body()),
        })
        result = built.check()
        self.assertEqual(result["cpf_mask"], mask(CPF))
        self.assertNotIn(CPF, json.dumps(result))
        self.assertEqual(result["numero_familiar"], str(NUMERO_FAMILIAR))

    def test_check_rejects_a_token_whose_subject_disagrees_with_the_api(self):
        built = DataprevClient(jwt(sub="98765432109"), session=FakeSession(
            {"/suporte/configuracao/token": FakeResponse(200, int(CPF)),
             f"/pessoa/{CPF}/tipos-perfil": FakeResponse(200, profile_body())}))
        with self.assertRaises(ServiceError) as error:
            built.check()
        self.assertEqual(error.exception.code, "identity_mismatch")

    def test_export_is_the_bearer_alone_with_declared_validity(self):
        built = client({})
        result = export(built)
        self.assertEqual(result["base_url"], built.base)
        self.assertTrue(result["headers"]["Authorization"].startswith("Bearer "))
        self.assertNotIn(CPF, json.dumps(result))
        self.assertIn("seconds_left", result["token_validity"])

    def test_session_for_mounts_the_dataprev_tls_profile(self):
        session = session_for("fixture-token")
        self.assertEqual(session.headers["Authorization"], "Bearer fixture-token")
        self.assertTrue(any(getattr(adapter, "ssl_context", None) or True
                            for adapter in session.adapters.values()))

    def test_save_writes_the_validated_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = save({"pdf": PDF}, Path(temporary) / "out" / "comprovante.pdf")
            self.assertEqual(path.read_bytes(), PDF)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
