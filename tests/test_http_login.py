import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

import pytest
import requests

from auth.handoff import CaptchaHandoff, password_page
from auth.govbr import AuthError
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
def test_integrated_login_closes_browser_before_http_password(monkeypatch, deferred_capture,optimized):
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

    def capture():
        callback = page.route.call_args.args[1]
        callback(Mock(request=SimpleNamespace(url="https://sso.acesso.gov.br/login", method="POST",
            post_data="token=" + "A" * 40, headers={})))

    def solve(*args, **kwargs):
        if deferred_capture:
            page.wait_for_selector.side_effect = lambda *a, **kw: capture()
        else:
            capture()
        return True, {"round": 0, "solve_s": 1}

    monkeypatch.setattr(http_login.hcaptcha, "solve_round", solve)
    monkeypatch.setattr(http_login, "password_page", lambda *a: Mock())
    auth = Mock(authorization_code="fixture-code")
    auth.finish.return_value = [("https://meu.inss.gov.br/", 200)]

    def password(*args, **kwargs):
        assert events == ["browser_closed", "manager_stopped"]
        assert kwargs == {"captcha_required": False}
        events.append("http_password")

    auth.submit_password.side_effect = password
    monkeypatch.setattr(http_login, "GovBrAuth", lambda *a, **kw: auth)
    monkeypatch.setattr(http_login, "verify_api", lambda *a: a[-1].update(api_session_verified=True))
    session = Mock(headers={}, cookies={})
    session.post.return_value = Response()
    receipt = http_login.login(session, "00000000000", "fixture-password", "fixture-key",optimize_interactions=optimized)
    assert receipt["success"] and receipt["api_session_verified"]
    assert receipt["browser_password_posts"] == 0 and receipt["http_password_posts"] == 1
    session.post.assert_called_once()
    assert events[-1] == "http_password"
    manager.start.assert_called_once_with(wait_ready=True)
    assert receipt['interaction_profile']==('optimized' if optimized else 'reference')


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
def test_docker_launcher_mounts_credentials_readonly_without_values(monkeypatch, tmp_path,fast):
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
                                    "--out", str(tmp_path / "out")]+(['--fast-interactions'] if fast else []))
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
