"""Meu INSS queries run inside the Actions worker right after login.

Returns one JSON-serializable result with the PDFs inlined as base64, so the runner can seal it
and POST it straight to the caller (n8n) instead of exporting API tokens through an artifact.
Every component fails independently; ``errors`` lists what is missing and ``complete`` is False then.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import re
import tempfile
from pathlib import Path

import requests

from inss.medical import download_medical_report, list_medical_reports
from inss.session import API, concession_letters, credit_windows, extract, session_benefits

TIMEOUT = 30


def _pdf(response):
    """The PDF endpoints return raw PDF bytes or a JSON/string envelope with base64."""
    raw = response.content
    if raw[:5] == b"%PDF-":
        return raw
    try:
        body = json.loads(raw)
    except ValueError:
        body = raw.decode("utf-8", "replace")
    if isinstance(body, dict):
        body = body.get("pdf") or body.get("arquivo") or body.get("conteudo")
    if isinstance(body, str):
        body = re.sub(r"^data:application/pdf;base64,", "", body.strip())
        if body.startswith("JVBER"):
            try:
                return base64.b64decode(body)
            except ValueError:
                return None
    return None


def _document(kind, filename, content, **extra):
    return {"kind": kind, "filename": filename, "bytes": len(content),
            "base64": base64.b64encode(content).decode(), **extra}


def _statement(headers, cpf, window):
    first, last = window
    result = {"window": f"{first}/{last}", "http_status": None}
    try:
        response = requests.get(f"{API}/hiscreServices/historicocreditosPdf/{cpf.lstrip('0')}/{first}/{last}",
                                headers=headers, timeout=TIMEOUT, allow_redirects=False)
        result["http_status"] = response.status_code
        content = _pdf(response) if response.status_code == 200 else None
        if content is None:
            result["error"] = "documento_indisponivel" if response.status_code == 404 else "resposta_sem_pdf"
        return result, content
    except requests.RequestException:
        result["error"] = "falha_de_rede"
        return result, None


def _protocol(headers, number):
    result = {"protocol": number, "http_status": None, "found": False}
    try:
        response = requests.get(f"{API}/SagService/tarefa/{number}", params={"nome": ""},
                                headers=headers, timeout=TIMEOUT, allow_redirects=False)
        result["http_status"] = response.status_code
        if response.status_code == 200:
            data = response.json()
            found = re.sub(r"\D", "", str(data.get("numeroProtocolo") or data.get("protocolo") or ""))
            if found == number:
                service = data.get("servico")
                result.update(found=True, status=data.get("status") or data.get("situacao"),
                              service=(service or {}).get("nome") if isinstance(service, dict) else data.get("nomeServico"),
                              updated_at=data.get("dataUltimaAtualizacao") or data.get("dataAtualizacao"))
    except (requests.RequestException, ValueError, AttributeError):
        result["error"] = "consulta_falhou"
    return result


def run(session, cpf, options):
    years = int(options.get("years", 5))
    protocol = str(options.get("protocol") or "")
    auth = session.headers["Authorization"].removeprefix("Bearer ")
    mi = session.headers["miToken"]
    headers = dict(session.headers)
    windows = credit_windows(years=years)
    out = {"benefits": [], "history": None, "statement": None, "concession_letters": [],
           "medical_reports": [], "protocol": None, "documents": [], "errors": []}
    try:
        out["benefits"] = session_benefits(mi)
    except ValueError:
        out["errors"].append({"component": "benefits", "code": "invalid_benefit_list"})

    with tempfile.TemporaryDirectory(prefix="inss-docs-") as temporary, \
            ThreadPoolExecutor(max_workers=5) as pool:
        folder = Path(temporary)
        history = pool.submit(extract, cpf, auth, mi, windows, session=session, include_userinfo=False)
        statement = pool.submit(_statement, headers, cpf, windows[0])
        letters = pool.submit(concession_letters, auth, mi, folder, session=session)
        medical = []
        if options.get("medical_reports"):
            try:
                eligible = [report["benefit_number"] for report in list_medical_reports(mi)]
            except ValueError:
                eligible = []
            medical = [pool.submit(download_medical_report, auth, mi, nb, folder, session=session)
                       for nb in eligible]
        status = pool.submit(_protocol, headers, protocol) if protocol else None

        data = history.result()
        out["history"] = {"windows": data["windows"], "credits": data["creditos"],
                          "benefits_in_history": data["beneficios"], "n_unique_credits": data["n_unique_credits"]}
        for window in data["windows"]:
            if window.get("error"):
                out["errors"].append({"component": "history", "window": window["window"], "code": window["error"]})

        info, content = statement.result()
        out["statement"] = info
        if content:
            out["documents"].append(_document("extrato", "extrato-meu-inss.pdf", content))
        else:
            out["errors"].append({"component": "statement", "code": info.get("error")})

        try:
            letter_results = letters.result()
        except ValueError:
            letter_results = []
            out["errors"].append({"component": "concession_letters", "code": "benefit_outside_session"})
        for letter in letter_results:
            letter.pop("parse_error", None)
            out["concession_letters"].append(letter)
            if letter.get("file"):
                path = folder / letter["file"]
                out["documents"].append(_document("carta", letter["file"], path.read_bytes(),
                                                  benefit_number=letter["benefit_number"]))
            else:
                out["errors"].append({"component": "concession_letters", "benefit_number": letter["benefit_number"],
                                      "code": "documento_indisponivel"})

        for job in medical:
            report = job.result()
            out["medical_reports"].append(report)
            if report.get("file"):
                path = folder / report["file"]
                out["documents"].append(_document("laudo", report["file"], path.read_bytes(),
                                                  benefit_number=report["benefit_number"]))
            else:
                out["errors"].append({"component": "medical_reports", "benefit_number": report["benefit_number"],
                                      "code": "documento_indisponivel"})

        if status is not None:
            out["protocol"] = status.result()
            if not out["protocol"]["found"]:
                out["errors"].append({"component": "protocol", "code": "protocolo_nao_encontrado"})

    out["complete"] = not out["errors"]
    return out
