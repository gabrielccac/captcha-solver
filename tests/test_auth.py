"""Testes offline do transporte de autenticação (sem rede, sem credencial)."""
import pytest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from auth.captcha import StaticToken
from auth.form import find_form
from auth.govbr import AuthError, GovBrAuth, LoginPage, TwoFactorRequired

ACCOUNT_HTML = """
<form id="login" action="/login" method="post">
  <input type="hidden" name="_csrf" value="csrf-1">
  <input name="accountId" type="tel">
  <button type="submit" name="operation" value="enter-account-id">Continuar</button>
</form>
"""

PASSWORD_HTML = """
<form id="login" action="/login" method="post">
  <input type="hidden" name="_csrf" value="csrf-2">
  <input type="hidden" name="accountId" value="***.***.***-**">
  <input type="hidden" name="token" value="tok-9">
  <input type="password" name="password">
  <button type="submit" name="operation" value="enter-password">Continuar</button>
</form>
"""

INVALID_HTML = """
<form action="/login" method="post">
  <input type="hidden" name="_csrf" value="c3">
  <input name="accountId" type="tel">
  <div class="br-message">Usuário e/ou senha inválidos. (ERL0003900)</div>
</form>
"""

TWO_FACTOR_HTML = """
<h1>Confirme sua identidade com a Verificação em duas etapas</h1>
<input id="otpInput" name="otpInput" type="text">
"""


class Resp:
    def __init__(self, status=200, text="", headers=None, url=""):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = url


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []
        self.cookies = self

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if urlsplit(url).path == "/authorize":
            self.state = parse_qs(urlsplit(url).query)["state"][0]
        return self.routes(url, "GET", None)

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, data))
        return self.routes(url, "POST", data)

    def get_dict(self):
        return {"sessao": "abc"}


def make(session, token="tok"):
    return GovBrAuth(session, StaticToken(token))


def routes(url, method, data):
    if "authorize" in url:
        return Resp(302, headers={"Location": "https://sso.acesso.gov.br/login?authorization_id=abc123"})
    if method == "GET" and "/login" in url:
        return Resp(200, ACCOUNT_HTML, url=url)
    if method == "POST" and data and "operation" in data:
        if data["operation"] == "enter-account-id":
            return Resp(200, PASSWORD_HTML, url="https://sso.acesso.gov.br/login")
        return Resp(302, headers={"Location": "https://www.comprasnet.gov.br/seguro/landing_sso.asp?code=XYZ"})
    if "landing_sso" in url:
        return Resp(200, "<html>ok</html>")
    if "intro" in url:
        return Resp(200, "<html>ok</html>")
    return Resp(404, "")


def test_fluxo_completo():
    s = FakeSession(routes)
    a = make(s)
    assert a.start() == "abc123"

    page = a.open_login()
    assert page.kind == "account"

    page = a.submit_account("12345678901")
    assert page.kind == "password"

    a.submit_password("senha-falsa")
    a._next_location += "&state=" + a.state
    hops = a.finish()
    assert any("landing_sso" in h[0] for h in hops)
    assert a.cookies() == {"sessao": "abc"}


def test_resume_uses_fresh_authorization_and_no_form_posts():
    def route(url, method, data):
        if urlsplit(url).path == "/authorize":
            return Resp(302, headers={"Location": "https://meu.inss.gov.br/?code=fresh&state=" + s.state})
        return Resp(200, "<html>portal</html>", url=url)
    s = FakeSession(route)
    a = GovBrAuth(s, StaticToken("unused"), client_id="autorizar.meu.inss.gov.br",
                  redirect_uri="https://meu.inss.gov.br/")
    a.resume()
    state = a.state
    assert a.authorization_code == "fresh"
    a.resume()
    assert a.state != state
    assert all(call[0] == "GET" for call in s.calls)


@pytest.mark.parametrize("html,code", [(ACCOUNT_HTML, "sso_login_required"),
    (PASSWORD_HTML, "sso_login_required"), ("<html>consent</html>", "sso_interaction_required"),
    (TWO_FACTOR_HTML, "two_factor_required")])
def test_resume_stops_when_interaction_is_required(html, code):
    def route(url, method, data):
        if urlsplit(url).path == "/authorize":
            return Resp(302, headers={"Location": "/login?authorization_id=fixture"})
        return Resp(200, html, url=url)
    s = FakeSession(route)
    with pytest.raises(AuthError, match="^" + code + "$"):
        make(s).resume()
    assert all(call[0] == "GET" for call in s.calls)


def test_resume_rejects_a_mismatched_callback_state():
    s = FakeSession(lambda *a: Resp(302, headers={"Location":
        "https://www.comprasnet.gov.br/seguro/landing_sso.asp?code=fresh&state=wrong"}))
    with pytest.raises(AuthError, match="state_mismatch"):
        make(s).resume()
    assert len(s.calls) == 1


def test_token_e_csrf_sao_enviados():
    s = FakeSession(routes)
    a = make(s, token="token-captcha")
    a.start(); a.open_login(); a.submit_account("12345678901")
    a.submit_password("x")

    post = [c for c in s.calls if c[0] == "POST" and c[2].get("operation") == "enter-password"][0]
    assert post[2]["h-captcha-response"] == "token-captcha"
    assert post[2]["_csrf"] == "csrf-2"
    assert post[2]["token"] == "tok-9"


def test_senha_invalida_classificada():
    def r(url, method, data):
        if "authorize" in url:
            return Resp(302, headers={"Location": "/login?authorization_id=abc"})
        if method == "GET":
            return Resp(200, PASSWORD_HTML, url=url)
        return Resp(200, INVALID_HTML, url=url)

    a = make(FakeSession(r))
    a.start(); a.open_login()
    with pytest.raises(AuthError) as e:
        a.submit_password("errada")
    assert e.value.code == "invalid_credentials"


def test_dois_fatores_classificado():
    def r(url, method, data):
        if "authorize" in url:
            return Resp(302, headers={"Location": "/login?authorization_id=abc"})
        return Resp(200, TWO_FACTOR_HTML, url=url)

    a = make(FakeSession(r))
    a.start()
    with pytest.raises(TwoFactorRequired):
        a.open_login()


def test_form_replay_preserva_hidden():
    f = find_form(PASSWORD_HTML, has_field="password")
    assert f is not None
    data = f.payload(password="p", **{"h-captcha-response": "t"})
    assert data["token"] == "tok-9"
    assert data["operation"] == "enter-password"
    assert data["password"] == "p"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests

    def denied(*args, **kwargs):
        pytest.fail("Os testes de autenticação não podem acessar a rede")

    monkeypatch.setattr(requests.sessions.Session, "request", denied)


def password_auth(response):
    s = FakeSession(lambda *args: response)
    a = make(s)
    a.page = LoginPage("https://sso.acesso.gov.br/login?authorization_id=fixture", PASSWORD_HTML)
    return a, s


@pytest.mark.parametrize(("response", "code"), [
    (Resp(403, "erro secreto"), "http_forbidden"),
    (Resp(429), "rate_limited"),
    (Resp(500), "http_error"),
    (Resp(400, INVALID_HTML), "invalid_credentials"),
    (Resp(400, TWO_FACTOR_HTML), "two_factor_required"),
    (Resp(400, "<p>Captcha inválido.</p>"), "captcha_invalid"),
    (Resp(400, "<p>fixture-secret desconhecido</p>"), "http_error"),
    (Resp(200, PASSWORD_HTML), "password_not_accepted"),
    (Resp(200, "<h1>Erro</h1>"), "password_not_accepted"),
    (Resp(302), "password_not_accepted"),
    (Resp(200, INVALID_HTML), "invalid_credentials"),
    (Resp(200, TWO_FACTOR_HTML), "two_factor_required"),
])
def test_password_failure_never_succeeds(response, code):
    a, s = password_auth(response)
    with pytest.raises(AuthError, match=code) as error:
        a.submit_password("fixture-password")
    if response.status_code == 400:
        assert error.value.http_status == 400
        assert str(error.value) == code
    with pytest.raises(AuthError, match="login_not_completed"):
        a.finish()
    assert len(s.calls) == 1  # sem retry de senha/token


def test_missing_token_does_not_post():
    a, s = password_auth(Resp())
    a.captcha = StaticToken("")
    with pytest.raises(AuthError, match="captcha_token_missing"):
        a.submit_password("fixture-password")
    assert not s.calls


def test_password_after_browser_account_captcha_does_not_reuse_token():
    a, s = password_auth(Resp(302, headers={"Location": "https://meu.inss.gov.br/"}))

    class AlreadyConsumed:
        def solve(self, *args):
            raise AssertionError("Captcha já consumido na etapa de CPF")

    a.captcha = AlreadyConsumed()
    a.submit_password("fixture-password", captcha_required=False)
    assert "h-captcha-response" not in s.calls[-1][2]
    assert s.calls[-1][2]["_csrf"] == "csrf-2"
    assert s.calls[-1][2]["password"] == "fixture-password"


def test_static_token_is_consumed_once():
    token = StaticToken(" fixture-token ")
    assert token.solve() == "fixture-token"
    assert token.solve() == ""


@pytest.mark.parametrize("action", ["https://example.invalid/login", "http://sso.acesso.gov.br/login",
                                         "https://sso.acesso.gov.br.evil.invalid/login"])
def test_untrusted_form_never_receives_credentials(action):
    a, s = password_auth(Resp())
    a.page.html = PASSWORD_HTML.replace('action="/login"', f'action="{action}"')
    with pytest.raises(AuthError, match="untrusted_url"):
        a.submit_password("fixture-password")
    assert not s.calls
    assert a.captcha.solve() == "tok"  # valida destino antes de consumir token


@pytest.mark.parametrize("method_markup", ['method="get"', ''])
def test_get_form_never_receives_password(method_markup):
    a, s = password_auth(Resp())
    a.page.html = PASSWORD_HTML.replace('method="post"', method_markup)
    with pytest.raises(AuthError, match="unsupported_form_method"):
        a.submit_password("fixture-password")
    assert not s.calls


def test_authorize_encodes_params_and_keeps_exact_login_location():
    login = "/login?authorization_id=abc-123_45&extra=keep"
    s = FakeSession(lambda url, *_: Resp(302, headers={"Location": login})
                    if urlsplit(url).path == "/authorize" else Resp(200, ACCOUNT_HTML, url=url))
    a = GovBrAuth(s, StaticToken("fixture"), redirect_uri="https://www.comprasnet.gov.br/cb?a=1&b=2")
    assert a.start() == "abc-123_45"
    query = parse_qs(urlsplit(s.calls[0][1]).query)
    assert query["redirect_uri"] == [a.redirect_uri]
    assert query["scope"] == ["openid profile email phone govbr_confiabilidades"]
    assert query["state"] == [a.state]
    assert query["nonce"][0] != a.state
    a.open_login()
    assert s.calls[-1][1] == "https://sso.acesso.gov.br" + login


@pytest.mark.parametrize("location", ["", "/login", "/login?authorization_id=", "/login?authorization_id=a&authorization_id=b"])
def test_authorize_requires_one_id(location):
    a = make(FakeSession(lambda *_: Resp(302, headers={"Location": location})))
    with pytest.raises(AuthError, match="authorize_failed"):
        a.start()


def test_cpf_redirect_follows_to_password():
    def route(url, method, data):
        if method == "POST":
            return Resp(303, headers={"Location": "/password?authorization_id=fixture"})
        return Resp(200, PASSWORD_HTML, url=url)

    a = make(FakeSession(route))
    a.page = LoginPage("https://sso.acesso.gov.br/login", ACCOUNT_HTML)
    assert a.submit_account("00000000000").kind == "password"
    assert a.s.calls[-1] == ("GET", "https://sso.acesso.gov.br/password?authorization_id=fixture")


def test_relative_redirects_redact_oauth_code_and_state():
    def route(url, *_):
        if "landing_sso.asp" in url:
            return Resp(302, headers={"Location": "intro.htm?session=fixture-secret#fixture-fragment"})
        return Resp(200, "<html>Portal</html>")

    a = make(FakeSession(route))
    a.state = "fixture-state"
    a._next_location = a.redirect_uri + "?code=fixture-code&state=fixture-state"
    hops = a.finish()
    assert hops == [(a.redirect_uri, 302), ("https://www.comprasnet.gov.br/seguro/intro.htm", 200)]
    assert "fixture-" not in repr(hops)


@pytest.mark.parametrize(("location", "response", "code"), [
    ("https://www.comprasnet.gov.br/seguro/landing_sso.asp?code=fixture-code&state=wrong", Resp(), "state_mismatch"),
    ("https://www.comprasnet.gov.br/seguro/landing_sso.asp?code=fixture-code", Resp(), "state_mismatch"),
    ("https://www.comprasnet.gov.br/seguro/landing_sso.asp?error=access_denied", Resp(), "oauth_error"),
    ("https://www.comprasnet.gov.br/seguro/landing_sso.asp", Resp(), "login_not_completed"),
    ("https://example.invalid/?code=fixture-code", Resp(), "untrusted_url"),
    ("https://sso.acesso.gov.br/login", Resp(200, TWO_FACTOR_HTML), "two_factor_required"),
    ("https://sso.acesso.gov.br/login", Resp(200, INVALID_HTML), "invalid_credentials"),
    ("https://sso.acesso.gov.br/consent", Resp(200, "Autorizar"), "login_not_completed"),
    ("https://sso.acesso.gov.br/login", Resp(302), "redirect_missing"),
])
def test_finish_rejects_incomplete_or_untrusted_flow(location, response, code):
    a = make(FakeSession(lambda *_: response))
    a.state = "fixture-state"
    a._next_location = location
    with pytest.raises(AuthError, match=code):
        a.finish()
    if code in ("state_mismatch", "untrusted_url", "oauth_error"):
        assert not a.s.calls


def test_redirect_loop_stops_without_retrying_password():
    a = make(FakeSession(lambda *_: Resp(302, headers={"Location": "/login"})))
    a._next_location = "https://sso.acesso.gov.br/login"
    with pytest.raises(AuthError, match="redirect_limit"):
        a.finish(max_hops=2)
    assert len(a.s.calls) == 2


def test_adopted_callback_requires_known_expected_state():
    a = make(FakeSession(lambda *_: Resp(200, "Portal")))
    a._next_location = a.redirect_uri + "?code=fixture-code&state=fixture-state"
    with pytest.raises(AuthError, match="state_mismatch"):
        a.finish()
    assert not a.s.calls


@pytest.mark.parametrize(("fixture", "code"), [("govbr-invalid-credentials.html", "invalid_credentials"),
                                               ("govbr-two-factor.html", "two_factor_required")])
def test_sanitized_reference_fixtures(fixture, code):
    html = (Path(__file__).parent / "fixtures" / fixture).read_text(encoding="utf-8")
    a, _ = password_auth(Resp(200, html))
    with pytest.raises(AuthError, match=code):
        a.submit_password("fixture-password")


def test_errors_and_repr_do_not_retain_html_or_query_secrets():
    page = LoginPage("https://sso.acesso.gov.br/?code=fixture-secret", PASSWORD_HTML)
    error = AuthError("password_form_missing", "fixture-secret")
    assert "fixture-secret" not in repr(page) + str(error) + repr(vars(error))
    assert "tok-9" not in repr(find_form(PASSWORD_HTML))


def test_form_replays_only_first_successful_submit():
    html = '''<form><input name="csrf" value="fixture-csrf">
    <input disabled name="disabled" value="omit">
    <button type="button" name="cancel">Cancelar</button>
    <button name="operation" value="login">Entrar</button>
    <input type="submit" name="other" value="omit">
    <input type="reset" name="reset" value="omit">
    <input type="button" name="button" value="omit">
    <input type="checkbox" name="remember" checked>
    <input type="checkbox" name="unchecked">
    </form>'''
    assert find_form(html).payload() == {"csrf": "fixture-csrf", "operation": "login", "remember": "on"}


def test_script_text_does_not_count_as_otp_challenge():
    assert LoginPage("https://sso.acesso.gov.br/login",
                     PASSWORD_HTML + '<script>const name="otpInput";</script>').kind == "password"
