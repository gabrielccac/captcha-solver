"""Credenciamento gov.br no gateway do CadÚnico, inteiro no navegador.

O CadÚnico é um cliente OAuth2 de servidor: state, nonce e PKCE nascem na aplicação e
a troca do `code` acontece no próprio gateway, então não existe troca para fazer em
Python. Depois do callback o SPA guarda o token em ``localStorage``; é esse Bearer —
nunca o cookie de sessão — que autentica o transacional-api.
"""
from __future__ import annotations

import io
import time
from contextlib import ExitStack, redirect_stderr
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

import requests
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from auth.govbr import AuthError, fragment_param
from captcha import hcaptcha
from captcha.browser import ManagedBrowser, _x11 as _x11_display
from inss.http_errors import observe, request_failure
from inss.session import login_failure, submit_cpf, submit_password
from . import callback
from .client import DataprevClient, ServiceError, claims, prepare

PORTAL = "https://cadunico.dataprev.gov.br/"
APPLICATION_HOST = "cadunico.dataprev.gov.br"
START = PORTAL + "transacional/api/oauth2/authorization/gateway-transacional"
CLIENT = "meucadunico.dataprev.gov.br"
SSO_HOST = "sso.acesso.gov.br"
ACCOUNT_FIELD = 'input[name="accountId"], input#accountId'
PASSWORD_SELECTOR = 'input[type="password"]'
MODEL = "meta/muse-spark-1.3-contributor"
MAX_ROUNDS = 3
WAIT_SECONDS = 40
POLL_SECONDS = 0.5
TRANSIENT_RETRIES = 2
TRANSIENT_WAIT_SECONDS = 5


def fragment_params(fragment):
    """Nomes dos parâmetros do fragmento, sem valores: o token pode estar nele."""
    if "?" not in fragment:
        return []
    return sorted(parse_qs(fragment.split("?", 1)[1], keep_blank_values=True))


def storage_keys(page):
    """Chaves do localStorage da página final: nomes, nunca valores."""
    try:
        return sorted(page.evaluate("() => Object.keys(window.localStorage)") or [])
    except PlaywrightError:
        return []


def application_token(page):
    """Bearer que o SPA do CadÚnico guardou, ou `""` enquanto a página navega."""
    try:
        token = page.evaluate("() => window.localStorage.getItem('token')") or ""
        return token or fragment_param(page.url, "token")
    except PlaywrightError:
        return ""


def account_or_consent(page):
    """Próximo passo do SSO: formulário de CPF, senha, consentimento ou 2FA."""
    try:
        if page.query_selector(ACCOUNT_FIELD):
            return "account"
        if page.query_selector(PASSWORD_SELECTOR):
            return "password"
        if page.get_by_role("button", name="Autorizar", exact=True).count():
            return "consent"
        return ""
    except PlaywrightError:
        return ""


def callback_outcome(page, watch=None):
    """Desfecho conclusivo do callback: o da resposta observada ou o da URL atual."""
    if watch is not None and watch.outcome is not None:
        return watch.outcome
    try:
        return callback.classify(page.url)
    except PlaywrightError:
        return None


def retry_transient(page, watch, outcome, deadline):
    """Espera uma resposta nova quando o gateway sinaliza falha passageira.

    A espera é curta e limitada: se o retorno não mudar, quem insiste é o
    ``--retry`` do fluxo, com nova autorização. Devolve o token que aparecer e o
    desfecho novo, quando houver.
    """
    if watch is not None:
        watch.outcome = None
    for _ in range(int(TRANSIENT_WAIT_SECONDS / POLL_SECONDS)):
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
        token = application_token(page)
        if token:
            return token, None
        change = callback_outcome(page, watch)
        if change is not None and change != outcome:
            return "", change
    return "", None


def wait_for_token(page, receipt, timeout=WAIT_SECONDS, watch=None):
    """Espera o desfecho autenticado, que pode chegar por SSO silencioso.

    Erro definitivo no callback encerra a espera na hora, sem gastar o timeout.
    Falha passageira sinalizada ganha uma espera curta por uma resposta nova e,
    se nada mudar, também encerra; o silêncio — nenhum callback, nenhuma
    resposta — espera a janela inteira, porque aí não há o que ler.
    """
    started = time.monotonic()
    deadline = started + timeout
    retries = TRANSIENT_RETRIES
    events = receipt.setdefault("callback_events", [])
    while time.monotonic() < deadline:
        token = application_token(page)
        if token:
            return token
        try:
            failure = login_failure(page)
        except PlaywrightError:
            failure = None
        if failure:
            raise AuthError(failure.code, http_status=getattr(failure, "status", None))
        outcome = callback_outcome(page, watch)
        while outcome is not None:
            events.append(outcome.event((time.monotonic() - started) * 1000))
            if outcome.kind == "terminal" or not retries:
                raise AuthError(outcome.code, http_status=outcome.http_status)
            retries -= 1
            token, change = retry_transient(page, watch, outcome, deadline)
            if token:
                return token
            if change is None:
                raise AuthError(outcome.code, http_status=outcome.http_status)
            outcome = change
        time.sleep(POLL_SECONDS)
    events.append(callback.Outcome("silent", "callback_silent").event(
        (time.monotonic() - started) * 1000))
    if watch is not None:
        receipt["callback_silence"] = {"redirect_seen": watch.redirect_seen,
                                      "document_seen": watch.document_seen}
    url = urlsplit(page.url)
    receipt["final_host"] = url.hostname or ""
    receipt["final_path"] = url.path
    receipt["final_has_hash"] = bool(url.fragment)
    receipt["final_fragment_params"] = fragment_params(url.fragment)
    receipt["final_storage_keys"] = storage_keys(page)
    return ""


def wait_outcome(page, receipt, timeout=WAIT_SECONDS):
    """Espera o primeiro desfecho útil: token do SSO silencioso ou formulário de CPF.

    Um único laço, de propósito: esperar o token com prazo fixo antes de olhar o
    formulário custa esse prazo inteiro em todo login frio.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        token = application_token(page)
        if token:
            return "token", token
        try:
            if urlsplit(page.url).hostname == SSO_HOST and page.query_selector(ACCOUNT_FIELD):
                return "account", ""
            failure = login_failure(page)
        except PlaywrightError:
            failure = None
        if failure:
            raise AuthError(failure.code, http_status=getattr(failure, "status", None))
        time.sleep(0.5)
    receipt["final_host"] = urlsplit(page.url).hostname or ""
    return "unknown", ""


def callback_watch(page):
    """Escuta as respostas do callback; sem eventos na página, sobra a URL."""
    watch = callback.CallbackWatch()
    try:
        page.on("response", watch.observe)
    except PlaywrightError:
        pass
    return watch


def browser_source_label(headless=False):
    """De onde vem o display: Xvfb no Linux, a tela do usuário no resto, nada no headless."""
    if headless:
        return "chrome_headless"
    return "xvfb_chrome" if _x11_display() else "chrome_display"


def browser_leg(cpf, password, api_key, receipt, phase, cdp_url="", headless=False):
    """Gateway, CPF, hCaptcha, senha no próprio formulário e captura do token."""
    with ExitStack() as resources:
        playwright = resources.enter_context(sync_playwright())
        if cdp_url:
            receipt["browser_source"] = "external_cdp"
            browser = playwright.chromium.connect_over_cdp(cdp_url)
        else:
            receipt["browser_source"] = browser_source_label(headless)
            profile = resources.enter_context(TemporaryDirectory(prefix="dataprev-govbr-"))
            manager = ManagedBrowser(profile, quiet=True, headless=headless)
            resources.callback(manager.stop)
            browser = playwright.chromium.connect_over_cdp(manager.start(wait_ready=True))
            receipt["cdp_port"] = manager.cdp_port
            resources.callback(browser.close)
        page = browser.contexts[0].pages[0]
        watch = callback_watch(page)
        receipt["browser_version"] = browser.version
        phase("gateway")
        page.goto(START, wait_until="domcontentloaded", timeout=45000)
        kind, value = wait_outcome(page, receipt)
        if kind == "token":
            receipt["sso_reused"] = True
            return value
        if kind != "account":
            raise AuthError("login_page_unrecognized")
        masked = f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"
        phase("cpf")
        submit_cpf(page, masked)
        phase("captcha")
        with TemporaryDirectory(prefix="dataprev-captcha-") as images:
            deadline = time.monotonic() + 30
            while not hcaptcha.vis_modal(page) and not hcaptcha.page_state(
                    page, PASSWORD_SELECTOR).get("done"):
                failure = login_failure(page)
                if failure:
                    raise AuthError(failure.code)
                if time.monotonic() > deadline:
                    raise AuthError("captcha_challenge_missing")
                page.wait_for_timeout(500)
            for round_index in range(MAX_ROUNDS):
                if hcaptcha.page_state(page, PASSWORD_SELECTOR).get("done"):
                    break
                with redirect_stderr(io.StringIO()):
                    done, info = hcaptcha.solve_round(page, images, round_index, MODEL,
                        "minimal", PASSWORD_SELECTOR, api_key=api_key)
                receipt.setdefault("rounds", []).append({key: info[key] for key in
                    ("round", "modal_wait_s", "capture_s", "extract_s", "solve_s",
                     "click_s", "verdict_s") if isinstance(info.get(key), (int, float))})
                if info.get("why") == "selection_not_confirmed":
                    raise AuthError("captcha_selection_not_confirmed")
                if "solve_error" in info:
                    raise AuthError("solver_provider_error")
                if done or hcaptcha.page_state(page, PASSWORD_SELECTOR).get("done"):
                    break
            if not page.query_selector(PASSWORD_SELECTOR):
                raise AuthError("captcha_not_accepted")
        phase("password")
        submit_password(page, password, APPLICATION_HOST)

        try:
            authorize = page.get_by_role("button", name="Autorizar", exact=True)
            if authorize.count() and authorize.first.is_visible():
                receipt["consent_clicked"] = True
                authorize.first.click(timeout=10000)
        except PlaywrightError:
            pass
        phase("token")
        token = wait_for_token(page, receipt, watch=watch)
        if not token:
            raise AuthError("token_not_captured")
        return token


def verify_token(session, token, cpf, receipt) -> None:
    """O Bearer precisa ser deste CPF, deste cliente e estar no prazo."""
    values = claims(token)
    receipt["token_audience"] = values.get("aud")
    if values.get("aud") != CLIENT:
        raise AuthError("token_wrong_audience")
    if str(values.get("sub")) != cpf:
        raise AuthError("identity_mismatch")
    try:
        if float(values.get("exp", 0)) <= time.time():
            raise AuthError("token_expired")
    except (TypeError, ValueError):
        raise AuthError("token_claims_unreadable") from None
    session.headers["Authorization"] = "Bearer " + token
    try:
        client = DataprevClient(session=session)
    except ServiceError as error:
        raise AuthError(error.code) from None
    try:
        authenticated = client.identity()
    except ServiceError as error:
        receipt["identity_status"] = error.status
        raise AuthError("identity_failed", http_status=error.status) from None
    receipt["identity_status"] = 200
    if authenticated != cpf:
        raise AuthError("identity_mismatch")


def login(session, cpf, password, api_key, *, receipt=None, cdp_url="", progress=None,
          headless=False):
    """Autentica a `requests.Session` do chamador no cadunico.dataprev.gov.br.

    Um login por chamada. Ao final a sessão carrega ``Authorization: Bearer`` com o
    token gov.br do CadÚnico; o token nunca entra no recibo.
    """
    receipt = {} if receipt is None else receipt
    receipt.update(success=False, state_delegated=True, identity_status=None,
                   phase_events=[], model=MODEL)
    receipt.pop("failure_stage", None)
    receipt.pop("failure_http_status", None)
    started = time.monotonic()

    def phase(name):
        receipt["phase_events"].append({"phase": name,
            "elapsed_ms": round((time.monotonic() - started) * 1000)})
        if progress:
            progress(name)

    observer = observe(session, receipt)
    observer.__enter__()
    try:
        if len(cpf) != 11 or not cpf.isascii() or not cpf.isdigit():
            raise AuthError("invalid_cpf_format")
        if not password:
            raise AuthError("password_missing_in_env")
        if not api_key:
            raise AuthError("solver_key_missing")
        if session.headers.get("Authorization") or session.cookies:
            raise AuthError("fresh_http_session_required")
        prepare(session)
        phase("browser_start")
        token = browser_leg(cpf, password, api_key, receipt, phase, cdp_url,
                            headless=headless)
        phase("identity")
        verify_token(session, token, cpf, receipt)
        receipt.update(success=True, code="ok")
        return receipt
    except AuthError as error:
        events = receipt["phase_events"]
        if events:
            receipt["failure_stage"] = events[-1]["phase"]
        if error.http_status is not None:
            receipt["failure_http_status"] = error.http_status
        receipt["code"] = error.code
        raise
    except requests.RequestException as error:
        receipt["code"] = request_failure(error, receipt)
        raise AuthError(receipt["code"]) from None
    except PlaywrightError as error:
        receipt["browser_error_type"] = type(error).__name__
        receipt["code"] = "browser_error"
        raise AuthError("browser_error") from None
    except Exception as error:
        receipt["runtime_error_type"] = type(error).__name__
        receipt["code"] = "dataprev_login_runtime_error"
        raise AuthError("dataprev_login_runtime_error") from None
    finally:
        observer.__exit__(None, None, None)
        receipt["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        events = receipt["phase_events"]
        receipt["phase_ms"] = {event["phase"]: (events[i + 1]["elapsed_ms"] if i + 1 < len(events)
            else receipt["elapsed_ms"]) - event["elapsed_ms"] for i, event in enumerate(events)}
