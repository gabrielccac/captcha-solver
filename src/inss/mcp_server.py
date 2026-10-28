"""Local MCP access to Meu INSS payment history."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Lock

from mcp.server import MCPServer

mcp = MCPServer("meu-inss")
_login_lock = Lock()
DOCUMENTS_DIR = Path(__file__).resolve().parents[2] / "runs" / "mcp"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@mcp.tool()
def get_inss_payments(cpf: str, password: str = "", medical_report_nb: str = "") -> dict:
    """Return payments, concession letters, eligible medical reports and token expiry.

    The GOV.BR password is read from GOVBR_PASSWORD in the local .env unless
    one is supplied; it is used only for this call. Payment status fields are returned
    as reported by Meu INSS; they do not independently confirm bank availability.
    concession_letters contains persistent local PDF paths, one per benefit.
    benefits lists all session benefits; payments include their benefit_number.
    medical_reports lists eligible benefits, not confirmed PDF availability.
    Set medical_report_nb to a benefit_number from that list to download its PDF;
    medical_report returns its HTTP status and persistent file_path, or an error.
    Rejected credentials or a pending second factor return error_code and
    login_evidence instead of payment data; they are not inferred from timeouts.
    Token expiry is declared by the issuer; revocation can invalidate it earlier.
    """
    if not re.fullmatch(r"\d{11}|\d{3}\.\d{3}\.\d{3}-\d{2}", cpf):
        raise ValueError("CPF must have 11 digits")
    if medical_report_nb and not re.fullmatch(r"[0-9]{1,15}", medical_report_nb):
        raise ValueError("Medical report NB must contain 1 to 15 digits")
    digits = "".join(c for c in cpf if c.isdigit())
    with _login_lock, tempfile.TemporaryDirectory(prefix="inss-mcp-") as work:
        out = Path(work) / "out"
        env = dict(os.environ, GOVBR_PASSWORD=password) if password else dict(os.environ)
        command = [sys.executable, "-m", "inss.workflow", "--cpf", digits,
                   "--out", str(out), "--profile-dir", str(Path(work) / "profile"),
                   "--cdp-port", str(_free_port()), "--reasoning-effort", "minimal"]
        if medical_report_nb:
            command.extend(["--medical-report-nb", medical_report_nb])
        try:
            result = subprocess.run(command, env=env, capture_output=True,
                                    text=True, timeout=300, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Meu INSS login timed out") from None
        if result.returncode:
            try:
                failure = json.loads(result.stdout)
                model_errors = [r.get("solve_error", "") for r in failure.get("rounds", [])]
                reason = ("OpenRouter rejected inference (HTTP 403)"
                          if any("403 Client Error" in error for error in model_errors)
                          else failure.get("fatal", "workflow failed"))
                if failure.get("error_code") in ("invalid_credentials", "two_factor_required"):
                    return {
                        "cpf_mask": f"{digits[:3]}.***.***-{digits[-2:]}",
                        "error_code": failure["error_code"], "error": reason,
                        "login_evidence": failure.get("login_evidence"),
                    }
            except (ValueError, AttributeError):
                reason = "workflow failed"
            raise RuntimeError(f"Meu INSS: {reason}")
        data = json.loads((out / f"extratos-{digits}.json").read_text())
        if data.get("userinfo_status") != 200:
            raise RuntimeError("Meu INSS customer request failed")
        letters = data.get("concession_letters", [])
        report = data.get("medical_report")
        documents = letters + ([report] if report else [])
        if any(document.get("file") for document in documents):
            DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
            destination = Path(tempfile.mkdtemp(prefix="inss-", dir=DOCUMENTS_DIR))
            for document in documents:
                filename = document.pop("file", None)
                if filename:
                    source = (out / filename).resolve()
                    if source.parent != out.resolve() or source.suffix != ".pdf":
                        raise RuntimeError("Invalid document file path")
                    target = destination / source.name
                    shutil.move(source, target)
                    target.chmod(0o600)
                    document["file_path"] = str(target)
        return {
            "cpf_mask": f"{digits[:3]}.***.***-{digits[-2:]}",
            "payment_count": len(data["creditos"]),
            "benefits": data.get("benefits", []),
            "benefits_error": data.get("benefits_error"),
            "concession_letters": letters,
            "medical_reports": data.get("medical_reports", []),
            "medical_reports_error": data.get("medical_reports_error"),
            "medical_report": report,
            "token_validity": data.get("token_validity"),
            "payments": [{
                "benefit_number": str(c["nb"]) if c.get("nb") is not None else None,
                "competence": c.get("dtCompetenciaCredito"),
                "payment_date": c.get("dtPgtoCredito"),
                "net_amount": c.get("valorLiquido"),
                "status": c.get("tipoStatusCredito"),
                "paid": c.get("inCreditoPago"),
                "bank_returned": c.get("inRetornoBanco"),
                "valid_from": c.get("dtInicioValidade"),
                "valid_until": c.get("dtFimValidade"),
            } for c in data["creditos"]],
            "windows": [{"interval": w["window"], "http_status": w["status"]}
                        for w in data["windows"]],
        }


def main():
    mcp.run()


if __name__ == "__main__":
    main()
