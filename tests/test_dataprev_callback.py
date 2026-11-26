"""Testes offline do callback do gateway, com o retorno simulado localmente.

Nenhum teste abre o portal: o retorno do OAuth chega por uma página falsa, com os
três desfechos possíveis — erro definitivo, falha passageira sinalizada e silêncio.
"""
import json
import time

import pytest
from playwright.sync_api import Error as PlaywrightError

from auth.govbr import AuthError
from dataprev import callback
from dataprev import login as dataprev_login

HOME = "https://cadunico.dataprev.gov.br/#/home"
CALLBACK = "https://cadunico.dataprev.gov.br/transacional/login/oauth2/code/gateway"
STATE = "estado-sintetico"
CODE = "codigo-sintetico"


class Response:
    """Resposta do gateway simulada: URL do callback e HTTP, sem corpo."""

    def __init__(self, url, status):
        self.url = url
        self.status = status


class Page:
    """Página falsa: entrega o próximo retorno do gateway quando é consultada."""

    def __init__(self, url=HOME, responses=(), token="", token_after=0):
        self.url = url
        self.responses = list(responses)
        self.token = token
        self.token_after = token_after
        self.handlers = {}
        self.polls = 0

    def on(self, event, handler):
        self.handlers[event] = handler

    def evaluate(self, script):
        if "getItem" not in script:
            return []
        self.polls += 1
        if self.responses:
            self.handlers["response"](self.responses.pop(0))
        return self.token if self.polls >= self.token_after else ""


def terminal(status=400):
    return Response(f"{CALLBACK}?error=access_denied&state={STATE}", status)


def transient(status=503):
    return Response(f"{CALLBACK}?error=temporarily_unavailable&state={STATE}", status)


def quiet_login_failure(monkeypatch):
    monkeypatch.setattr(dataprev_login, "login_failure", lambda page: None)


@pytest.mark.parametrize("params,status,kind,code", [
    ("error=access_denied&state=" + STATE, 400, "terminal", "callback_access_denied"),
    ("error=login_required&state=" + STATE, 400, "terminal", "callback_access_denied"),
    ("error=consent_required&state=" + STATE, 400, "terminal", "callback_access_denied"),
    ("error=invalid_scope&state=" + STATE, 400, "terminal", "callback_rejected"),
    ("error=inexistente&state=" + STATE, 400, "terminal", "callback_rejected"),
    ("error=temporarily_unavailable&state=" + STATE, 503, "transient",
     "callback_temporarily_unavailable"),
    ("error=server_error&state=" + STATE, 500, "transient", "callback_temporarily_unavailable"),
    ("code=" + CODE + "&state=" + STATE, 403, "terminal", "callback_rejected"),
    ("code=" + CODE + "&state=" + STATE, 429, "transient", "callback_temporarily_unavailable"),
    ("code=" + CODE + "&state=" + STATE, 502, "transient", "callback_temporarily_unavailable"),
])
def test_each_conclusive_callback_maps_to_one_short_code(params, status, kind, code):
    outcome = callback.classify(f"{CALLBACK}?{params}", status)
    assert (outcome.kind, outcome.code, outcome.http_status) == (kind, code, status)


@pytest.mark.parametrize("url,status", [
    (f"{CALLBACK}?code={CODE}&state={STATE}", 200),
    (f"{CALLBACK}?code={CODE}&state={STATE}", 302),
    (f"{CALLBACK}?code={CODE}&state={STATE}", None),
    (f"{CALLBACK}?state={STATE}", 400),
    (CALLBACK, 500),
    ("https://cadunico.dataprev.gov.br/transacional/api/consulta", 503),
    ("https://outro.gov.br/callback?error=access_denied&state=" + STATE, 400),
    ("", None)])
def test_no_conclusive_answer_stays_open(url, status):
    assert callback.classify(url, status) is None


def test_the_error_can_arrive_in_the_fragment():
    outcome = callback.classify(
        f"https://cadunico.dataprev.gov.br/#/login?error=access_denied&state={STATE}")
    assert (outcome.kind, outcome.code) == ("terminal", "callback_access_denied")


def test_the_log_line_carries_only_stage_time_and_code():
    outcome = callback.classify(f"{CALLBACK}?error=access_denied&code={CODE}&state={STATE}", 400)
    event = outcome.event(1234.56)
    assert event == {"stage": "token", "kind": "terminal", "code": "callback_access_denied",
                     "http_status": 400, "elapsed_ms": 1235}
    for secret in (CODE, STATE, "?", "cadunico"):
        assert secret not in json.dumps(event)


def test_the_watch_ignores_responses_that_are_not_the_callback():
    watch = callback.CallbackWatch()
    for url, status in [
            ("https://cadunico.dataprev.gov.br/transacional/api/consulta", 503),
            ("https://exemplo.gov.br/callback?error=access_denied", 400),
            (f"{CALLBACK}?code={CODE}&state={STATE}", 200)]:
        watch.observe(Response(url, status))
    assert watch.outcome is None


def test_the_watch_keeps_only_the_classified_outcome():
    watch = callback.CallbackWatch()
    watch.observe(transient())
    watch.observe(terminal())
    assert watch.outcome == callback.Outcome("transient", "callback_temporarily_unavailable", 503)
    assert not hasattr(watch, "url")


def test_a_definitive_callback_error_aborts_almost_at_once(monkeypatch):
    quiet_login_failure(monkeypatch)
    pauses = []
    monkeypatch.setattr(dataprev_login.time, "sleep", pauses.append)
    page = Page(responses=[terminal(400)])
    receipt = {}
    with pytest.raises(AuthError) as error:
        dataprev_login.wait_for_token(page, receipt, timeout=30,
                                      watch=dataprev_login.callback_watch(page))
    assert error.value.code == "callback_access_denied"
    assert error.value.http_status == 400
    assert page.polls == 1 and pauses == []
    assert receipt["callback_events"] == [
        {"stage": "token", "kind": "terminal", "code": "callback_access_denied",
         "http_status": 400, "elapsed_ms": receipt["callback_events"][0]["elapsed_ms"]}]
    assert "final_host" not in receipt


def test_a_definitive_error_in_the_url_alone_also_aborts(monkeypatch):
    quiet_login_failure(monkeypatch)
    monkeypatch.setattr(dataprev_login.time, "sleep", pytest.fail)
    page = Page(url=f"{CALLBACK}?error=access_denied&state={STATE}")
    with pytest.raises(AuthError, match="^callback_access_denied$"):
        dataprev_login.wait_for_token(page, {}, timeout=30)


def test_a_transient_callback_waits_once_for_a_new_answer_then_stops(monkeypatch):
    quiet_login_failure(monkeypatch)
    pauses = []
    monkeypatch.setattr(dataprev_login.time, "sleep", pauses.append)
    slices = int(dataprev_login.TRANSIENT_WAIT_SECONDS / dataprev_login.POLL_SECONDS)
    page = Page(responses=[transient(503) for _ in range(slices + 1)])
    receipt = {}
    with pytest.raises(AuthError) as error:
        dataprev_login.wait_for_token(page, receipt, timeout=30,
                                      watch=dataprev_login.callback_watch(page))
    assert error.value.code == "callback_temporarily_unavailable"
    assert error.value.http_status == 503
    assert pauses == [dataprev_login.POLL_SECONDS] * slices
    assert [event["code"] for event in receipt["callback_events"]] == [
        "callback_temporarily_unavailable"]


def test_a_transient_callback_gets_a_limited_number_of_waits(monkeypatch):
    quiet_login_failure(monkeypatch)
    pauses = []
    monkeypatch.setattr(dataprev_login.time, "sleep", pauses.append)
    page = Page(responses=[transient(503), transient(429), transient(500)])
    receipt = {}
    with pytest.raises(AuthError) as error:
        dataprev_login.wait_for_token(page, receipt, timeout=30,
                                      watch=dataprev_login.callback_watch(page))
    assert error.value.code == "callback_temporarily_unavailable"
    assert error.value.http_status == 500
    assert [event["http_status"] for event in receipt["callback_events"]] == [503, 429, 500]
    assert pauses == [dataprev_login.POLL_SECONDS] * dataprev_login.TRANSIENT_RETRIES


def test_a_transient_callback_that_recovers_returns_the_token(monkeypatch):
    quiet_login_failure(monkeypatch)
    pauses = []
    monkeypatch.setattr(dataprev_login.time, "sleep", pauses.append)
    page = Page(responses=[transient(503)], token="fixture-token", token_after=3)
    receipt = {}
    assert dataprev_login.wait_for_token(page, receipt, timeout=30,
                                         watch=dataprev_login.callback_watch(page)) == "fixture-token"
    assert len(pauses) == 2
    assert [event["code"] for event in receipt["callback_events"]] == [
        "callback_temporarily_unavailable"]


def test_a_definitive_error_during_the_transient_wait_wins(monkeypatch):
    quiet_login_failure(monkeypatch)
    monkeypatch.setattr(dataprev_login.time, "sleep", lambda seconds: None)
    page = Page(responses=[transient(503), terminal(400)])
    receipt = {}
    with pytest.raises(AuthError) as error:
        dataprev_login.wait_for_token(page, receipt, timeout=30,
                                      watch=dataprev_login.callback_watch(page))
    assert error.value.code == "callback_access_denied"
    assert [event["code"] for event in receipt["callback_events"]] == [
        "callback_temporarily_unavailable", "callback_access_denied"]


def test_silence_keeps_the_whole_timeout_and_says_so(monkeypatch):
    quiet_login_failure(monkeypatch)
    page = Page()
    receipt = {}
    started = time.monotonic()
    assert dataprev_login.wait_for_token(page, receipt, timeout=0.3, slow_timeout=0.3,
                                         watch=dataprev_login.callback_watch(page)) == ""
    assert time.monotonic() - started >= 0.3
    assert receipt["callback_events"] == [
        {"stage": "token", "kind": "silent", "code": "callback_silent",
         "http_status": None, "elapsed_ms": receipt["callback_events"][0]["elapsed_ms"]}]
    assert receipt["final_host"] == "cadunico.dataprev.gov.br"


def test_the_watch_attaches_to_the_page_response_events():
    page = Page()
    watch = dataprev_login.callback_watch(page)
    assert page.handlers["response"] == watch.observe


def test_a_page_without_response_events_still_waits_on_the_url(monkeypatch):
    quiet_login_failure(monkeypatch)

    class Deaf:
        url = f"{CALLBACK}?error=access_denied&state={STATE}"

        def on(self, event, handler):
            raise PlaywrightError("sem eventos nesta página")

        def evaluate(self, script):
            return ""

    watch = dataprev_login.callback_watch(Deaf())
    assert watch.outcome is None
    with pytest.raises(AuthError, match="^callback_access_denied$"):
        dataprev_login.wait_for_token(Deaf(), {}, timeout=5, watch=watch)


def test_repeated_rounds_keep_the_same_verdict_and_the_same_cost(monkeypatch):
    """Volume: muitos ciclos seguidos, sem variar código, esperas ou vazamento."""
    quiet_login_failure(monkeypatch)
    pauses = []
    monkeypatch.setattr(dataprev_login.time, "sleep", pauses.append)
    veredictos = []
    for _ in range(40):
        page = Page(responses=[terminal(400)])
        receipt = {}
        with pytest.raises(AuthError) as error:
            dataprev_login.wait_for_token(page, receipt, timeout=30,
                                          watch=dataprev_login.callback_watch(page))
        veredictos.append((error.value.code, error.value.http_status,
                           len(receipt["callback_events"])))
        assert json.dumps(receipt).find(CODE) == -1
        assert json.dumps(receipt).find(STATE) == -1
    assert set(veredictos) == {("callback_access_denied", 400, 1)}
    assert pauses == []


def test_repeated_silent_rounds_cost_only_the_configured_timeout(monkeypatch):
    """Volume: sem resposta, cada rodada vira um único evento de silêncio."""
    quiet_login_failure(monkeypatch)
    monkeypatch.setattr(dataprev_login.time, "sleep", lambda seconds: None)
    for _ in range(10):
        page = Page()
        receipt = {}
        assert dataprev_login.wait_for_token(page, receipt, timeout=0.02, slow_timeout=0,
                                             watch=dataprev_login.callback_watch(page)) == ""
        assert receipt["callback_events"] == [
            {"stage": "token", "kind": "silent", "code": "callback_silent",
             "http_status": None, "elapsed_ms": receipt["callback_events"][0]["elapsed_ms"]}]


def test_repeated_transient_rounds_never_exceed_the_wait_budget(monkeypatch):
    quiet_login_failure(monkeypatch)
    slices = int(dataprev_login.TRANSIENT_WAIT_SECONDS / dataprev_login.POLL_SECONDS)
    for _ in range(20):
        pauses = []
        monkeypatch.setattr(dataprev_login.time, "sleep", pauses.append)
        page = Page(responses=[transient(503) for _ in range(slices + 1)])
        receipt = {}
        with pytest.raises(AuthError) as error:
            dataprev_login.wait_for_token(page, receipt, timeout=30,
                                          watch=dataprev_login.callback_watch(page))
        assert error.value.code == "callback_temporarily_unavailable"
        assert pauses == [dataprev_login.POLL_SECONDS] * slices
        assert len(receipt["callback_events"]) == 1


class Document:
    """Resposta da aplicação, com tipo de recurso e Content-Length do cliente."""

    def __init__(self, url, status, resource_type="document", content_length=None):
        self.url = url
        self.status = status
        self.request = type("Request", (), {"resource_type": resource_type})()
        self.headers = {} if content_length is None else {"content-length": str(content_length)}


def test_the_watch_records_the_redirect_and_the_last_application_document():
    watch = callback.CallbackWatch()
    watch.observe(Document(f"{CALLBACK}?code={CODE}&state={STATE}", 302))
    assert watch.redirect_seen is True
    assert watch.last_document == ""
    assert watch.outcome is None
    watch.observe(Document(HOME, 200))
    assert watch.last_document == "/"


def test_a_background_request_does_not_count_as_the_application_document():
    watch = callback.CallbackWatch()
    watch.observe(Document("https://cadunico.dataprev.gov.br/transacional/api/x", 200, "xhr"))
    assert watch.last_document == ""


def test_the_watch_records_an_empty_200_with_its_route():
    watch = callback.CallbackWatch()
    watch.observe(Document("https://cadunico.dataprev.gov.br/transacional/login/oauth2/x",
                           200, "fetch", 0))
    watch.observe(Document(HOME, 200, "document", 1200))
    assert watch.empty_200s == [("cadunico.dataprev.gov.br",
                                 "/transacional/login/oauth2/x", "fetch")]


def test_the_watch_keeps_only_host_and_status_of_errors():
    watch = callback.CallbackWatch()
    watch.observe(Document(f"{CALLBACK}?error=qualquer&state={STATE}", 500))
    watch.observe(Document(f"{CALLBACK}?error=qualquer&state={STATE}", 500))
    watch.observe(Document("https://cadunico.dataprev.gov.br/transacional/api/y", 404, "xhr"))
    watch.observe(Document("https://cadunico.dataprev.gov.br/pdf/12345678901/comprovante", 404, "xhr"))
    assert watch.errors == [("cadunico.dataprev.gov.br", 500,
                             "/transacional/login/oauth2/code/gateway"),
                            ("cadunico.dataprev.gov.br", 404,
                             "/transacional/api/y")]
    assert "12345678901" not in repr(watch.errors)


@pytest.mark.parametrize("entregues,redirect,document", [
    ([Document(f"{CALLBACK}?code={CODE}&state={STATE}", 302), Document(HOME, 200)], True, "/"),
    ([Document(f"{CALLBACK}?code={CODE}&state={STATE}", 302)], True, ""),
    ([], False, "")])
def test_silence_records_the_path_the_callback_took(monkeypatch, entregues, redirect, document):
    quiet_login_failure(monkeypatch)
    monkeypatch.setattr(dataprev_login.time, "sleep", lambda seconds: None)
    page = Page()
    watch = dataprev_login.callback_watch(page)
    for response in entregues:
        page.handlers["response"](response)
    receipt = {}
    assert dataprev_login.wait_for_token(page, receipt, timeout=0.02, slow_timeout=0,
                                         watch=watch) == ""
    assert receipt["callback_silence"] == {"redirect_seen": redirect,
                                           "last_document": document, "errors": [],
                                           "empty_200s": []}
    assert receipt["callback_events"][0]["code"] == "callback_silent"
    assert CODE not in json.dumps(receipt) and STATE not in json.dumps(receipt)


def test_a_silence_after_the_application_loaded_stops_at_the_short_window(monkeypatch):
    quiet_login_failure(monkeypatch)
    monkeypatch.setattr(dataprev_login.time, "sleep", lambda seconds: None)
    page = Page()
    watch = dataprev_login.callback_watch(page)
    page.handlers["response"](Document(HOME, 200))
    receipt = {}
    started = time.monotonic()
    assert dataprev_login.wait_for_token(page, receipt, timeout=0.05,
                                         watch=watch, slow_timeout=2) == ""
    assert time.monotonic() - started < 1
    assert receipt["callback_silence"]["last_document"] == "/"


def test_a_silence_while_the_application_never_loaded_uses_the_long_window(monkeypatch):
    quiet_login_failure(monkeypatch)
    monkeypatch.setattr(dataprev_login.time, "sleep", lambda seconds: None)
    page = Page()
    watch = dataprev_login.callback_watch(page)
    receipt = {}
    started = time.monotonic()
    assert dataprev_login.wait_for_token(page, receipt, timeout=0.05,
                                         watch=watch, slow_timeout=0.4) == ""
    assert time.monotonic() - started >= 0.4
    assert receipt["callback_silence"]["last_document"] == ""
