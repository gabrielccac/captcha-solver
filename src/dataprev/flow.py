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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--document", choices=("comprovante", "consulta", "all"), default="all")
    ap.add_argument("--env-file")
    ap.add_argument("--cdp-url", help="Chrome ja aberto com --remote-debugging-port")
    args = ap.parse_args()
    os.umask(0o077)
    values = load_env_file(args.env_file) if args.env_file else dict(os.environ)
    values = {key: values.get(key) or os.environ.get(key, "") for key in KEYS}
    cpf, password, api_key = credentials(values)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    receipt = {"document": args.document, "success": False, "documents": {}, "errors": []}
    auth_receipt = {}
    session = requests.Session()
    try:
        try:
            login(session, cpf, password, api_key, receipt=auth_receipt, cdp_url=args.cdp_url or "")
        except AuthError as error:
            receipt.update(code=error.code, http_status=error.http_status, stage="credenciamento",
                           credenciamento=auth_receipt)
            raise
        receipt["credenciamento"] = auth_receipt
        token = session.headers["Authorization"].removeprefix("Bearer ")
        receipt.update(collect(DataprevClient(token), out, args.document))
        receipt["credenciamento"] = auth_receipt
    except AuthError:
        receipt["success"] = False
    except ServiceError as error:
        receipt.update(code=error.code, http_status=error.status, success=False)
    finally:
        session.close()
    receipt["total_ms"] = round((time.monotonic() - started) * 1000)
    (out / "receipt.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"success": receipt["success"], "code": receipt.get("code"),
                      "documents": list(receipt.get("documents", {})),
                      "errors": receipt.get("errors", []),
                      "total_ms": receipt["total_ms"]}, indent=1, ensure_ascii=False))
    if receipt.get("code") in CREDENTIAL_CODES or receipt.get("stage") == "credenciamento":
        return 3
    return 0 if receipt["success"] else 4


if __name__ == "__main__":
    sys.exit(main())
