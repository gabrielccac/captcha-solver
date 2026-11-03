"""Testes da ponte browser -> HTTP, com página falsa (sem browser real)."""
import pytest

from auth.browser_captcha import BrowserCaptcha, adopt, read_token


class FakePage:
    def __init__(self, tokens, html="<html><form id='f'></form></html>"):
        self.tokens = list(tokens)
        self.html = html
        self.waits = 0
        self.context = type("Ctx", (), {"cookies": lambda self: [
            {"name": "sessao", "value": "xyz", "domain": ".acesso.gov.br", "path": "/"}
        ]})()

    def evaluate(self, js):
        if "outerHTML" in js:
            return self.html
        if "navigator.userAgent" in js:
            return "browser-test-user-agent"
        return self.tokens.pop(0) if len(self.tokens) > 1 else (self.tokens[0] if self.tokens else "")

    def wait_for_timeout(self, ms):
        self.waits += 1

    def goto(self, url, **kw):
        raise AssertionError("Não navegar ao consumir um token já resolvido")


class FakeSession:
    def __init__(self):
        self.cookies = type("Jar", (), {"set": self._set, "get_dict": lambda s: s.store})()
        self.store = {}
        self.headers = {}
        self.cookie_options = {}

    def _set(self, name, value, **options):
        self.store[name] = value
        self.cookie_options[name] = options


def test_le_token_apos_espera():
    page = FakePage(["", "", "TOKEN-1"])
    assert read_token(page, timeout_ms=5000, poll_ms=500) == "TOKEN-1"
    assert page.waits == 2


def test_browser_captcha_chama_mint():
    page = FakePage(["TOKEN-2"])
    chamou = []
    cap = BrowserCaptcha(page, mint=lambda p: chamou.append(p))
    assert cap.solve(sitekey="k", url="https://sso.acesso.gov.br/login") == "TOKEN-2"
    assert chamou == [page]
    assert cap.solve() == ""
    assert chamou == [page]


def test_adopt_transfere_cookies():
    page = FakePage(["t"])
    s = FakeSession()
    html = adopt(page, s)
    assert s.store == {"sessao": "xyz"}
    assert "<form" in html
    assert s.headers["User-Agent"] == "browser-test-user-agent"


def test_timeout_devolve_vazio():
    page = FakePage([""])
    assert read_token(page, timeout_ms=1000, poll_ms=500) == ""


def test_timeout_zero_does_not_wait():
    page = FakePage([""])
    assert read_token(page, timeout_ms=0) == ""
    assert page.waits == 0


@pytest.mark.parametrize(("timeout", "poll"), [(-1, 500), (100, 0), (100, -1)])
def test_invalid_timeouts_rejected(timeout, poll):
    with pytest.raises(ValueError):
        read_token(FakePage([""]), timeout_ms=timeout, poll_ms=poll)


def test_adopt_preserves_secure_cookie_scope_and_browser_user_agent():
    from requests import Session
    from auth.captcha import StaticToken
    from auth.govbr import GovBrAuth

    page = FakePage(["fixture"])
    page.context.cookies = lambda: [{"name": "fixture-session", "value": "fixture-value",
                                    "domain": "sso.acesso.gov.br", "path": "/login",
                                    "secure": True, "expires": 2_000_000_000}]
    with Session() as session:
        adopt(page, session)
        GovBrAuth(session, StaticToken("fixture"))
        cookie = next(iter(session.cookies))
        assert (cookie.domain, cookie.path, cookie.secure, cookie.expires) == (
            "sso.acesso.gov.br", "/login", True, 2_000_000_000)
        assert session.headers["User-Agent"] == "browser-test-user-agent"
