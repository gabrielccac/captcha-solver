"""Ponte browser -> transporte HTTP.

O browser resolve; este módulo só *lê* o resultado e transfere a identidade
(cookies) para a sessão `requests`. Nenhuma lógica de resolução mora aqui.
"""
from __future__ import annotations

# Ordem importa: o hCaptcha escreve no textarea oculto; getResponse() é fallback.
TOKEN_JS = """() => {
  const el = document.querySelector('[name="h-captcha-response"]');
  if (el && el.value) return el.value;
  const g = document.querySelector('textarea[name="g-recaptcha-response"]');
  if (g && g.value) return g.value;
  try { if (window.hcaptcha) return window.hcaptcha.getResponse() || ''; } catch (e) {}
  return '';
}"""

HTML_JS = "() => document.documentElement.outerHTML"


def read_token(page, timeout_ms: int = 90_000, poll_ms: int = 500) -> str:
    """Espera o token aparecer e devolve o valor (string vazia se não vier)."""
    if timeout_ms < 0 or poll_ms <= 0:
        raise ValueError("timeout_ms deve ser >= 0 e poll_ms > 0")
    waited = 0
    while waited <= timeout_ms:
        tok = page.evaluate(TOKEN_JS) or ""
        if tok:
            return tok
        if waited == timeout_ms:
            break
        delay = min(poll_ms, timeout_ms - waited)
        page.wait_for_timeout(delay)
        waited += delay
    return ""


class BrowserCaptcha:
    """`CaptchaProvider` que usa uma página já controlada por você.

    `mint` é opcional: se None, assume que a página já resolveu o desafio
    (ex.: você chamou `solve_round` antes).
    """

    def __init__(self, page, mint=None, timeout_ms: int = 90_000):
        self.page = page
        self._mint = mint
        self.timeout_ms = timeout_ms
        self._used = False

    def solve(self, sitekey=None, url="", action=None) -> str:
        if self._used:
            return ""
        self._used = True
        if self._mint:
            self._mint(self.page)
        return read_token(self.page, self.timeout_ms)


def adopt(page, session) -> str:
    """Copia os cookies do browser para a sessão `requests` e devolve o HTML atual.

    Cookies e User-Agent preservam parte do contexto. O servidor ainda pode
    rejeitar o token por vínculo com IP, TLS ou outros sinais do browser.
    """
    for c in page.context.cookies():
        session.cookies.set(c["name"], c["value"], domain=c["domain"],
                            path=c.get("path", "/"), secure=c.get("secure", False),
                            expires=c["expires"] if c.get("expires", -1) > 0 else None)
    session.headers["User-Agent"] = page.evaluate("() => navigator.userAgent")
    return page.evaluate(HTML_JS)
