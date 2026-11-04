"""Local MCP access to Meu INSS payment history."""
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
from inss.query import query_windows, validate_benefit_numbers

mcp = MCPServer("meu-inss")
_login_lock = Lock()
DOCUMENTS_DIR = Path(os.environ.get("INSS_DOCUMENTS_DIR", str(Path.cwd() / "runs" / "mcp")))


@mcp.tool()
def get_inss_payments(cpf: str, password: str = "", medical_report_nb: str = "",
        years: int = 1, start_date: str = "", end_date: str = "",
        include_payments: bool = True, include_letters: bool = False,
        letter_benefit_numbers: list[str] | None = None,
        fast_interactions: bool = False, batch_captcha: bool = False) -> dict:
    """Query Meu INSS through the same HTTP login/extraction path as inss-query.

    The GOV.BR password is read from GOVBR_PASSWORD in the local .env unless
    one is supplied; it is used only for this call. Payment status fields are returned
    as reported by Meu INSS; they do not independently confirm bank availability.
    Default: one recent API year of payments, benefit/report lists, no PDF downloads.
    years accepts 1..5; alternatively provide both ISO start_date and end_date.
    Set include_payments=False for metadata/documents only. include_letters=True
    downloads all session concession letters; letter_benefit_numbers selects NBs.
    concession_letters contains persistent local PDF paths for requested letters.
    benefits lists all session benefits; payments include their benefit_number.
    medical_reports lists eligible benefits, not confirmed PDF availability.
    Set medical_report_nb to a benefit_number from that list to download its PDF;
    medical_report returns its HTTP status and persistent file_path, or an error.
    Failed windows/documents do not discard successful ones: inspect complete,
    partial, errors and each window status. Authentication failure stops the query.
    Token expiry is declared by the issuer; revocation can invalidate it earlier.
    """
    if not re.fullmatch(r"\d{11}|\d{3}\.\d{3}\.\d{3}-\d{2}", cpf, flags=re.ASCII):
        raise ValueError("CPF must have 11 digits")
    if medical_report_nb and not re.fullmatch(r"[0-9]{1,15}", medical_report_nb):
        raise ValueError("Medical report NB must contain 1 to 15 digits")
    query_windows(years, start_date, end_date)
    selected = validate_benefit_numbers([] if letter_benefit_numbers is None else letter_benefit_numbers)
    digits = "".join(c for c in cpf if c.isdigit())
    started = time.monotonic()
    with _login_lock, tempfile.TemporaryDirectory(prefix="inss-mcp-") as work:
        queue_ms = round((time.monotonic() - started) * 1000)
        out = Path(work) / "out"
        command = [sys.executable, "-m", "inss.query", "--credentials-stdin",
                   "--out", str(out), "--years", str(years)]
        if start_date:
            command.extend(["--start-date", start_date, "--end-date", end_date])
        for enabled, flag in ((not include_payments, "--skip-payments"),
                (include_letters, "--include-letters"), (fast_interactions, "--fast-interactions"),
                (batch_captcha, "--batch-captcha")):
            if enabled:
                command.append(flag)
        for nb in selected:
            command.extend(["--letter-nb", nb])
        if medical_report_nb:
            command.extend(["--medical-report-nb", medical_report_nb])
        try:
            result = subprocess.run(command, input=json.dumps({"cpf": digits, "password": password}),
                                    capture_output=True,
                                    text=True, timeout=300, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Meu INSS login timed out") from None
        if result.returncode:
            try:
                failure = json.loads(result.stdout)
                code = failure.get("code")
                if code in ("invalid_credentials", "two_factor_required", "solver_provider_error",
                            "captcha_unsolved", "network_error", "captcha_selection_not_confirmed",
                            "password_missing_in_env", "solver_key_missing", "invalid_query",
                            "local_credentials_identity_mismatch"):
                    return {
                        "cpf_mask": f"{digits[:3]}.***.***-{digits[-2:]}",
                        "error_code": code, "error": "Meu INSS query did not complete",
                    }
            except (ValueError, AttributeError):
                pass
            raise RuntimeError("Meu INSS query failed")
        data = json.loads((out / "query.json").read_text(encoding="utf-8"))
        if data.get("userinfo_status") != 200:
            raise RuntimeError("Meu INSS customer request failed")
        letters = data.get("concession_letters", [])
        report = data.get("medical_report")
        documents = letters + ([report] if report else [])
        if any(document.get("file") for document in documents):
            try:
                DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
                destination = Path(tempfile.mkdtemp(prefix="inss-", dir=DOCUMENTS_DIR))
            except OSError:
                destination = None
            for document in documents:
                filename = document.pop("file", None)
                if filename:
                    source = (out / filename).resolve()
                    if source.parent != out.resolve() or source.suffix != ".pdf":
                        raise RuntimeError("Invalid document file path")
                    try:
                        if destination is None:
                            raise OSError("Document directory unavailable")
                        target = destination / source.name
                        shutil.move(source, target)
                        target.chmod(0o600)
                        document["file_path"] = str(target)
                    except OSError:
                        document["error"] = "Document could not be persisted"
                        data.update(complete=False, partial=True)
                        data.setdefault("errors", []).append({"component": "documents", "code": "local_io_error"})
        timings = data.get("timings", {})
        timings.update(queue_ms=queue_ms, mcp_total_ms=round((time.monotonic() - started) * 1000))
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
            "complete": data.get("complete", True),
            "partial": data.get("partial", False),
            "errors": data.get("errors", []),
            "payments_requested": data.get("payments_requested", True),
            "letters_requested": data.get("letters_requested", False),
            "timings": timings,
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
            "windows": [{"interval": w["window"], "http_status": w["status"],
                         "error": w.get("error"), "elapsed_s": w.get("elapsed_s")}
                        for w in data["windows"]],
        }


def main():
    mcp.run()


if __name__ == "__main__":
    main()
