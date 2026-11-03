"""Camada de autenticação desacoplada de browser.

Regra de ouro: este pacote não resolve captcha. Ele *consome* um token de
qualquer provedor (manual, browser, serviço) e executa o fluxo HTTP.
"""
from .captcha import CaptchaProvider, ManualCaptcha, StaticToken, NullCaptcha
from .govbr import GovBrAuth, AuthError, TwoFactorRequired

__all__ = [
    "CaptchaProvider",
    "ManualCaptcha",
    "StaticToken",
    "NullCaptcha",
    "GovBrAuth",
    "AuthError",
    "TwoFactorRequired",
]
