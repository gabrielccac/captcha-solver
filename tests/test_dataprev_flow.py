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


def run_flow(monkeypatch, tmp_path, extra=(), document="comprovante"):
    receipt_path = tmp_path / "receipt.json"
    seen = {}

    def stub_login(session, cpf, password, api_key, *, receipt=None, cdp_url="", progress=None):
        receipt.update(phase_events=[], success=False)
        progress("browser_start")
        receipt["phase_events"].append({"phase": "browser_start", "elapsed_ms": 4000})
        progress("cpf")
        seen["receipt"] = json.loads(receipt_path.read_text(encoding="utf-8"))
        raise AuthError("token_not_captured")

    monkeypatch.setenv("GOVBR_CPF", CPF)
    monkeypatch.setenv("GOVBR_PASSWORD", PASSWORD)
    monkeypatch.setenv("OPENROUTER_API_KEY", API_KEY)
    monkeypatch.setattr(dataprev_flow, "login", stub_login)
    monkeypatch.setattr(sys, "argv", ["flow", "--out", str(tmp_path), "--document", document, *extra])
    code = dataprev_flow.main()
    return code, json.loads(receipt_path.read_text(encoding="utf-8")), seen["receipt"]


def test_o_recibo_e_gravado_durante_o_credenciamento(monkeypatch, tmp_path, capsys):
    code, final, durante = run_flow(monkeypatch, tmp_path)
    assert code == 3
    assert durante["stage"] == "credenciamento"
    assert durante["credenciamento"]["phase_now"] == "cpf"
    assert durante["document"] == "comprovante"
    assert final["code"] == "token_not_captured"
    assert "phase_now" not in final["credenciamento"]
    segredos = json.dumps(final)
    for secreto in (PASSWORD, API_KEY):
        assert secreto not in segredos


def test_log_de_texto_mostra_o_passo_no_stderr(monkeypatch, tmp_path, capsys):
    run_flow(monkeypatch, tmp_path)
    linhas = capsys.readouterr().err
    assert "inicio documento=comprovante" in linhas
    assert "credenciamento phase=browser_start" in linhas
    assert "credenciamento_falhou code=token_not_captured" in linhas
    assert "fim success=False code=token_not_captured" in linhas


def test_log_jsonl_entrega_um_evento_por_linha_e_arquiva(monkeypatch, tmp_path, capsys):
    run_flow(monkeypatch, tmp_path, extra=("--log-format", "jsonl"))
    linhas = [linha for linha in capsys.readouterr().err.splitlines() if linha.strip()]
    eventos = [json.loads(linha) for linha in linhas]
    assert [evento["event"] for evento in eventos] == [
        "inicio", "credenciamento", "credenciamento", "credenciamento_falhou", "fim"]
    for evento in eventos:
        assert {"ts", "t", "run_id", "event"} <= set(evento)
        assert evento["ts"].endswith("Z") and "T" in evento["ts"]
    assert eventos[0]["run_id"] == eventos[-1]["run_id"]
    assert eventos[-1]["code"] == "token_not_captured"

    arquivados = [json.loads(linha) for linha in
                  (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [evento["event"] for evento in arquivados] == [evento["event"] for evento in eventos]
    assert all("ts" in evento and "run_id" in evento for evento in arquivados)


def test_log_jsonl_nao_grava_credenciais(monkeypatch, tmp_path, capsys):
    run_flow(monkeypatch, tmp_path, extra=("--log-format", "jsonl"))
    arquivo = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    for secreto in (PASSWORD, API_KEY, CPF):
        assert secreto not in arquivo
