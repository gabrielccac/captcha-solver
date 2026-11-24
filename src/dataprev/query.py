"""Consulta CadÚnico por HTTP com um Bearer gov.br já emitido."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from inss.http_login import load_env_file
from .client import DataprevClient, ServiceError, export, save

DOCUMENTS = {"comprovante": "comprovante", "consulta": "consulta_completa", "all": None}
CREDENTIAL_CODES = {"session_expired", "not_authorized", "identity_mismatch", "identity_unparsed",
                    "credential_missing", "credential_wrong_audience", "credential_wrong_issuer"}


def collect(client, out, document):
    """Baixa os documentos pedidos. Falha em um documento não derruba o outro."""
    receipt = {"document": document, "documents": {}, "errors": []}
    receipt["credential"] = export(client)["token_validity"]
    receipt["session"] = client.check()
    wanted = [DOCUMENTS[document]] if DOCUMENTS[document] else ["comprovante", "consulta_completa"]
    for name in wanted:
        try:
            result = client.consulta_completa() if name == "consulta_completa" else client.comprovante()
        except ServiceError as error:
            receipt["errors"].append({"document": name, "code": error.code, "http_status": error.status})
            continue
        path = save(result, out / f"{name.replace('_', '-')}.pdf")
        receipt["documents"][name] = {key: value for key, value in result.items()
                                      if key not in {"pdf", "qr_code", "validacao"}}
        receipt["documents"][name].update(file=path.name,
                                         qr_code=bool(result.get("qr_code")),
                                         validacao_pdf=bool((result.get("validacao") or {}).get("pdf")))
    receipt["success"] = bool(receipt["documents"]) and not receipt["errors"]
    return receipt


def bearer(path=None, explicit=""):
    value = explicit or os.environ.get("DATAPREV_BEARER", "")
    if path:
        value = load_env_file(path).get("DATAPREV_BEARER", "") or value
    return value


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--document", choices=tuple(DOCUMENTS), default="all")
    ap.add_argument("--env-file")
    ap.add_argument("--token", help="Bearer já emitido; prefira DATAPREV_BEARER no ambiente")
    args = ap.parse_args()
    os.umask(0o077)
    token = bearer(args.env_file, args.token)
    if not token:
        ap.error("DATAPREV_BEARER is required, by environment or --env-file")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        receipt = collect(DataprevClient(token), out, args.document)
    except ServiceError as error:
        receipt = {"document": args.document, "success": False, "documents": {}, "errors": [],
                   "code": error.code, "http_status": error.status}
    receipt["total_ms"] = round((time.monotonic() - started) * 1000)
    (out / "receipt.json").write_text(json.dumps(receipt, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(receipt, indent=1, ensure_ascii=False))
    if receipt.get("code") in CREDENTIAL_CODES:
        return 3
    return 0 if receipt["success"] else 4


if __name__ == "__main__":
    sys.exit(main())
