"""MCP local do CadÚnico: comprovante e consulta completa em PDF.

Roda na máquina do cliente, de dentro da rede dele, porque é ali que o login
gov.br acontece: navegador local, hCaptcha e senha. O Dataprev recusa o host
cadunico em faixas de datacenter, então a instalação local não é preferência,
é o único caminho suportado.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Lock

from mcp.server import MCPServer

from dataprev.query import CREDENTIAL_CODES

mcp = MCPServer("cadunico")
_flow_lock = Lock()
TIMEOUT_SECONDS = 300
DOCUMENTS_DIR = Path(os.environ.get("DATAPREV_DOCUMENTS_DIR",
                                    str(Path.cwd() / "runs" / "mcp" / "dataprev")))
ENV_FILE = os.environ.get("DATAPREV_ENV_FILE", ".env")
DOCUMENTS = {"comprovante": "comprovante", "consulta": "consulta", "ambos": "all"}
RECOVERABLE = CREDENTIAL_CODES | {
    "invalid_cpf_format", "password_missing_in_env", "solver_key_missing", "solver_provider_error",
    "browser_error", "captcha_challenge_missing", "captcha_not_accepted",
    "captcha_selection_not_confirmed", "login_page_unrecognized", "token_not_captured",
    "token_expired", "token_claims_unreadable", "token_wrong_audience", "identity_failed",
    "identity_mismatch", "dataprev_login_runtime_error", "fresh_http_session_required",
}


@mcp.tool()
def get_cadunico_documents(cpf: str, password: str = "", api_key: str = "",
                           documento: str = "ambos") -> dict:
    """Baixa do CadÚnico o comprovante e a consulta completa, em PDF.

    Faz o credenciamento no gov.br no navegador desta máquina (hCaptcha e senha)
    e baixa os documentos pedidos. A senha vale só para esta chamada: chega por
    entrada padrão, nunca como argumento de processo, e não volta na resposta.
    password vazio usa GOVBR_PASSWORD do .env local (ou de DATAPREV_ENV_FILE);
    api_key é a chave do modelo que resolve o hCaptcha e, vazia, usa
    OPENROUTER_API_KEY do mesmo arquivo.
    documento aceita comprovante, consulta ou ambos.
    Cada documento volta em documents com file_path local (PDF em modo 600) e os
    metadados do próprio Dataprev, incluindo qr_code e validacao_pdf.
    Falha em um documento não derruba o outro: leia complete e errors.
    O credenciamento frio leva cerca de 90 segundos por causa do hCaptcha.
    Códigos de credencial e de navegador voltam em error_code, sem exceção;
    identidade divergente (identity_mismatch) indica CPF diferente do titular.
    """
    if not re.fullmatch(r"\d{11}|\d{3}\.\d{3}\.\d{3}-\d{2}", cpf, flags=re.ASCII):
        raise ValueError("CPF deve ter 11 dígitos")
    if documento not in DOCUMENTS:
        raise ValueError("documento deve ser comprovante, consulta ou ambos")
    digits = "".join(c for c in cpf if c.isdigit())
    started = time.monotonic()
    with _flow_lock, tempfile.TemporaryDirectory(prefix="cadunico-mcp-") as work:
        queue_ms = round((time.monotonic() - started) * 1000)
        out = Path(work) / "out"
        command = [sys.executable, "-m", "dataprev.flow", "--credentials-stdin",
                   "--out", str(out), "--document", DOCUMENTS[documento]]
        env_file = Path(ENV_FILE)
        if env_file.is_file():
            command.extend(["--env-file", str(env_file)])
        try:
            subprocess.run(command, input=json.dumps({"cpf": digits, "password": password,
                                                      "api_key": api_key}) + "\n",
                           capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("O credenciamento no gov.br excedeu o tempo limite") from None
        receipt = _receipt(out)
        if not receipt:
            raise RuntimeError("O fluxo do CadÚnico não produziu recibo")
        if not receipt.get("success") and not receipt.get("documents"):
            code = receipt.get("code") or "flow_failed"
            if code not in RECOVERABLE:
                raise RuntimeError(f"O fluxo do CadÚnico falhou ({code})")
            return {
                "cpf_mask": _mask(digits),
                "documento": documento,
                "complete": False,
                "error_code": code,
                "error": "O credenciamento no gov.br não concluiu",
                "stage": receipt.get("stage"),
                "http_status": receipt.get("http_status"),
                "credenciamento": _credenciamento(receipt),
                "errors": receipt.get("errors", []),
                "timings": {"credenciamento_ms": _elapsed(receipt), "queue_ms": queue_ms},
            }
        documents = _persist(receipt.get("documents") or {}, out)
        if not documents:
            raise RuntimeError("O fluxo do CadÚnico terminou sem documentos")
        return {
            "cpf_mask": _mask(digits),
            "documento": documento,
            "documents": documents,
            "complete": not receipt.get("errors"),
            "errors": receipt.get("errors", []),
            "token_validity": receipt.get("credential"),
            "credenciamento": _credenciamento(receipt),
            "timings": {"credenciamento_ms": _elapsed(receipt),
                        "flow_total_ms": receipt.get("total_ms"),
                        "queue_ms": queue_ms,
                        "mcp_total_ms": round((time.monotonic() - started) * 1000)},
        }


def _receipt(out: Path) -> dict:
    try:
        return json.loads((out / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _mask(digits: str) -> str:
    return f"{digits[:3]}.***.***-{digits[-2:]}"


def _elapsed(receipt: dict) -> object:
    return (receipt.get("credenciamento") or {}).get("elapsed_ms")


def _credenciamento(receipt: dict) -> dict:
    source = receipt.get("credenciamento") or {}
    return {key: source.get(key) for key in
            ("sso_reused", "consent_clicked", "identity_status", "browser_error_type",
             "runtime_error_type", "failure_stage", "final_host")
            if source.get(key) is not None} | {"phase_ms": source.get("phase_ms")}


def _persist(found: dict, out: Path) -> dict:
    documents = {}
    destination = None
    for name, meta in found.items():
        entry = {key: value for key, value in meta.items() if key != "file"}
        source = (out / str(meta.get("file") or "")).resolve()
        if source.parent != out.resolve() or source.suffix != ".pdf" or not source.is_file():
            entry["error"] = "PDF ausente na saída do fluxo"
            documents[name] = entry
            continue
        try:
            if destination is None:
                DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
                destination = Path(tempfile.mkdtemp(prefix="cadunico-", dir=DOCUMENTS_DIR))
            target = destination / source.name
            shutil.move(str(source), target)
            target.chmod(0o600)
            entry.update(file_path=str(target), bytes=target.stat().st_size)
        except OSError:
            entry["error"] = "PDF não pôde ser gravado em disco"
        documents[name] = entry
    return documents


def main():
    mcp.run()


if __name__ == "__main__":
    main()
