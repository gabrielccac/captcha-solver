"""Shared CLI/MCP query: browser-to-HTTP login, selected APIs, partial results."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import sys
import time

import requests

from auth.govbr import AuthError
from .http_login import load_env_file, login
from .medical import download_medical_report, list_medical_reports
from .session import concession_letters, credit_windows, extract, session_benefits, token_validity


def query_windows(years=1, start_date="", end_date="", *, today=None):
    """ISO input; preserve the API's nonoverlapping calendar-year windows."""
    if type(years) is not int or not 1 <= years <= 5:
        raise ValueError("years must be between 1 and 5")
    today = today or date.today()
    if not start_date and not end_date:
        return credit_windows(today, years=years)
    if not start_date or not end_date:
        raise ValueError("start_date and end_date are required together")
    if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (start_date, end_date)):
        raise ValueError("Dates must use YYYY-MM-DD")
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        raise ValueError("Invalid date") from None
    candidates = credit_windows(end, years=5)
    earliest = datetime.strptime(candidates[-1][0], "%d-%m-%Y").date()
    if start > end or end > today or start < earliest:
        raise ValueError("Date range must be ordered, not future, and within five API windows")
    windows = []
    for first, last in candidates:
        first, last = (datetime.strptime(value, "%d-%m-%Y").date() for value in (first, last))
        if last >= start:
            windows.append((max(first, start).strftime("%d-%m-%Y"), last.strftime("%d-%m-%Y")))
    return windows


def validate_benefit_numbers(numbers):
    if not isinstance(numbers, list) or any(not isinstance(nb, str)
            or not re.fullmatch(r"[0-9]{1,15}", nb) for nb in numbers):
        raise ValueError("Benefit numbers must be a list of numeric strings")
    return list(dict.fromkeys(numbers))


def query(cpf, password, api_key, output_dir, *, years=1, start_date="", end_date="",
          include_payments=True, include_letters=False, letter_benefit_numbers=None,
          medical_report_nb="", fast_interactions=False, batch_captcha=False, receipt=None):
    windows = query_windows(years, start_date, end_date)
    selected = validate_benefit_numbers([] if letter_benefit_numbers is None else letter_benefit_numbers)
    if medical_report_nb:
        validate_benefit_numbers([medical_report_nb])
    receipt = {} if receipt is None else receipt
    started = time.monotonic()
    with requests.Session() as session:
        login(session, cpf, password, api_key, receipt=receipt,
              optimize_interactions=fast_interactions, batch_captcha=batch_captcha)
        receipt["login_ms"] = receipt["elapsed_ms"]
        extracting = time.monotonic()
        auth = session.headers["Authorization"].removeprefix("Bearer ")
        mi = session.headers["miToken"]
        component_ms = {}

        def measured(name, action, *args, **kwargs):
            started = time.monotonic()
            try:
                return action(*args, **kwargs)
            finally:
                component_ms[name] = round((time.monotonic() - started) * 1000)

        # Coordinators wait on the shared request pool; they never occupy its slots.
        # Only the selected medical request uses session.get; other jobs copy headers.
        with ThreadPoolExecutor(max_workers=5) as requests_pool, ThreadPoolExecutor(max_workers=2) as groups:
            payment_job = groups.submit(measured, "payments_ms", extract, cpf, auth, mi,
                windows if include_payments else [], session=session, include_userinfo=False,
                executor=requests_pool)
            letter_job = (groups.submit(measured, "letters_ms", concession_letters, auth, mi,
                output_dir, benefit_numbers=selected if selected else None, session=session,
                executor=requests_pool) if include_letters or selected else None)
            report_job = (requests_pool.submit(measured, "medical_report_ms", download_medical_report,
                auth, mi, medical_report_nb, output_dir, session=session) if medical_report_nb else None)
            data = payment_job.result()
            data.update(userinfo_status=receipt["authenticated_endpoint_http_status"],
                        payments_requested=include_payments, letters_requested=bool(include_letters or selected),
                        concession_letters=[], benefits=[], medical_reports=[], errors=[])
            if letter_job is not None:
                try:
                    data["concession_letters"] = letter_job.result()
                    for letter in data["concession_letters"]:
                        if letter.get("error") or letter.get("parse_error"):
                            data["errors"].append({"component": "concession_letters", "code": "document_incomplete"})
                except ValueError:
                    data["errors"].append({"component": "concession_letters", "code": "benefit_outside_session"})
                except OSError:
                    data["errors"].append({"component": "concession_letters", "code": "local_io_error"})
            if report_job is not None:
                try:
                    data["medical_report"] = report_job.result()
                except ValueError:
                    data["medical_report"] = {"benefit_number": medical_report_nb, "http_status": None,
                        "file": None, "error": "Benefit not listed for medical reports in this session"}
                if data["medical_report"].get("error"):
                    data["errors"].append({"component": "medical_report", "code": "document_unavailable"})
        for window in data["windows"]:
            if window.get("error"):
                data["errors"].append({"component": "payments", "window": window["window"],
                                       "code": window["error"], "http_status": window["status"]})
        try:
            data["benefits"] = session_benefits(mi)
            data["medical_reports"] = list_medical_reports(mi)
        except ValueError:
            data["benefits_error"] = "Session benefit list unavailable"
            data["medical_reports_error"] = "Medical report benefit list unavailable"
            data["errors"].append({"component": "benefits", "code": "invalid_benefit_list"})
        try:
            data["token_validity"] = token_validity(auth, mi)
        except (ValueError, TypeError, OverflowError, OSError):
            data["token_validity"] = None
            data["errors"].append({"component": "token_validity", "code": "invalid_expiry_claims"})
        data["complete"] = not data["errors"]
        data["partial"] = not data["complete"]
        receipt.update(extraction_ms=round((time.monotonic() - extracting) * 1000),
            total_ms=round((time.monotonic() - started) * 1000),
            complete=data["complete"], partial=data["partial"],
            code="ok" if data["complete"] else "partial_results")
        data["timings"] = {key: receipt[key] for key in ("login_ms", "extraction_ms", "total_ms")}
        data["timings"].update(component_ms)
        data["login_timing"] = {key: receipt[key] for key in
            ("phase_ms", "rounds", "selection_waits", "captcha_batch") if key in receipt}
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.error = lambda _: parser.exit(2, "Invalid query arguments.\n")
    parser.add_argument("--env-file", type=Path, default=Path(os.environ.get("INSS_ENV_FILE", ".env")))
    parser.add_argument("--credentials-stdin", action="store_true", help="Read cpf/password JSON from stdin")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--years", type=int, default=1)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--skip-payments", action="store_true")
    parser.add_argument("--include-letters", action="store_true")
    parser.add_argument("--letter-nb", action="append", default=[])
    parser.add_argument("--medical-report-nb", default="")
    parser.add_argument("--fast-interactions", action="store_true")
    parser.add_argument("--batch-captcha", action="store_true")
    args = parser.parse_args()
    receipt = {"success": False}
    try:
        query_windows(args.years, args.start_date, args.end_date)
        validate_benefit_numbers(args.letter_nb)
        if args.medical_report_nb:
            validate_benefit_numbers([args.medical_report_nb])
        values = load_env_file(args.env_file) if args.env_file.exists() else {}
        for key in ("GOVBR_CPF", "GOVBR_PASSWORD", "OPENROUTER_API_KEY"):
            if key in os.environ:
                values[key] = os.environ[key]
        incoming = json.load(sys.stdin) if args.credentials_stdin else {}
        if not isinstance(incoming, dict) or any(not isinstance(value, str) for value in incoming.values()):
            raise ValueError("Invalid credential input")
        cpf = incoming.get("cpf") or values.get("GOVBR_CPF", "")
        if not re.fullmatch(r"\d{11}|\d{3}\.\d{3}\.\d{3}-\d{2}", cpf, flags=re.ASCII):
            raise ValueError("Invalid CPF")
        cpf = re.sub(r"\D", "", cpf)
        local_cpf = re.sub(r"\D", "", values.get("GOVBR_CPF", ""))
        if not incoming.get("password") and local_cpf and local_cpf != cpf:
            raise AuthError("local_credentials_identity_mismatch")
        password = incoming.get("password") or values.get("GOVBR_PASSWORD", "")
        args.out.mkdir(parents=True, exist_ok=True)
        data = query(cpf, password, values.get("OPENROUTER_API_KEY", ""), args.out,
            years=args.years, start_date=args.start_date, end_date=args.end_date,
            include_payments=not args.skip_payments, include_letters=args.include_letters,
            letter_benefit_numbers=args.letter_nb, medical_report_nb=args.medical_report_nb,
            fast_interactions=args.fast_interactions, batch_captcha=args.batch_captcha, receipt=receipt)
        path = args.out / "query.json"
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except AuthError as error:
        receipt.update(success=False, code=error.code)
    except ValueError:
        receipt.update(success=False, code="invalid_query")
    except OSError:
        receipt.update(success=False, code="local_io_error")
    except Exception:
        receipt.update(success=False, code="query_runtime_error")
    if args.out.is_dir():
        (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("success", "code")}))
    return 0 if receipt["success"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
