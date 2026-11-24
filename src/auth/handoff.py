"""Retain the browser's protected POST; consume its exact body once over HTTP."""
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urljoin, urlsplit

from .govbr import AuthError, GovBrAuth, LoginPage, REDIRECTS
from .page_diagnostics import classify_page, is_upstream_rejection_page

SSO = "sso.acesso.gov.br"
HANDOFF_SELECTOR = "[data-auth-handoff]"


def trusted_sso(url):
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc == SSO


class CaptchaHandoff:
    def __init__(self, native=False):
        self.native = native
        self.body = None
        self.url = None
        self.headers = {}
        self.captured_at = None
        self.consumed = False
        self.observed = False
        self.body_bytes = None
        self.field_names = []

    def intercept(self, route):
        request = route.request
        if not trusted_sso(request.url) or request.method != "POST":
            route.continue_()
            return
        body = request.post_data or ""
        fields = parse_qs(body, keep_blank_values=True)
        if not any(len(fields.get(key, [""])[0]) > 20 for key in
                   ("h-captcha-response", "g-recaptcha-response", "token")):
            route.continue_()
            return
        # Diagnostic native transport: the browser keeps ownership of the POST.
        if self.native:
            if self.observed:
                route.abort()
                return
            self.observed = True
            self.url = request.url
            self.headers = request.headers
            self.captured_at = time.monotonic()
            self.body_bytes = len(body.encode("utf-8"))
            self.field_names = sorted(fields)
            route.continue_()
            return
        if self.body is not None or self.consumed:
            route.abort()
            return
        self.body, self.url = body, request.url
        self.headers = request.headers
        self.captured_at = time.monotonic()
        self.body_bytes = len(body.encode("utf-8"))
        self.field_names = sorted(fields)
        # This LOCAL marker proves capture only; the server has not accepted it.
        route.fulfill(status=200, content_type="text/html",
                      body="<main data-auth-handoff>HTTP handoff ready.</main>")

    def consume(self, session):
        if self.consumed or self.body is None:
            raise AuthError("token_unavailable_or_already_consumed")
        if not trusted_sso(self.url):
            raise AuthError("untrusted_url")
        self.consumed = True
        allowed = {"accept", "accept-language", "content-type", "origin", "referer"}
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() in allowed or key.lower().startswith("sec-")}
        try:
            return session.post(self.url, data=self.body.encode("utf-8"), headers=headers,
                                timeout=30, allow_redirects=False)
        finally:
            self.body = self.url = None
            self.headers.clear()


def consume_with_curl(captured, session, *, impersonate="safari17_0", timeout=30):
    """Replay the captured POST with a browser TLS/HTTP2 fingerprint instead of urllib3."""
    if captured.consumed or captured.body is None:
        raise AuthError("token_unavailable_or_already_consumed")
    if not trusted_sso(captured.url):
        raise AuthError("untrusted_url")
    try:
        from curl_cffi import requests as browser_http
    except ImportError:
        raise AuthError("curl_transport_unavailable") from None
    captured.consumed = True
    allowed = {"accept", "accept-language", "content-type", "origin", "referer"}
    headers = {key: value for key, value in captured.headers.items()
               if key.lower() in allowed or key.lower().startswith("sec-")}
    headers["Cookie"] = "; ".join(f"{cookie.name}={cookie.value}" for cookie in session.cookies)
    client = browser_http.Session(impersonate=impersonate)
    try:
        response = client.post(captured.url, data=captured.body.encode("utf-8"),
                               headers=headers, timeout=timeout, allow_redirects=False)
    finally:
        captured.body = captured.url = None
        captured.headers.clear()
        client.close()
    for cookie in getattr(getattr(response, "cookies", None), "jar", None) or []:
        session.cookies.set(cookie.name, cookie.value, domain=cookie.domain,
                            path=cookie.path or "/", secure=True)
    return SimpleNamespace(status_code=response.status_code, url=response.url or "",
                           text=response.text, content=response.content, headers=response.headers)


def password_page(session, response, *, diagnostics=None):
    for hop in range(5):
        if not trusted_sso(response.url):
            raise AuthError("untrusted_url")
        if response.status_code == 400:
            diagnostic = classify_page(response.text, response.url, '', status=400, hops=hop)
            if diagnostics is not None:
                diagnostics['captcha_terminal'] = diagnostic
            if diagnostic['kind'] == 'captcha_error':
                raise AuthError('captcha_http_not_accepted', http_status=400)
        GovBrAuth._check_response(response)
        page = LoginPage(response.url, response.text)
        diagnostic = classify_page(page.html, page.url, '', known_kind=page.kind,
                                   status=response.status_code, hops=hop)
        if page.kind == "two_factor" or diagnostic['kind'] == 'two_factor':
            if diagnostics is not None:
                diagnostics['captcha_terminal'] = diagnostic
            raise AuthError("two_factor_required")
        if diagnostic['kind'] in ('consent', 'account_update'):
            if diagnostics is not None:
                diagnostics['captcha_terminal'] = diagnostic
            raise AuthError('govbr_interaction_required')
        if diagnostic['kind'] == 'captcha_error':
            if diagnostics is not None:
                diagnostics['captcha_terminal'] = diagnostic
            raise AuthError('captcha_http_not_accepted')
        if response.status_code == 200 and is_upstream_rejection_page(diagnostic):
            if diagnostics is not None:
                diagnostics['captcha_terminal'] = diagnostic
            raise AuthError('upstream_request_rejected', http_status=response.status_code)
        if page.kind == "password":
            return page
        if response.status_code not in REDIRECTS:
            if diagnostics is not None:
                diagnostics['captcha_terminal'] = diagnostic
            raise AuthError("captcha_http_not_accepted")
        location = response.headers.get("Location")
        if not location:
            raise AuthError("captcha_redirect_missing")
        target = urljoin(response.url, location)
        if not trusted_sso(target):
            raise AuthError("unexpected_captcha_redirect")
        response = session.get(target, timeout=30, allow_redirects=False)
    raise AuthError("captcha_redirect_limit")
