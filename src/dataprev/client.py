"""Cliente HTTP do transacional-api do CadÚnico a partir do token gov.br.

Este módulo não autentica: ele consome o ``access_token`` gov.br emitido para o
cliente ``meucadunico.dataprev.gov.br``. Validação ao vivo em 2026-11-24: o
transacional-api aceita esse Bearer sozinho, sem cookie de sessão, e recusa
qualquer CPF que não seja o do token com ``401 {"mensagemErro": {"codigo":"401"}}``.
"""
from __future__ import annotations

import base64
import json
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

BASE = "https://cadunico.dataprev.gov.br/transacional/api/transacional-api/v1"
PORTAL = "https://cadunico.dataprev.gov.br/"
AUDIENCE = "meucadunico.dataprev.gov.br"
CNAS_VERSION = "1.36.03"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36")
TIMEOUT = (5, 60)
MIN_INTERVAL = 1.2
MAX_BODY = 8_000_000

TOKEN = "/suporte/configuracao/token"
PERFIS = "/pessoa/{cpf}/tipos-perfil"
COMPROVANTE = "/pdf/{numero_familiar}/comprovante-cadastro/{identificador}/{cpf}"
CONSULTA_COMPLETA = "/pdf/{cpf}/{numero_familiar}/exportar-consulta-completa"

STATUS_CODES = {403: "forbidden", 404: "not_found", 429: "rate_limited"}


class ServiceError(RuntimeError):
    """Falha sanitizada: código curto, sem CPF, token, URL, corpo ou mensagem do servidor."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status


def tls_context():
    """Contexto exigido pelo Dataprev.

    O servidor só oferece TLS 1.2 com suítes RSA estáticas, que o OpenSSL 3 recusa no
    nível de segurança padrão. O ``SECLEVEL=1`` libera a negociação sem desligar a
    verificação do certificado; tentar TLS 1.3 ou o padrão do Python falha com
    ``sslv3 alert handshake failure``.
    """
    context = create_urllib3_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers("DEFAULT@SECLEVEL=1")
    return context


class _Adapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = tls_context()
        super().init_poolmanager(*args, **kwargs)


HOST_PREFIX = "https://cadunico.dataprev.gov.br/"


def prepare(session) -> requests.Session:
    """Aplica o perfil TLS do Dataprev só no host do CadÚnico.

    Vale para qualquer sessão que fale com esse host — inclusive as chamadas do
    gateway durante o login, que também passam por ele.
    """
    session.mount(HOST_PREFIX, _Adapter())
    return session


def session_for(token: str) -> requests.Session:
    """Sessão somente com o Bearer: cookies não são necessários e não são mantidos."""
    session = prepare(requests.Session())
    session.headers.update({"Authorization": "Bearer " + token, "Accept": "application/json",
                            "CnasVersao": CNAS_VERSION, "Referer": PORTAL, "User-Agent": USER_AGENT})
    return session


def claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError):
        return {}


def mask(cpf: str) -> str:
    return f"{cpf[:3]}.***.***-{cpf[-2:]}" if len(cpf) == 11 else "***"


def _digits(value) -> str | None:
    text = str(value).strip()
    return text if len(text) == 11 and text.isdigit() else None


def _size(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _decode(value) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None
    return raw or None


def _failure(error, receipt):
    from inss.http_errors import request_failure
    return request_failure(error, receipt)


class DataprevClient:
    """Leituras do transacional-api. Toda falha é ``ServiceError`` com código curto."""

    def __init__(self, token: str = "", *, session=None, base: str = BASE,
                 timeout=TIMEOUT, cnas_version: str = CNAS_VERSION, min_interval: float = MIN_INTERVAL):
        self.min_interval = min_interval
        self._last_call = 0.0
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.cnas_version = cnas_version
        self.session = session or session_for(token)
        header = self.session.headers.get("Authorization", "")
        self.token = token or header.removeprefix("Bearer ").strip()
        if not self.token:
            raise ServiceError("credential_missing")
        self.claims = claims(self.token)
        if self.claims.get("aud") and self.claims["aud"] != AUDIENCE:
            raise ServiceError("credential_wrong_audience")
        if self.claims.get("iss") and urlsplit(self.claims["iss"]).hostname != "sso.acesso.gov.br":
            raise ServiceError("credential_wrong_issuer")

    def seconds_left(self) -> float | None:
        """Validade declarada do Bearer. Não garante que ele não tenha sido revogado."""
        try:
            return round(float(self.claims["exp"]) - time.time())
        except (KeyError, TypeError, ValueError):
            return None

    def subject(self) -> str | None:
        return _digits(self.claims.get("sub"))

    def _headers(self) -> dict:
        """Headers do app, sempre por chamada.

        Não filtre pelo que a sessão já tem: ``requests.Session`` traz ``Accept: */*``
        e o User-Agent de ``python-requests``, e com esses valores o serviço responde
        XML no lugar de JSON.
        """
        return {"Accept": "application/json", "CnasVersao": self.cnas_version,
                "Referer": PORTAL, "User-Agent": USER_AGENT}

    def _pace(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: dict | None = None, receipt: dict | None = None):
        """Uma leitura por chamada, com ritmo e uma nova tentativa em resposta vazia.

        Medido em 2026-11-24: chamadas em rajada fazem o serviço devolver 200 com
        corpo vazio de forma intermitente; com pausa entre elas não houve falha.
        """
        receipt = {} if receipt is None else receipt
        started = time.monotonic()
        for attempt in range(2):
            self._pace()
            status, body = self._read(path, params, receipt)
            if status != 200 or body:
                break
            if attempt == 0:
                time.sleep(self.min_interval * 2)
        receipt.setdefault("elapsed_ms", round((time.monotonic() - started) * 1000))
        if status == 401:
            raise ServiceError("not_authorized" if body else "session_expired", 401)
        if status != 200:
            code = STATUS_CODES.get(status, "upstream_error")
            if status == 404 and path.startswith("/pdf/"):
                code = "document_unavailable"
            raise ServiceError(code, status)
        if not body:
            raise ServiceError("upstream_empty_response", 200)
        response = requests.Response()
        response.status_code = status
        response._content = body
        return response

    def _read(self, path: str, params: dict | None, receipt: dict):
        try:
            response = self.session.get(self.base + path, params=params or None,
                                        headers=self._headers(), timeout=self.timeout,
                                        allow_redirects=False, stream=True)
        except requests.RequestException as error:
            raise ServiceError(_failure(error, receipt)) from None
        body = bytearray()
        try:
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise ServiceError("upstream_response_limit", response.status_code)
        except requests.RequestException as error:
            raise ServiceError(_failure(error, receipt)) from None
        finally:
            response.close()
        receipt.update(http_status=response.status_code, response_bytes=len(body))
        return response.status_code, bytes(body)

    def _json(self, response):
        try:
            return json.loads(response.content)
        except (ValueError, UnicodeError):
            raise ServiceError("upstream_invalid_json", response.status_code) from None

    def identity(self) -> str:
        """CPF autenticado na sessão, segundo o próprio serviço."""
        cpf = _digits(self._json(self._get(TOKEN)))
        if cpf is None:
            raise ServiceError("identity_unparsed")
        return cpf

    def profiles(self, cpf: str) -> list:
        cpf = _digits(cpf)
        if cpf is None:
            raise ServiceError("invalid_cpf_format")
        value = self._json(self._get(PERFIS.format(cpf=cpf)))
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ServiceError("profiles_unparsed")
        return value

    def profile(self, cpf: str) -> dict:
        """Perfil do CPF que traz ``numeroFamiliar`` e ``identificador`` para os PDFs."""
        own = [item for item in self.profiles(cpf)
               if _digits(item.get("cpf")) == _digits(cpf) and _size(item.get("numeroFamiliar"))]
        if not own:
            raise ServiceError("profile_not_found")
        if len(own) > 1:
            raise ServiceError("profile_ambiguous")
        return own[0]

    def check(self) -> dict:
        """Credencial utilizável: identidade, vínculo familiar e validade declarada."""
        cpf = self.identity()
        if self.subject() and self.subject() != cpf:
            raise ServiceError("identity_mismatch")
        profile = self.profile(cpf)
        return {"cpf_mask": mask(cpf), "numero_familiar": str(profile["numeroFamiliar"]),
                "perfil": (profile.get("tipoPerfil") or {}).get("nome"),
                "seconds_left": self.seconds_left()}

    def comprovante(self, cpf: str = "", *, representante_legal: bool = False) -> dict:
        """Comprovante de cadastro: PDF principal mais a folha de validação e o QR code."""
        receipt = {}
        cpf = _digits(cpf) or self.identity()
        profile = self.profile(cpf)
        path = COMPROVANTE.format(numero_familiar=profile["numeroFamiliar"],
                                  identificador=profile["identificador"], cpf=cpf)
        params = {"representanteLegal": "true"} if representante_legal else None
        body = self._json(self._get(path, params, receipt))
        if not isinstance(body, dict):
            raise ServiceError("comprovante_unparsed")
        pdf = _decode(body.get("base64PDF"))
        if pdf is None or not pdf.startswith(b"%PDF-"):
            raise ServiceError("document_not_pdf", 502)
        size = _size(body.get("tamanhoDoArquivo"))
        if size is not None and abs(size - len(pdf)) > 4:
            raise ServiceError("document_size_mismatch", 502)
        validacao = body.get("validacaoComprovante") if isinstance(body.get("validacaoComprovante"), dict) else {}
        return {
            "document": "comprovante",
            "pdf": pdf,
            "size_bytes": len(pdf),
            "http_status": receipt.get("http_status"),
            "elapsed_ms": receipt.get("elapsed_ms"),
            "numero_familiar": str(body.get("numeroFamiliar") or profile["numeroFamiliar"]),
            "chave_seguranca": body.get("chaveSeguranca"),
            "url_validacao": body.get("urlValidacaoComprovante"),
            "data_emissao": body.get("dataEmissao"),
            "municipio_uf": body.get("municipioUF"),
            "cadastro_atualizado": body.get("cadastroAtualizado"),
            "data_cadastro": body.get("dataCadastro"),
            "data_atualizacao": body.get("dataAtualizacao"),
            "data_limite_proxima_atualizacao": body.get("dataLimiteParaProximaAtualizacao"),
            "faixa_renda_familiar_total": body.get("faixaRendaFamiliarTotal"),
            "faixa_renda_familiar_por_pessoa": body.get("faixaRendaFamiliarPorPessoa"),
            "eh_indigena": body.get("ehIndigena"),
            "eh_quilombola": body.get("ehQuilombola"),
            "integrantes": body.get("integrantes") if isinstance(body.get("integrantes"), list) else [],
            "qr_code": _decode(body.get("base64QRCode")),
            "validacao": {"codigo": validacao.get("codigoValidacao"),
                          "solicitante": validacao.get("solicitante"),
                          "data_emissao": validacao.get("dataEmissao"),
                          "pdf": _decode(validacao.get("bytes"))},
        }

    def consulta_completa(self, cpf: str = "") -> dict:
        """Exportação do formulário completo. Não existe para todo cadastro."""
        receipt = {}
        cpf = _digits(cpf) or self.identity()
        profile = self.profile(cpf)
        path = CONSULTA_COMPLETA.format(cpf=cpf, numero_familiar=profile["numeroFamiliar"])
        body = self._json(self._get(path, receipt=receipt))
        if not isinstance(body, dict):
            raise ServiceError("document_unparsed")
        pdf = _decode(body.get("base64PDF"))
        if pdf is None or not pdf.startswith(b"%PDF-"):
            raise ServiceError("document_not_pdf", 502)
        size = _size(body.get("tamanhoDoArquivo"))
        if size is not None and abs(size - len(pdf)) > 4:
            raise ServiceError("document_size_mismatch", 502)
        return {
            "document": "consulta_completa",
            "pdf": pdf,
            "size_bytes": len(pdf),
            "http_status": receipt.get("http_status"),
            "elapsed_ms": receipt.get("elapsed_ms"),
            "numero_familiar": str(profile["numeroFamiliar"]),
            "familia_cadastrada": body.get("familiaCadastrada"),
        }


def export(client: DataprevClient) -> dict:
    """Credencial para um consumidor externo, no mesmo formato que o serviço hospedado usa.

    O transacional-api não precisa de cookie: o Bearer gov.br é a credencial inteira.
    """
    return {"base_url": client.base, "headers": {"Authorization": "Bearer " + client.token,
                                                 "CnasVersao": client.cnas_version},
            "token_validity": {"seconds_left": client.seconds_left(), "subject": mask(client.subject() or "")}}


def save(result: dict, path, *, mode: int = 0o600) -> Path:
    """Grava o PDF já validado. O chamador decide onde e quando apagar."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as stream:
        target.chmod(mode)
        stream.write(result["pdf"])
    return target
