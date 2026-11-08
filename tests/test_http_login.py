import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

import pytest
import requests

from auth.handoff import CaptchaHandoff, password_page
from auth.govbr import AuthError, GovBrAuth
from inss.http_login import load_env_file, verify_api
from inss import http_login


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Auth unit tests cannot access the network")
    monkeypatch.setattr(requests.sessions.Session, "request", denied)


def test_env_file_is_read_as_data_without_evaluation(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# comentÃ¡rio\nGOVBR_CPF=00000000000\nGOVBR_PASSWORD="fixture@password"\nOPENROUTER_API_KEY=fixture-key\n', encoding="utf-8")
    loaded = load_env_file(path)
    assert loaded["GOVBR_PASSWORD"] == "fixture@password"
    assert loaded["OPENROUTER_API_KEY"] == "fixture-key"


def test_capture_blocks_browser_and_http_consumes_exactly_once():
    body = "operation=captcha&token=" + "A" * 40 + "&_csrf=fixture%2Bcsrf"
    route = Mock(request=SimpleNamespace(url="https://sso.acesso.gov.br/login?authorization_id=fixture",
        method="POST", post_data=body, headers={"content-type": "application/x-www-form-urlencoded",
                                              "cookie": "fixture=private", "accept-encoding": "br"}))
    capture = CaptchaHandoff()
    capture.intercept(route)
    route.continue_.assert_not_called()
    route.fulfill.assert_called_once()
    assert capture.captured_at is not None
    session = Mock()
    capture.consume(session)
    assert session.post.call_args.kwargs["data"] == body.encode()
    assert "cookie" not in session.post.call_args.kwargs["headers"]
    assert "accept-encoding" not in session.post.call_args.kwargs["headers"]
    with pytest.raises(AuthError, match="already_consumed"):
        capture.consume(session)
    session.post.assert_called_once()


def test_initial_cpf_form_is_forwarded_without_fabricating_token():
    route = Mock(request=SimpleNamespace(url="https://sso.acesso.gov.br/login", method="POST",
        post_data="accountId=00000000000&_csrf=fixture", headers={}))
    capture = CaptchaHandoff()
    capture.intercept(route)
    route.continue_.assert_called_once()
    route.fulfill.assert_not_called()
    assert capture.captured_at is None


class Response:
    status_code = 200
    text = "<html>Portal</html>"
    headers = {}

    def __init__(self, data=None):
        self.data = data

    def json(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_callback_is_exchanged_and_authenticated_endpoint_is_required():
    cpf = "00000000000"
    payload = base64.urlsafe_b64encode(json.dumps({"sub": cpf}).encode()).decode().rstrip("=")
    token = "fixture." + payload + ".fixture"
    session = Mock()
    responses = iter([Response({"access_token": token}),
                      Response({"miToken": "fixture-mi"}), Response()])
    headers_used = []

    def get(url, **kwargs):
        headers_used.append(dict(kwargs.get("headers", {})))
        return next(responses)

    session.get.side_effect = get
    receipt = {}
    verify_api(session, "fixture-code", cpf, receipt)
    assert receipt["api_session_verified"] is True
    assert receipt["authenticated_endpoint_http_status"] == 200
    assert session.get.call_count == 3
    assert "/govbrservices/token/fixture-code/clientid/" in session.get.call_args_list[0].args[0]
    assert session.get.call_args.kwargs["stream"] is True
    assert session.get.call_args.kwargs["headers"]["miToken"] == "fixture-mi"
    assert headers_used[1]["miToken"] == "undefined"
    assert headers_used[2]["miToken"] == "fixture-mi"
    assert session.headers.update.call_args.args[0]["miToken"] == "fixture-mi"


@pytest.mark.parametrize("failure,code", [(0, "code_exchange_failed"),
    (1, "userinfo_bootstrap_failed"), (2, "authenticated_endpoint_failed")])
def test_api_failures_never_export_session_tokens(failure, code):
    payload = base64.urlsafe_b64encode(b'{"sub":"00000000000"}').decode().rstrip("=")
    responses = [Response({"access_token": "x." + payload + ".x"}),
                 Response({"miToken": "fixture-mi"}), Response()]
    responses[failure].status_code = 403
    session = Mock()
    session.get.side_effect = responses
    receipt = {}
    with pytest.raises(AuthError, match=code):
        verify_api(session, "fixture-code", "00000000000", receipt)
    assert not receipt.get("api_session_verified")
    session.headers.update.assert_not_called()


def test_api_rejects_wrong_identity_before_bootstrap():
    payload = base64.urlsafe_b64encode(b'{"sub":"11111111111"}').decode().rstrip("=")
    session = Mock()
    session.get.return_value = Response({"access_token": "x." + payload + ".x"})
    with pytest.raises(AuthError, match="identity_mismatch"):
        verify_api(session, "fixture-code", "00000000000", {})
    session.get.assert_called_once()


@pytest.mark.parametrize("failure", [None, "sso_login_required", "authenticated_identity_mismatch",
                                     "network_error", "runtime_error"])
def test_http_renewal_preserves_old_state_on_failure_and_never_starts_browser(monkeypatch, failure):
    real_session = requests.Session
    original, sso, api = real_session(), real_session(), real_session()
    original.headers.update({"User-Agent": "fixture-actual-ua", "Authorization": "Bearer expired-fixture",
                             "miToken": "private-old-mi"})
    original.cookies.set("fixture_sso", "private-old-cookie", domain="sso.acesso.gov.br", secure=True)
    before_headers, before_cookies = dict(original.headers), original.cookies.get_dict()
    clients = iter([sso, api])
    monkeypatch.setattr(http_login.requests, "Session", lambda: next(clients))
    browser = Mock(side_effect=AssertionError("renewal cannot start browser"))
    monkeypatch.setattr(http_login, "ManagedBrowser", browser)
    auth = Mock(authorization_code="fresh-fixture-code")

    def authorize():
        assert "Authorization" not in sso.headers and "miToken" not in sso.headers
        assert sso.headers["User-Agent"] == "fixture-actual-ua"
        assert sso.cookies.get("fixture_sso") == "private-old-cookie"
        sso.cookies.set("fixture_sso", "private-rotated-cookie", domain="sso.acesso.gov.br", secure=True)
        if failure == "sso_login_required":
            raise AuthError(failure)
        if failure == "network_error":
            raise requests.Timeout("private-url-cookie-details")
        if failure == "runtime_error":
            raise RuntimeError("private-url-cookie-details")
        return [("https://meu.inss.gov.br/", 200)]

    auth.resume.side_effect = authorize
    monkeypatch.setattr(http_login, "GovBrAuth", lambda *a, **kw: auth)

    def verify(client, code, cpf, receipt):
        assert client is api and code == "fresh-fixture-code"
        if failure == "authenticated_identity_mismatch":
            raise AuthError(failure)
        api.headers.update({"Authorization": "Bearer private-new-access", "miToken": "private-new-mi"})
        receipt.update(authenticated_endpoint_http_status=200, api_session_verified=True)

    monkeypatch.setattr(http_login, "verify_api", verify)
    receipt = {}
    if failure is None:
        http_login.renew(original, "00000000000", receipt=receipt)
        assert original.headers["Authorization"] == "Bearer private-new-access"
        assert original.headers["miToken"] == "private-new-mi"
        assert original.cookies.get("fixture_sso") == "private-rotated-cookie"
        assert receipt["success"] and receipt["access_token_changed"]
    else:
        code = "renewal_runtime_error" if failure == "runtime_error" else failure
        with pytest.raises(AuthError, match="^" + code + "$"):
            http_login.renew(original, "00000000000", receipt=receipt)
        assert dict(original.headers) == before_headers
        assert original.cookies.get_dict() == before_cookies
        assert not receipt["success"]
    browser.assert_not_called()
    assert receipt["http_password_posts"] == receipt["browser_password_posts"] == 0
    assert "private" not in json.dumps(receipt)
    original.close()


def test_http_renewal_requires_sso_context_before_network(monkeypatch):
    with requests.Session() as session:
        session.headers.update(Authorization="Bearer fixture", miToken="fixture")
        factory = Mock()
        monkeypatch.setattr(http_login.requests, "Session", factory)
        with pytest.raises(AuthError, match="sso_session_missing"):
            http_login.renew(session, "00000000000")
        factory.assert_not_called()


@pytest.mark.parametrize("url", ["http://sso.acesso.gov.br/login",
    "https://sso.acesso.gov.br.evil.invalid/login", "https://sso.acesso.gov.br:444/login",
    "https://private@sso.acesso.gov.br/login"])
def test_handoff_does_not_capture_untrusted_requests(url):
    route = Mock(request=SimpleNamespace(url=url, method="POST", post_data="token=" + "A" * 40))
    capture = CaptchaHandoff()
    capture.intercept(route)
    assert capture.captured_at is None
    route.continue_.assert_called_once()


def test_failed_post_cannot_reuse_captcha():
    capture = CaptchaHandoff()
    capture.body, capture.url = "token=" + "A" * 40, "https://sso.acesso.gov.br/login"
    session = Mock()
    session.post.side_effect = requests.Timeout("fixture-secret")
    with pytest.raises(requests.Timeout):
        capture.consume(session)
    assert capture.body is None
    with pytest.raises(AuthError, match="already_consumed"):
        capture.consume(session)
    session.post.assert_called_once()


def test_password_form_required_after_captcha():
    response = SimpleNamespace(url="https://sso.acesso.gov.br/login", status_code=200,
                               text="<html>captcha invÃ¡lido</html>")
    with pytest.raises(AuthError, match="captcha_http_not_accepted"):
        password_page(Mock(), response)


@pytest.mark.parametrize('deferred_capture', [False, True])
@pytest.mark.parametrize('optimized',[False,True])
@pytest.mark.parametrize('batch',[False,True])
@pytest.mark.parametrize('failure_stage', [None, 'http_consume_captcha', 'http_password', 'http_oauth_redirect'])
def test_integrated_login_closes_browser_before_http_password(monkeypatch, deferred_capture,optimized,batch,
                                                            failure_stage):
    events = []
    page, browser, manager = MagicMock(), MagicMock(), Mock()
    manager.start.return_value = "http://127.0.0.1:1234"
    manager.stop.side_effect = lambda: events.append("manager_stopped")
    browser.contexts = [SimpleNamespace(pages=[page])]
    browser.version = "fixture-version"
    browser.close.side_effect = lambda: events.append("browser_closed")
    pw = MagicMock()
    pw.__enter__.return_value.chromium.connect_over_cdp.return_value = browser
    monkeypatch.setattr(http_login, "sync_playwright", lambda: pw)
    monkeypatch.setattr(http_login, "ManagedBrowser", lambda *a, **kw: manager)
    monkeypatch.setattr(http_login.browser_login, "goto_login", lambda p: None)
    monkeypatch.setattr(http_login.browser_login, "submit_cpf", lambda *a,**kw: None)
    monkeypatch.setattr(http_login.hcaptcha, "vis_modal", lambda p: True)
    monkeypatch.setattr(http_login, "adopt", lambda *a: None)
    prefetched=Mock()
    prefetched.statistics.return_value={'enabled':True,'model_calls':0}
    if batch:
        from captcha import prefetch
        monkeypatch.setattr(prefetch,'PrefetchedSolver',lambda transport:prefetched)
        monkeypatch.setattr(http_login.requests,'Session',MagicMock())

    def capture():
        callback = page.route.call_args.args[1]
        callback(Mock(request=SimpleNamespace(url="https://sso.acesso.gov.br/login", method="POST",
            post_data="token=" + "A" * 40, headers={})))

    def solve(*args, **kwargs):
        if not batch:
            assert kwargs["vision_session"] is not session
            assert not kwargs["vision_session"].headers.get("Authorization")
        if deferred_capture:
            page.wait_for_selector.side_effect = lambda *a, **kw: capture()
        else:
            capture()
        return True, {"round": 0, "solve_s": 1}

    monkeypatch.setattr(http_login.hcaptcha, "solve_round", solve)
    failure = SimpleNamespace(status_code=503, url="https://sso.acesso.gov.br/login",
                              text="fixture-secret-response")
    if failure_stage != "http_consume_captcha":
        monkeypatch.setattr(http_login, "password_page", lambda *a: Mock())
    auth = Mock(authorization_code="fixture-code")
    auth.finish.return_value = [("https://meu.inss.gov.br/", 200)]
    if failure_stage == "http_oauth_redirect":
        auth.finish.side_effect = lambda: GovBrAuth._check_response(failure)

    def password(*args, **kwargs):
        assert events == ["browser_closed", "manager_stopped"]
        assert kwargs == {"captcha_required": False}
        events.append("http_password")
        if failure_stage == "http_password":
            GovBrAuth._check_response(failure)

    auth.submit_password.side_effect = password
    monkeypatch.setattr(http_login, "GovBrAuth", lambda *a, **kw: auth)
    monkeypatch.setattr(http_login, "verify_api", lambda *a: a[-1].update(api_session_verified=True))
    session = Mock(headers={}, cookies={})
    session.post.return_value = failure if failure_stage == "http_consume_captcha" else Response()
    if failure_stage:
        receipt = {}
        with pytest.raises(AuthError, match="^http_error$"):
            http_login.login(session, "00000000000", "fixture-password", "fixture-key", receipt=receipt,
                            optimize_interactions=optimized, batch_captcha=batch)
        assert receipt["failure_stage"] == failure_stage
        assert receipt["failure_http_status"] == 503
        assert receipt["http_password_posts"] == (failure_stage != "http_consume_captcha")
        assert auth.submit_password.call_count == receipt["http_password_posts"]
        assert auth.finish.call_count == (failure_stage == "http_oauth_redirect")
        assert "fixture-secret-response" not in json.dumps(receipt)
        session.post.assert_called_once()
        return
    receipt = http_login.login(session, "00000000000", "fixture-password", "fixture-key",optimize_interactions=optimized,batch_captcha=batch)
    assert receipt["success"] and receipt["api_session_verified"]
    assert receipt["browser_password_posts"] == 0 and receipt["http_password_posts"] == 1
    session.post.assert_called_once()
    assert events[-1] == "http_password"
    manager.start.assert_called_once_with(wait_ready=True)
    assert receipt['interaction_profile']==('optimized' if optimized else 'reference')
    if batch:
        prefetched.attach.assert_called_once_with(page)
        prefetched.close.assert_called_once()
        assert receipt['captcha_batch']['enabled']


@pytest.mark.parametrize("error,code", [(AuthError("two_factor_required"), "two_factor_required"),
    (requests.Timeout("fixture-secret"), "network_error"),
    (RuntimeError("fixture-secret"), "login_runtime_error")])
def test_cli_errors_never_log_secrets(monkeypatch, tmp_path, capsys, error, code):
    import sys
    monkeypatch.setattr(sys, "argv", ["inss-login-http", "--out", str(tmp_path)])
    monkeypatch.setattr(http_login, "load_env_file", lambda *a: {"GOVBR_CPF": "00000000000",
        "GOVBR_PASSWORD": "fixture-secret", "OPENROUTER_API_KEY": "fixture-key"})
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(http_login, "login", fail)
    assert http_login.main() == 3
    text = (tmp_path / "receipt.json").read_text(encoding="utf-8")
    assert json.loads(text)["code"] == code
    assert "fixture-secret" not in text + capsys.readouterr().out


def test_cli_rejects_secret_arguments_without_echo(monkeypatch, capsys):
    import sys
    monkeypatch.setattr(sys, "argv", ["inss-login-http", "--password", "fixture-secret"])
    with pytest.raises(SystemExit):
        http_login.main()
    assert "fixture-secret" not in capsys.readouterr().err


@pytest.mark.parametrize('fast',[False,True])
@pytest.mark.parametrize('batch',[False,True])
def test_docker_launcher_mounts_credentials_readonly_without_values(monkeypatch, tmp_path,fast,batch):
    import importlib.util
    from pathlib import Path
    import sys
    script = Path(__file__).parents[1] / "scripts/govbr_http_docker.py"
    spec = importlib.util.spec_from_file_location("http_docker_test", script)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    env = tmp_path / ".env"
    env.write_text("GOVBR_PASSWORD=fixture-secret", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["govbr_http_docker.py", "--env-file", str(env),
                                    "--out", str(tmp_path / "out")]+(['--fast-interactions'] if fast else [])
                                    +(['--batch-captcha'] if batch else []))
    monkeypatch.setattr(launcher.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(launcher.os, "getgid", lambda: 1000, raising=False)
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.main() == 0
    command = run.call_args.args[0]
    assert any("dst=/run/credentials.env,readonly" in arg for arg in command)
    assert "fixture-secret" not in repr(command)
    assert "--env" not in command and "--env-file" not in command[:command.index("inss-login-http")]
    assert "--read-only" in command and "--publish" not in command
    assert ('--fast-interactions' in command)==fast
    assert ('--batch-captcha' in command)==batch


def test_solver_accepts_explicit_key_without_modifying_environment(monkeypatch):
    from captcha import solver
    from PIL import Image
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-environment-key")
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": "[1]"}}]}
    post = Mock(return_value=response)
    monkeypatch.setattr(solver.requests, "post", post)
    image = Image.new("RGB", (2, 2), "black")
    assert solver.solve(image, [image] * 9, "test", api_key="fixture-explicit-key") == [1]
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer fixture-explicit-key"
    assert solver.os.environ["OPENROUTER_API_KEY"] == "fixture-environment-key"


def test_library_errors_hide_browser_payloads(monkeypatch):
    def fail():
        raise http_login.PlaywrightError("fixture-secret-in-browser-error")
    monkeypatch.setattr(http_login, "sync_playwright", fail)
    with requests.Session() as session, pytest.raises(AuthError, match="^browser_error$") as error:
        http_login.login(session, "00000000000", "fixture-password", "fixture-key")
    assert error.value.__suppress_context__


def test_unconfirmed_selection_stops_without_reclick_or_password(monkeypatch):
    import pytest
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    from inss import http_login
    from auth.govbr import AuthError
    page,browser,manager=MagicMock(),MagicMock(),Mock()
    browser.contexts=[SimpleNamespace(pages=[page])]
    pw=MagicMock()
    pw.__enter__.return_value.chromium.connect_over_cdp.return_value=browser
    monkeypatch.setattr(http_login,'sync_playwright',lambda:pw)
    monkeypatch.setattr(http_login,'ManagedBrowser',lambda *a,**kw:manager)
    monkeypatch.setattr(http_login.browser_login,'goto_login',Mock())
    monkeypatch.setattr(http_login.browser_login,'submit_cpf',Mock())
    monkeypatch.setattr(http_login.hcaptcha,'vis_modal',lambda p:True)
    solve=Mock(return_value=(False,{'why':'selection_not_confirmed'}))
    monkeypatch.setattr(http_login.hcaptcha,'solve_round',solve)
    session=Mock(headers={},cookies={})
    with pytest.raises(AuthError,match='captcha_selection_not_confirmed'):
        http_login.login(session,'00000000000','fixture-password','fixture-key')
    solve.assert_called_once()
    session.post.assert_not_called()
    browser.close.assert_called_once()
    manager.stop.assert_called_once()
