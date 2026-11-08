"""Transporte HTTP do SSO gov.br; captcha e browser pertencem ao chamador."""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from .captcha import CaptchaProvider
from .form import find_form

AUTHZ = "https://sso.acesso.gov.br/authorize"
SSO_ORIGIN = "https://sso.acesso.gov.br"
DEFAULT_SCOPE = "openid profile email phone govbr_confiabilidades"
REDIRECTS = (301, 302, 303, 307, 308)
INVALID_MSG = re.compile(r"Usu[aá]rio e/ou senha inv[aá]lidos|\bERL0003900\b", re.I)
TWO_FACTOR = re.compile(r'<input\b[^>]*\b(?:name|id)=["\']otpInput["\']', re.I)


def safe_url(url: str) -> str:
    """Somente origem/caminho: queries e fragments podem conter credenciais SSO."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))


class AuthError(RuntimeError):
    def __init__(self, code: str, evidence: str = "", *, http_status: int | None = None):
        # Não guardar HTML, URLs, corpos ou mensagens arbitrárias do servidor.
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class TwoFactorRequired(AuthError):
    def __init__(self, evidence: str = ""):
        super().__init__("two_factor_required")


@dataclass
class LoginPage:
    url: str = field(repr=False)
    html: str = field(repr=False)

    @property
    def kind(self) -> str:
        if TWO_FACTOR.search(self.html):
            return "two_factor"
        if find_form(self.html, has_field="password"):
            return "password"
        if find_form(self.html, has_field="accountId"):
            return "account"
        return "unknown"


class GovBrAuth:
    def __init__(self, session, captcha: CaptchaProvider, *,
                 client_id="comprasnet.gov.br",
                 redirect_uri="https://www.comprasnet.gov.br/seguro/landing_sso.asp",
                 scope=DEFAULT_SCOPE, timeout=30):
        self.s = session
        self.captcha = captcha
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.timeout = timeout
        self.authorization_id: str | None = None
        self.authorization_code: str | None = None
        self.page: LoginPage | None = None
        self.state: str | None = None
        self._next_location = ""
        self._login_url = ""
        if self.s.headers.get("User-Agent", "").startswith("python-requests/"):
            del self.s.headers["User-Agent"]
        self.s.headers.setdefault("User-Agent", (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"))
        self.s.headers.setdefault("Accept-Language", "pt-BR,pt;q=0.9,en;q=0.8")
        self._check_url(self.redirect_uri, sso_only=False)

    def _check_url(self, url, *, sso_only=True):
        parts = urlsplit(url)
        allowed = {urlsplit(SSO_ORIGIN).netloc}
        if not sso_only:
            allowed.add(urlsplit(self.redirect_uri).netloc)
        if (parts.scheme != "https" or parts.netloc not in allowed
                or parts.username or parts.password):
            raise AuthError("untrusted_url")
        return url

    def _get(self, url):
        return self.s.get(url, timeout=self.timeout, allow_redirects=False)

    def _post_form(self, page, form, data):
        if form.method != "post":
            raise AuthError("unsupported_form_method")
        url = self._check_url(urljoin(page.url, form.action))
        return self.s.post(url, data=data, timeout=self.timeout, allow_redirects=False,
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Origin": SSO_ORIGIN, "Referer": page.url})

    @staticmethod
    def _check_response(resp):
        status = resp.status_code
        if status != 200 and status not in REDIRECTS:
            code = {403: "http_forbidden", 429: "rate_limited"}.get(status, "http_error")
            raise AuthError(code, http_status=status)

    def _read_page(self, resp, fallback_url):
        self._check_response(resp)
        self.page = LoginPage(getattr(resp, "url", "") or fallback_url, resp.text or "")
        if INVALID_MSG.search(self.page.html):
            raise AuthError("invalid_credentials")
        if self.page.kind == "two_factor":
            raise TwoFactorRequired()
        return self.page

    def authorize_url(self) -> str:
        self.authorization_id = None
        self.authorization_code = None
        self.page = None
        self._next_location = ""
        self._login_url = ""
        self.state = secrets.token_urlsafe(32)
        return AUTHZ + "?" + urlencode({
            "response_type": "code", "client_id": self.client_id,
            "scope": self.scope.replace("+", " "), "redirect_uri": self.redirect_uri,
            "state": self.state, "nonce": secrets.token_urlsafe(32),
        })

    def start(self) -> str:
        url = self.authorize_url()
        r = self._get(url)
        self._check_response(r)
        loc = r.headers.get("Location", "")
        ids = parse_qs(urlsplit(loc).query).get("authorization_id", [])
        if r.status_code not in REDIRECTS or len(ids) != 1 or not ids[0]:
            raise AuthError("authorize_failed")
        self._login_url = self._check_url(urljoin(url, loc))
        self.authorization_id = ids[0]
        return self.authorization_id

    def resume(self, max_hops: int = 8):
        """Obtain a fresh code using existing SSO cookies, without submitting forms."""
        url = self.authorize_url()
        response = self._get(url)
        self._check_response(response)
        location = response.headers.get("Location", "")
        if response.status_code not in REDIRECTS:
            self._read_page(response, url)
            raise AuthError("sso_login_required" if self.page.kind in ("account", "password")
                            else "sso_interaction_required")
        if not location:
            raise AuthError("redirect_missing")
        self._next_location = urljoin(url, location)
        try:
            return self.finish(max_hops=max_hops)
        except AuthError as error:
            if error.code == "login_not_completed":
                raise AuthError("sso_login_required" if self.page and self.page.kind in ("account", "password")
                                else "sso_interaction_required") from None
            raise

    def open_login(self) -> LoginPage:
        if not self.authorization_id:
            self.start()
        page = self._read_page(self._get(self._login_url), self._login_url)
        if page.kind == "unknown":
            raise AuthError("login_page_unrecognized")
        return page

    def submit_account(self, cpf: str) -> LoginPage:
        page = self.page or self.open_login()
        self._next_location = ""
        form = find_form(page.html, has_field="accountId")
        if form is None:
            raise AuthError("account_form_missing")
        r = self._post_form(page, form, form.payload(accountId=cpf))
        if r.status_code in REDIRECTS:
            loc = r.headers.get("Location", "")
            if not loc:
                raise AuthError("password_form_missing")
            url = self._check_url(urljoin(page.url, loc))
            r = self._get(url)
        else:
            url = page.url
        page = self._read_page(r, url)
        if page.kind != "password":
            raise AuthError("password_form_missing")
        return page

    def submit_password(self, password: str, cpf: str | None = None, *,
                        captcha_required: bool = True) -> LoginPage:
        self._next_location = ""
        page = self.page or self.open_login()
        if INVALID_MSG.search(page.html):
            raise AuthError("invalid_credentials")
        if page.kind == "two_factor":
            raise TwoFactorRequired()
        form = find_form(page.html, has_field="password")
        if form is None:
            raise AuthError("password_form_missing")
        # Validar destino antes de consumir o token de uso único.
        self._check_url(urljoin(page.url, form.action))
        overrides = {"password": password}
        if captcha_required:
            token = self.captcha.solve(None, page.url, None)
            if not token:
                raise AuthError("captcha_token_missing")
            overrides["h-captcha-response"] = token
        if cpf:
            overrides["accountId"] = cpf
        r = self._post_form(page, form, form.payload(**overrides))
        self._read_page(r, page.url)
        loc = r.headers.get("Location", "")
        if r.status_code not in REDIRECTS or not loc:
            raise AuthError("password_not_accepted")
        self._next_location = urljoin(self.page.url, loc)
        return self.page

    def finish(self, max_hops: int = 8):
        """Exige callback OAuth com code/state e HTTP 200 no portal configurado.

        Isso valida o transporte SSO; não comprova sessão utilizável em APIs nem
        executa JavaScript de aplicações como Meu INSS.
        """
        self.authorization_code = None
        loc, self._next_location = self._next_location, ""
        hops = []
        callback_code = None
        target = urlsplit(self.redirect_uri)
        for _ in range(max_hops):
            if not loc:
                break
            self._check_url(loc, sso_only=False)
            parts = urlsplit(loc)
            query = parse_qs(parts.query)
            if parts.netloc == target.netloc and parts.path == target.path:
                if "error" in query:
                    raise AuthError("oauth_error")
                if query.get("code"):
                    if self.state is None or query.get("state") != [self.state]:
                        raise AuthError("state_mismatch")
                    if len(query["code"]) != 1:
                        raise AuthError("oauth_callback_ambiguous")
                    callback_code = query["code"][0]
            r = self._get(loc)
            hops.append((safe_url(loc), r.status_code))
            page = self._read_page(r, loc)
            if r.status_code == 200:
                if callback_code and parts.netloc == target.netloc and page.kind == "unknown":
                    self.authorization_code = callback_code
                    return hops
                raise AuthError("login_not_completed")
            next_loc = r.headers.get("Location", "")
            if not next_loc:
                raise AuthError("redirect_missing")
            loc = urljoin(loc, next_loc)
        raise AuthError("redirect_limit" if loc else "login_not_completed")

    def cookies(self) -> dict[str, str]:
        return self.s.cookies.get_dict()
