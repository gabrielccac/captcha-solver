"""Contrato de captcha: o transporte não sabe (nem quer saber) como o token nasce.

Implemente `solve()` com o que você tiver — browser, serviço ou humano — e injete
em `GovBrAuth`. Nenhuma implementação aqui contorna verificação: todas recebem
um token já produzido pelo fluxo legítimo do portal.
"""
import getpass
import warnings
from typing import Protocol


def secure_prompt(label):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(label)


class CaptchaProvider(Protocol):
    def solve(self, sitekey: str | None, url: str, action: str | None = None) -> str:
        """Devolve o valor de `h-captcha-response` (ou string vazia se não houver)."""
        ...


class StaticToken:
    """Token já obtido fora do processo (ex.: capturado no seu navegador)."""

    def __init__(self, token: str):
        self.token = token.strip()

    def solve(self, sitekey=None, url="", action=None) -> str:
        token, self.token = self.token, ""
        return token


class ManualCaptcha:
    """Pede o token ao operador. Útil em teste controlado e em produção assistida."""

    def __init__(self, prompt=secure_prompt, label="Cole o valor de h-captcha-response (oculto): "):
        self._prompt = prompt
        self._label = label

    def solve(self, sitekey=None, url="", action=None) -> str:
        return (self._prompt(self._label) or "").strip()


class NullCaptcha:
    """Portais sem captcha."""

    def solve(self, sitekey=None, url="", action=None) -> str:
        return ""
