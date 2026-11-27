"""Credenciamento gov.br e documentos do CadÚnico no mesmo processo.

Uma execução faz o login no gateway (Chrome/Xvfb, hCaptcha e handoff de senha),
recebe o Bearer no redirecionamento final e baixa os documentos. O token não é
escrito em disco nem devolvido no recibo: ele vive na sessão em memória.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import requests

from auth.govbr import AuthError
from inss.http_login import load_env_file
from .client import DataprevClient, ServiceError
from .login import login
from .query import CREDENTIAL_CODES, collect

KEYS = ("GOVBR_CPF", "GOVBR_PASSWORD", "OPENROUTER_API_KEY")


def credentials(values):
    missing = [key for key in KEYS if not values.get(key)]
    if missing:
        raise SystemExit(f"faltam credenciais: {', '.join(missing)}")
    cpf = re.sub(r"\D", "", values["GOVBR_CPF"])
    if len(cpf) != 11:
        raise SystemExit("GOVBR_CPF deve ter 11 dígitos")
    return cpf, values["GOVBR_PASSWORD"], values["OPENROUTER_API_KEY"]


# Medido em 55 execuções reais com dados reais: a falha dominante é externa e
# passageira — silêncio do portal no token (26%), captcha que não emite token (9%),
# resposta vazia do upstream (7%) e state do gov.br (9%). Cada tentativa abre uma
# sessão nova, então repetir é o que converte essas falhas em documento, ao custo
# de outro hCaptcha e de cerca de 90 segundos.
RETRY_ATTEMPTS = 2
RETRY_WAIT_SECONDS = 10

TRANSIENT_CODES = {
    "token_not_captured", "captcha_not_accepted", "captcha_selection_not_confirmed",
    "captcha_challenge_missing", "browser_error", "dataprev_login_runtime_error",
    "upstream_empty_response", "upstream_invalid_json", "session_expired", "not_authorized",
    "identity_failed", "identity_unparsed", "callback_temporarily_unavailable",
    "connection_error", "read_timeout", "connect_timeout", "request_timeout",
    "incomplete_http_response",
}


def _tentativa(args, cpf, password, api_key, out, receipt, auth_receipt, registrar):
    """Um credenciamento completo. Falha de portal volta no recibo, nunca como exceção."""
    inicio = time.monotonic()
    session = requests.Session()

    def em_curso(phase_name):
        decorrido = (auth_receipt.get("phase_events") or [{}])[-1].get("elapsed_ms", 0)
        campos = {"phase": phase_name, "elapsed_ms": decorrido}
        if phase_name != "browser_start":
            campos.update(browser_version=auth_receipt.get("browser_version"),
                          browser_source=auth_receipt.get("browser_source"),
                          cdp_port=auth_receipt.get("cdp_port"))
        registrar("credenciamento", **campos)
        auth_receipt["phase_now"] = phase_name
        try:
            (out / "receipt.json").write_text(json.dumps(
                {**receipt, "stage": "credenciamento", "success": False,
                 "credenciamento": auth_receipt}, indent=1, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    try:
        try:
            login(session, cpf, password, api_key, receipt=auth_receipt,
                  cdp_url=args.cdp_url or "", progress=em_curso, headless=args.headless)
        except AuthError as error:
            registrar("credenciamento_falhou", code=error.code,
                      http_status=error.http_status,
                      etapa=auth_receipt.get("failure_stage"),
                      elapsed_ms=auth_receipt.get("elapsed_ms"))
            auth_receipt.pop("phase_now", None)
            receipt.update(code=error.code, http_status=error.http_status, stage="credenciamento",
                           credenciamento=auth_receipt)
            raise
        auth_receipt.pop("phase_now", None)
        receipt["credenciamento"] = auth_receipt
        registrar("credenciamento_ok", elapsed_ms=auth_receipt.get("elapsed_ms", 0))
        token = session.headers["Authorization"].removeprefix("Bearer ")
        receipt.update(collect(DataprevClient(token), out, args.document, progress=registrar))
        receipt["credenciamento"] = auth_receipt
    except AuthError:
        receipt["success"] = False
    except ServiceError as error:
        registrar("documento_falhou", code=error.code, http_status=error.status)
        receipt.update(code=error.code, http_status=error.status, success=False)
    finally:
        session.close()
        receipt["total_ms"] = round((time.monotonic() - inicio) * 1000)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--document", choices=("comprovante", "consulta", "all"), default="all")
    ap.add_argument("--env-file")
    ap.add_argument("--credentials-stdin", action="store_true",
                    help="Lê {cpf, password, api_key} da entrada padrão, sem variável de ambiente")
    ap.add_argument("--cdp-url", help="Chrome ja aberto com --remote-debugging-port")
    ap.add_argument("--headless", action="store_true",
                    help="Chrome headless em vez de janela, para contêiner e comparação")
    ap.add_argument("--retry", type=int, default=RETRY_ATTEMPTS,
                    help="tentativas extras quando a falha é passageira (portal, captcha, browser)")
    ap.add_argument("--retry-wait", type=int, default=RETRY_WAIT_SECONDS,
                    help="segundos de espera entre tentativas")
    ap.add_argument("--log-format", choices=("texto", "jsonl"), default="texto",
                    help="texto: uma linha por passo; jsonl: um evento JSON por linha, "
                         "também gravado em events.jsonl na saída")
    args = ap.parse_args()
    os.umask(0o077)
    values = load_env_file(args.env_file) if args.env_file else dict(os.environ)
    values = {key: values.get(key) or os.environ.get(key, "") for key in KEYS}
    if args.credentials_stdin:
        sent = json.loads(sys.stdin.readline(8192))
        for key, given in (("GOVBR_CPF", "cpf"), ("GOVBR_PASSWORD", "password"),
                           ("OPENROUTER_API_KEY", "api_key")):
            if sent.get(given):
                values[key] = sent[given]
    cpf, password, api_key = credentials(values)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    receipt = {"document": args.document, "success": False, "documents": {}, "errors": []}
    auth_receipt = {}

    run_id = uuid.uuid4().hex[:12]
    events_path = out / "events.jsonl"

    def registrar(event, **fields):
        """Um evento por linha: texto para gente, JSONL para quem lê por programa."""
        moment = time.time()
        registro = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(moment))
                          + f".{int(moment % 1 * 1000):03d}Z",
                    "t": round(time.monotonic() - started, 3), "run_id": run_id,
                    "event": event, **fields}
        if args.log_format == "jsonl":
            print(json.dumps(registro, ensure_ascii=False), file=sys.stderr, flush=True)
        else:
            detalhe = " ".join(f"{key}={value}" for key, value in fields.items())
            print(f"[{time.strftime('%H:%M:%S', time.localtime(moment))}] {event}"
                  + (f" {detalhe}" if detalhe else ""), file=sys.stderr, flush=True)
        try:
            with events_path.open("a", encoding="utf-8") as archive:
                archive.write(json.dumps(registro, ensure_ascii=False) + "\n")
        except OSError:
            pass

    registrar("inicio", documento=args.document, saida=str(out), tentativas=args.retry + 1)
    tentativas = []
    total = args.retry + 1
    for numero in range(1, total + 1):
        receipt = {"document": args.document, "success": False, "documents": {}, "errors": []}
        auth_receipt = {}
        registrar("tentativa", numero=numero, de=total)
        _tentativa(args, cpf, password, api_key, out, receipt, auth_receipt, registrar)
        codigo = receipt.get("code") or (receipt.get("errors") or [{}])[0].get("code")
        tentativas.append({"tentativa": numero, "code": codigo,
                           "total_ms": receipt.get("total_ms")})
        receipt["attempts"] = tentativas
        if receipt["success"] or codigo not in TRANSIENT_CODES or numero == total:
            break
        registrar("nova_tentativa", motivo=codigo, aguardando_s=args.retry_wait)
        time.sleep(args.retry_wait)

    documento_erro = (receipt.get("errors") or [{}])[0].get("code")
    registrar("fim", success=receipt["success"], code=receipt.get("code") or documento_erro,
              total_ms=receipt["total_ms"], documentos=list(receipt.get("documents", {})),
              tentativas=len(tentativas))
    (out / "receipt.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False),
                                      encoding="utf-8")
    print(json.dumps({"success": receipt["success"], "code": receipt.get("code"),
                      "documents": list(receipt.get("documents", {})),
                      "errors": receipt.get("errors", []),
                      "attempts": tentativas,
                      "total_ms": receipt["total_ms"]}, indent=1, ensure_ascii=False))
    if receipt.get("code") in CREDENTIAL_CODES or receipt.get("stage") == "credenciamento":
        return 3
    return 0 if receipt["success"] else 4


if __name__ == "__main__":
    sys.exit(main())
