"""Retain the browser's protected POST; consume its exact body once over HTTP."""
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from .govbr import AuthError, GovBrAuth, LoginPage, REDIRECTS

SSO = "sso.acesso.gov.br"
HANDOFF_SELECTOR = "[data-auth-handoff]"


def trusted_sso(url):
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc == SSO


class CaptchaHandoff:
    def __init__(self):
        self.body = None
        self.url = None
        self.headers = {}
        self.captured_at = None
        self.consumed = False

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
        if self.body is not None or self.consumed:
            route.abort()
            return
        self.body, self.url = body, request.url
        self.headers = request.headers
        self.captured_at = time.monotonic()
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


def password_page(session, response):
    for _ in range(5):
        if not trusted_sso(response.url):
            raise AuthError("untrusted_url")
        GovBrAuth._check_response(response)
        page = LoginPage(response.url, response.text)
        if page.kind == "two_factor":
            raise AuthError("two_factor_required")
        if page.kind == "password":
            return page
        if response.status_code not in REDIRECTS:
            raise AuthError("captcha_http_not_accepted")
        location = response.headers.get("Location")
        if not location:
            raise AuthError("captcha_redirect_missing")
        target = urljoin(response.url, location)
        if not trusted_sso(target):
            raise AuthError("unexpected_captcha_redirect")
        response = session.get(target, timeout=30, allow_redirects=False)
    raise AuthError("captcha_redirect_limit")
