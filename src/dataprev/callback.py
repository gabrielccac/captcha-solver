"""Desfecho do callback OAuth do gateway, classificado sem guardar URL nem corpo.

O retorno do gateway chega de três formas: erro definitivo (o retorno traz
``error=``), falha passageira sinalizada (HTTP 408/429/5xx ou
``error=temporarily_unavailable``/``server_error``) e silêncio, quando callback
nenhum aparece. Só o silêncio justifica esperar a janela inteira de timeout: os
outros dois desfechos são conclusivos e encerram a fase token em segundos.

O que sai daqui é só código curto, HTTP e tempo. Token, URL completa e corpo da
resposta nunca são copiados para o recibo, porque nem chegam a ser guardados.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

CALLBACK_HOSTS = {"cadunico.dataprev.gov.br", "meucadunico.dataprev.gov.br",
                  "sso.acesso.gov.br"}
APPLICATION_HOSTS = {"cadunico.dataprev.gov.br"}
CALLBACK_PARAMS = {"error", "code"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ACCESS_DENIED_ERRORS = {"access_denied", "login_required", "interaction_required",
                        "consent_required", "account_selection_required"}
TRANSIENT_ERRORS = {"temporarily_unavailable", "server_error"}
TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Outcome:
    """Desfecho conclusivo: código curto da etapa, HTTP e nada do conteúdo."""

    kind: str
    code: str
    http_status: int | None = None

    def event(self, elapsed_ms: float) -> dict:
        """Registro de log com etapa, tempo e código — sem URL, token ou corpo."""
        return {"stage": "token", "kind": self.kind, "code": self.code,
                "http_status": self.http_status, "elapsed_ms": round(elapsed_ms)}


def callback_params(url: str) -> dict:
    """Parâmetros do retorno do gateway, da query e do fragmento.

    Os valores ficam aqui só para classificar: só o código do erro sai daqui.
    """
    parts = urlsplit(url or "")
    params = {}
    if "?" in parts.fragment:
        params.update(parse_qs(parts.fragment.split("?", 1)[1]))
    params.update(parse_qs(parts.query))
    return params


def classify(url: str, status: int | None = None) -> Outcome | None:
    """Desfecho conclusivo do callback, ou None enquanto não há o que concluir.

    ``status`` é o HTTP da resposta do callback, quando o chamador o viu. Um
    retorno com ``code`` e HTTP 200 ainda pode terminar em token: não conclui.
    """
    parts = urlsplit(url or "")
    if parts.hostname not in CALLBACK_HOSTS:
        return None
    params = callback_params(url)
    if not params.keys() & CALLBACK_PARAMS:
        return None
    error = (params.get("error") or [""])[0]
    if error in TRANSIENT_ERRORS:
        return Outcome("transient", "callback_temporarily_unavailable", status)
    if error:
        code = ("callback_access_denied" if error in ACCESS_DENIED_ERRORS
                else "callback_rejected")
        return Outcome("terminal", code, status)
    if status is None:
        return None
    if status in TRANSIENT_STATUSES:
        return Outcome("transient", "callback_temporarily_unavailable", status)
    if 400 <= status < 500:
        return Outcome("terminal", "callback_rejected", status)
    return None


class CallbackWatch:
    """Guarda o primeiro desfecho conclusivo e o que a página entregou.

    O listener classifica na hora e retém só o ``Outcome`` e dois booleanos de
    percurso: se o gateway redirecionou com ``code``/``state`` e se o documento da
    aplicação carregou. Com eles, um silêncio deixa de ser um mistério: sem o
    redirecionamento a perda é do servidor, com ele a perda é do SPA.
    """

    def __init__(self):
        self.outcome: Outcome | None = None
        self.redirect_seen = False
        self.document_seen = False

    def observe(self, response) -> None:
        url = getattr(response, "url", "") or ""
        status = getattr(response, "status", None)
        try:
            host = urlsplit(url).hostname
            if (host in CALLBACK_HOSTS and status in REDIRECT_STATUSES
                    and callback_params(url).keys() & CALLBACK_PARAMS):
                self.redirect_seen = True
            if (host in APPLICATION_HOSTS and status == 200
                    and _resource_type(response) in ("", "document")):
                self.document_seen = True
            if self.outcome is not None:
                return
            outcome = classify(url, status)
        except (TypeError, ValueError, AttributeError):
            return
        if outcome is not None:
            self.outcome = outcome


def _resource_type(response) -> str:
    """Tipo do recurso quando o cliente informa (Playwright entrega 'document')."""
    return getattr(getattr(response, "request", None), "resource_type", "")
