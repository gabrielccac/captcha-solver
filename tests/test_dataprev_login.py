"""Testes offline do credenciamento do CadÚnico (sem rede, sem browser)."""
import base64
import json
import time
from urllib.parse import urlsplit

import pytest
import requests

from auth.govbr import AuthError
from dataprev import login as dataprev_login
from dataprev.client import BASE, TOKEN

CPF = "12345678901"
OTHER_CPF = "98765432100"
PASSWORD = "senha-sintetica"
API_KEY = "chave-sintetica"
IDENTITY_PATH = urlsplit(BASE).path + TOKEN


def app_token(sub=CPF, aud=dataprev_login.CLIENT, exp=None, iss="https://sso.acesso.gov.br/"):
    payload = {"sub": sub, "aud": aud, "iss": iss,
               "exp": int(time.time()) + 3600 if exp is None else exp}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"fixture.{body}.fixture"


def make_response(status, body="", content_type="application/json"):
    response = requests.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    response._content_consumed = True
    response.encoding = "utf-8"
    response.headers["Content-Type"] = content_type
    return response


class Identity:
    """Servidor falso que responde só a chamada de identidade."""

    def __init__(self, body=None, status=200):
        self.body = json.dumps(int(CPF)) if body is None else body
        self.status = status
        self.paths = []

    def send(self, request, **kwargs):
        self.paths.append(urlsplit(request.url).path)
        return make_response(self.status, self.body)


def login_session(monkeypatch, token=None, **identity):
    session = requests.Session()
    server = Identity(**identity)
    session.send = server.send
    token = app_token() if token is None else token
    monkeypatch.setattr(dataprev_login, "browser_leg",
                        lambda cpf, password, api_key, receipt, phase, cdp_url="", headless=False: token)
    return session, server


def test_login_leaves_the_bearer_in_the_caller_session(monkeypatch):
    session, server = login_session(monkeypatch)
    receipt = {}
    result = dataprev_login.login(session, CPF, PASSWORD, API_KEY, receipt=receipt)
    assert result is receipt
    assert receipt["success"] is True and receipt["code"] == "ok"
    assert receipt["identity_status"] == 200
    assert receipt["token_audience"] == dataprev_login.CLIENT
    assert session.headers["Authorization"].startswith("Bearer ")
    assert server.paths == [IDENTITY_PATH]
    dump = json.dumps(receipt)
    for secret in (CPF, PASSWORD, API_KEY):
        assert secret not in dump


def test_login_reports_an_identity_failure(monkeypatch):
    session, _ = login_session(monkeypatch, status=401, body="")
    receipt = {}
    with pytest.raises(AuthError, match="^identity_failed$"):
        dataprev_login.login(session, CPF, PASSWORD, API_KEY, receipt=receipt)
    assert receipt["identity_status"] == 401
    assert receipt["failure_stage"] == "identity"
    assert receipt["failure_http_status"] == 401
    assert receipt["success"] is False


@pytest.mark.parametrize("body", [json.dumps(int(OTHER_CPF)), '"nao-e-cpf"'])
def test_login_rejects_an_identity_that_is_not_the_cpf(body, monkeypatch):
    session, _ = login_session(monkeypatch, body=body)
    receipt = {}
    with pytest.raises(AuthError) as error:
        dataprev_login.login(session, CPF, PASSWORD, API_KEY, receipt=receipt)
    assert error.value.code in ("identity_mismatch", "identity_failed")
    dump = json.dumps(receipt)
    assert CPF not in dump and OTHER_CPF not in dump


@pytest.mark.parametrize("overrides,code", [
    ({"aud": "outro.cliente.gov.br"}, "token_wrong_audience"),
    ({"sub": OTHER_CPF}, "identity_mismatch"),
    ({"exp": int(time.time()) - 60}, "token_expired"),
    ({"iss": "https://exemplo.gov.br/"}, "credential_wrong_issuer")])
def test_login_rejects_a_token_that_is_not_usable(overrides, code, monkeypatch):
    session, server = login_session(monkeypatch, token=app_token(**overrides))
    receipt = {}
    with pytest.raises(AuthError) as error:
        dataprev_login.login(session, CPF, PASSWORD, API_KEY, receipt=receipt)
    assert error.value.code == code
    assert server.paths == []
    assert receipt["success"] is False


def test_login_refuses_a_used_session_without_touching_the_network(monkeypatch):
    session = requests.Session()
    session.headers["Authorization"] = "Bearer fixture"
    monkeypatch.setattr(dataprev_login, "browser_leg",
                        lambda *a, **k: pytest.fail("não deveria abrir o browser"))
    with pytest.raises(AuthError, match="^fresh_http_session_required$"):
        dataprev_login.login(session, CPF, PASSWORD, API_KEY, receipt={})


@pytest.mark.parametrize("arguments,code", [
    (("123", PASSWORD, API_KEY), "invalid_cpf_format"),
    ((CPF, "", API_KEY), "password_missing_in_env"),
    ((CPF, PASSWORD, ""), "solver_key_missing")])
def test_login_validates_arguments_before_the_browser(arguments, code, monkeypatch):
    monkeypatch.setattr(dataprev_login, "browser_leg",
                        lambda *a, **k: pytest.fail("não deveria abrir o browser"))
    with pytest.raises(AuthError, match="^" + code + "$"):
        dataprev_login.login(requests.Session(), *arguments, receipt={})


class Page:
    """Página falsa: só o que as funções de espera consultam."""

    def __init__(self, url, token="", failure=None):
        self.url = url
        self.token = token
        self.failure = failure
        self.evaluations = 0

    def evaluate(self, script):
        self.evaluations += 1
        return self.token


def test_token_is_taken_from_localstorage_after_the_redirect(monkeypatch):
    page = Page("https://cadunico.dataprev.gov.br/#/home", token="fixture-token")
    monkeypatch.setattr(dataprev_login, "login_failure", lambda page: None)
    receipt = {}
    assert dataprev_login.wait_for_token(page, receipt, timeout=1) == "fixture-token"
    assert "final_host" not in receipt


def test_token_is_also_read_from_the_url_fragment():
    page = Page("https://cadunico.dataprev.gov.br/#/successLogin?token=fixture-fragment", token="")
    assert dataprev_login.application_token(page) == "fixture-fragment"


def test_wait_timeout_records_the_fragment_shape_without_token_values(monkeypatch):
    page = Page("https://cadunico.dataprev.gov.br/#/home?retorno=valor-nao-lido", token="")
    page.evaluate = lambda script: [] if "localStorage" in script else ""
    monkeypatch.setattr(dataprev_login, "login_failure", lambda page: None)
    receipt = {}
    assert dataprev_login.wait_for_token(page, receipt, timeout=0.2) == ""
    assert receipt["final_fragment_params"] == ["retorno"]
    assert receipt["final_storage_keys"] == []
    assert "valor-nao-lido" not in json.dumps(receipt)


def test_wait_timeout_records_the_localstorage_keys_when_the_fragment_is_empty(monkeypatch):
    page = Page("https://cadunico.dataprev.gov.br/#/home", token="")
    page.evaluate = lambda script: ["session", "token", "usuario"] if "Object.keys" in script else ""
    monkeypatch.setattr(dataprev_login, "login_failure", lambda page: None)
    receipt = {}
    assert dataprev_login.wait_for_token(page, receipt, timeout=0.2) == ""
    assert receipt["final_fragment_params"] == []
    assert receipt["final_storage_keys"] == ["session", "token", "usuario"]


def test_browser_source_names_the_real_display(monkeypatch):
    monkeypatch.setattr(dataprev_login, "_x11_display", lambda: True)
    assert dataprev_login.browser_source_label() == "xvfb_chrome"
    monkeypatch.setattr(dataprev_login, "_x11_display", lambda: False)
    assert dataprev_login.browser_source_label() == "chrome_display"


def test_login_reports_phase_progress_as_it_happens(monkeypatch):
    session, server = login_session(monkeypatch)
    seen = []
    dataprev_login.login(session, CPF, PASSWORD, API_KEY, receipt={}, progress=seen.append)
    assert seen == ["browser_start", "identity"]


def test_wait_reports_the_final_host_when_the_token_never_arrives(monkeypatch):
    page = Page("https://sso.acesso.gov.br/login?client_id=fixture")
    monkeypatch.setattr(dataprev_login, "login_failure", lambda page: None)
    receipt = {}
    assert dataprev_login.wait_for_token(page, receipt, timeout=0.2) == ""
    assert receipt["final_host"] == "sso.acesso.gov.br"


def test_wait_raises_the_classified_login_failure(monkeypatch):
    class Failure:
        code = "invalid_credentials"

    page = Page("https://sso.acesso.gov.br/login", failure=Failure())
    monkeypatch.setattr(dataprev_login, "login_failure", lambda page: page.failure)
    with pytest.raises(AuthError, match="^invalid_credentials$"):
        dataprev_login.wait_for_token(page, {}, timeout=1)


def test_cold_login_does_not_wait_for_a_token_before_the_account_form(monkeypatch):
    monkeypatch.setattr(dataprev_login, "login_failure", lambda page: None)
    monkeypatch.setattr(dataprev_login, "application_token", lambda page: "")
    page = Page("https://sso.acesso.gov.br/login?client_id=fixture")
    page.query_selector = lambda selector: object()
    started = time.monotonic()
    assert dataprev_login.wait_outcome(page, {}, timeout=10) == ("account", "")
    assert time.monotonic() - started < 2


def test_warm_login_returns_the_token_without_asking_for_the_form(monkeypatch):
    monkeypatch.setattr(dataprev_login, "login_failure", lambda page: None)
    page = Page("https://cadunico.dataprev.gov.br/#/home", token="fixture-token")
    page.query_selector = lambda selector: pytest.fail("não deveria procurar o formulário")
    assert dataprev_login.wait_outcome(page, {}, timeout=5) == ("token", "fixture-token")
