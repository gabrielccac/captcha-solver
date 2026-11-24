"""Testes offline do fluxo completo do CadÚnico (sem rede, sem browser)."""
import json
import sys
from pathlib import Path

import pytest

from auth.govbr import AuthError
from dataprev import flow as dataprev_flow

CPF = "12345678901"
PASSWORD = "senha-sintetica"
API_KEY = "chave-sintetica"


def test_o_recibo_e_gravado_durante_o_credenciamento(monkeypatch, tmp_path, capsys):
    receipt_path = tmp_path / "receipt.json"

    def stub_login(session, cpf, password, api_key, *, receipt=None, cdp_url="", progress=None):
        receipt.update(phase_events=[], success=False)
        progress("browser_start")
        gravado = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert gravado["stage"] == "credenciamento"
        assert gravado["credenciamento"]["phase_now"] == "browser_start"
        assert gravado["document"] == "comprovante"
        raise AuthError("token_not_captured")

    monkeypatch.setenv("GOVBR_CPF", CPF)
    monkeypatch.setenv("GOVBR_PASSWORD", PASSWORD)
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    monkeypatch.setattr(dataprev_flow, "login", stub_login)
    monkeypatch.setattr(sys, "argv", ["flow", "--out", str(tmp_path), "--document", "comprovante"])
    assert dataprev_flow.main() == 3
    final = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert final["code"] == "token_not_captured"
    assert "phase_now" not in final["credenciamento"]
    segredos = json.dumps(final)
    for secreto in (PASSWORD, API_KEY):
        assert secreto not in segredos
