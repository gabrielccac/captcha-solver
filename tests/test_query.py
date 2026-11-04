"""Offline shared query tests: no SSO, model provider or real account data."""
import base64
from datetime import date, datetime, timedelta
import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from unittest.mock import Mock

import pytest
import requests

from auth.govbr import AuthError
from inss import query, session


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Query tests cannot access the network")
    monkeypatch.setattr(requests.sessions.Session, "request", denied)


def jwt(**claims):
    return "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"


@pytest.fixture
def login(monkeypatch):
    def authenticated(client, *args, receipt, **kwargs):
        client.headers.update(Authorization="Bearer " + jwt(sub="12345678901"),
            miToken=jwt(nbs=["1234567890", "9876543210"], nbsJson={"listaBeneficios": [
                {"numero": "1234567890", "codigoEspecie": 31},
                {"numero": "9876543210", "codigoEspecie": 21}]}))
        receipt.update(success=True, code="ok", elapsed_ms=10,
                       authenticated_endpoint_http_status=200)
    login = Mock(side_effect=authenticated)
    monkeypatch.setattr(query, "login", login)
    return login


def response(body=None, status=200):
    return Mock(status_code=status, json=Mock(return_value=body or {}))


def test_date_windows_cover_exact_range_without_overlap_or_gaps():
    windows = query.query_windows(start_date="2024-02-29", end_date="2026-02-28", today=date(2026, 3, 1))
    ordered = sorted((datetime.strptime(a, "%d-%m-%Y").date(),
                      datetime.strptime(b, "%d-%m-%Y").date()) for a, b in windows)
    assert ordered[0][0] == date(2024, 2, 29)
    assert ordered[-1][1] == date(2026, 2, 28)
    assert all(left[1] + timedelta(days=1) == right[0] for left, right in zip(ordered, ordered[1:]))
    assert len(query.query_windows(today=date(2026, 3, 1))) == 1
    assert len(query.query_windows(years=5, today=date(2026, 3, 1))) == 5


@pytest.mark.parametrize("options", [{"years": 0}, {"years": True}, {"years": 6},
    {"start_date": "2026-01-01"}, {"start_date": "2026-02-30", "end_date": "2026-03-01"},
    {"start_date": "2000-01-01", "end_date": "2026-03-01"},
    {"start_date": "2026-02-01", "end_date": "2026-01-01"},
    {"start_date": "2026-01-01", "end_date": "2099-01-01"}])
def test_invalid_selection_stops_before_login(tmp_path, login, options):
    with pytest.raises(ValueError):
        query.query("12345678901", "fixture-secret", "fixture-key", tmp_path, **options)
    login.assert_not_called()


def test_window_timeout_keeps_other_window_credits_and_status(monkeypatch):
    def get(url, **kwargs):
        if "/01-01-2026/" in url:
            return response({"creditosTO": [{"idCredito": 1, "nb": "1234567890"}],
                             "listaNbHiscreTO": ["1234567890"]})
        raise requests.Timeout("private signed URL and account details")
    monkeypatch.setattr(session.requests, "get", get)
    with requests.Session() as client:
        client.headers.update(Authorization="Bearer fixture", miToken="fixture")
        data = session.extract("12345678901", "fixture", "fixture",
            [("01-01-2026", "31-01-2026"), ("01-02-2026", "28-02-2026")],
            session=client, include_userinfo=False)
    assert len(data["creditos"]) == 1
    assert data["windows"][0]["status"] == 200
    assert data["windows"][1]["status"] is None
    assert data["windows"][1]["error"] == "network_error"
    assert "private" not in json.dumps(data)


@pytest.mark.parametrize("failed", [response(status=500),
    response(["not an object"]), response({"creditosTO": ["not a credit"]}),
    Mock(status_code=200, json=Mock(side_effect=requests.exceptions.JSONDecodeError("fixture", "", 0))),
    Mock(status_code=200, json=Mock(side_effect=ValueError("private token")))])
def test_invalid_window_response_does_not_discard_successful_sibling(monkeypatch, failed):
    def get(url, **kwargs):
        if "/01-01-2026/" in url:
            return response({"creditosTO": [{"idCredito": 1}]})
        return failed
    monkeypatch.setattr(session.requests, "get", get)
    with requests.Session() as client:
        data = session.extract("12345678901", "fixture", "fixture",
            [("01-01-2026", "31-01-2026"), ("01-02-2026", "28-02-2026")],
            session=client, include_userinfo=False)
    assert data["creditos"] == [{"idCredito": 1}]
    assert data["windows"][1]["error"] in ("http_error", "invalid_response")
    assert "private" not in json.dumps(data)


def test_default_uses_one_window_same_http_session_and_no_pdf_requests(monkeypatch, tmp_path, login):
    extract = Mock(return_value={"windows": [], "creditos": [], "n_unique_credits": 0})
    letters, medical = Mock(), Mock()
    monkeypatch.setattr(query, "extract", extract)
    monkeypatch.setattr(query, "concession_letters", letters)
    monkeypatch.setattr(query, "download_medical_report", medical)
    receipt = {}
    data = query.query("12345678901", "fixture-secret", "fixture-key", tmp_path, receipt=receipt)
    assert len(extract.call_args.args[3]) == 1
    assert extract.call_args.kwargs["session"] is login.call_args.args[0]
    assert extract.call_args.kwargs["include_userinfo"] is False
    letters.assert_not_called()
    medical.assert_not_called()
    assert data["complete"] and not data["partial"]
    assert len(data["benefits"]) == 2 and len(data["medical_reports"]) == 1
    assert data["timings"]["total_ms"] >= data["timings"]["extraction_ms"]
    assert "fixture-secret" not in json.dumps([data, receipt])


def test_metadata_and_selected_document_queries_do_not_request_credit_history(monkeypatch, tmp_path, login):
    extract = Mock(return_value={"windows": [], "creditos": []})
    letters = Mock(return_value=[{"benefit_number": "1234567890", "http_status": 200, "file": "fixture.pdf"}])
    monkeypatch.setattr(query, "extract", extract)
    monkeypatch.setattr(query, "concession_letters", letters)
    data = query.query("12345678901", "fixture-secret", "fixture-key", tmp_path,
                       include_payments=False, letter_benefit_numbers=["1234567890"])
    assert extract.call_args.args[3] == []
    assert letters.call_args.kwargs["benefit_numbers"] == ["1234567890"]
    assert data["letters_requested"] and not data["payments_requested"]


def test_partial_windows_and_documents_are_returned_together(monkeypatch, tmp_path, login):
    monkeypatch.setattr(query, "extract", Mock(return_value={"creditos": [{"idCredito": 1}],
        "windows": [{"window": "fixture", "status": None, "error": "network_error"}]}))
    monkeypatch.setattr(query, "concession_letters", Mock(return_value=[
        {"file": "fixture.pdf", "http_status": 200},
        {"file": None, "http_status": None, "error": "Carta request timed out or could not connect"}]))
    monkeypatch.setattr(query, "download_medical_report", Mock(return_value={
        "file": None, "http_status": 404, "error": "Medical report unavailable"}))
    receipt = {}
    data = query.query("12345678901", "fixture-secret", "fixture-key", tmp_path,
        include_letters=True, medical_report_nb="1234567890", receipt=receipt)
    assert data["partial"] and not data["complete"]
    assert len(data["creditos"]) == 1 and data["concession_letters"][0]["file"]
    assert {error["component"] for error in data["errors"]} == {"payments", "concession_letters", "medical_report"}
    assert receipt["success"] and receipt["code"] == "partial_results"


def test_document_directory_failure_keeps_successful_payments(monkeypatch, tmp_path, login):
    monkeypatch.setattr(query, "extract", Mock(return_value={"creditos": [{"idCredito": 1}], "windows": []}))
    monkeypatch.setattr(query, "concession_letters", Mock(side_effect=OSError("private path")))
    data = query.query("12345678901", "fixture-secret", "fixture-key", tmp_path, include_letters=True)
    assert data["partial"] and data["creditos"] == [{"idCredito": 1}]
    assert data["errors"] == [{"component": "concession_letters", "code": "local_io_error"}]
    assert "private" not in json.dumps(data)


def test_authentication_failure_never_requests_account_data(monkeypatch, tmp_path):
    monkeypatch.setattr(query, "login", Mock(side_effect=AuthError("two_factor_required")))
    extract = Mock()
    monkeypatch.setattr(query, "extract", extract)
    with pytest.raises(AuthError, match="two_factor_required"):
        query.query("12345678901", "fixture-secret", "fixture-key", tmp_path)
    extract.assert_not_called()


def test_cli_uses_same_service_and_keeps_stdin_credentials_out_of_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(query.sys, "argv", ["inss-query", "--out", str(tmp_path),
        "--credentials-stdin", "--env-file", str(tmp_path / "missing.env"), "--skip-payments"])
    monkeypatch.setattr(query.sys, "stdin", io.StringIO(json.dumps({"cpf": "12345678901", "password": "fixture-secret"})))
    def run(*args, receipt, **kwargs):
        assert args[:2] == ("12345678901", "fixture-secret")
        assert kwargs["include_payments"] is False
        receipt.update(success=True, code="partial_results")
        return {"partial": True, "creditos": [{"idCredito": 1}]}
    service = Mock(side_effect=run)
    monkeypatch.setattr(query, "query", service)
    assert query.main() == 0
    assert json.loads((tmp_path / "query.json").read_text())["partial"]
    assert "fixture-secret" not in capsys.readouterr().out


def test_selected_letters_reject_cross_account_nbs_and_fetch_only_requested(monkeypatch, tmp_path):
    getter = Mock(return_value=Mock(status_code=404))
    monkeypatch.setattr(session.requests, "get", getter)
    mi = jwt(nbs=["1234567890", "9876543210"])
    results = session.concession_letters("fixture", mi, tmp_path, benefit_numbers=["1234567890"])
    assert len(results) == 1 and getter.call_count == 1
    with pytest.raises(ValueError):
        session.concession_letters("fixture", mi, tmp_path, benefit_numbers=["1111111111"])
    assert getter.call_count == 1


def test_explicit_empty_windows_do_not_expand_to_five_years(monkeypatch):
    getter = Mock()
    monkeypatch.setattr(session.requests, "get", getter)
    with requests.Session() as client:
        data = session.extract("12345678901", "fixture", "fixture", [], session=client, include_userinfo=False)
    assert data["windows"] == []
    getter.assert_not_called()


@pytest.mark.parametrize("supplied", ["", "per-call-secret"])
def test_cli_local_password_fallback_and_per_call_override(monkeypatch, tmp_path, supplied):
    path = tmp_path / "credentials.env"
    path.write_text("GOVBR_CPF=12345678901\nGOVBR_PASSWORD=local-secret\nOPENROUTER_API_KEY=fixture-key\n")
    for key in ("GOVBR_CPF", "GOVBR_PASSWORD", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(query.sys, "argv", ["inss-query", "--out", str(tmp_path),
        "--env-file", str(path), "--credentials-stdin"])
    monkeypatch.setattr(query.sys, "stdin", io.StringIO(json.dumps({"cpf": "12345678901", "password": supplied})))
    def run(cpf, password, key, *args, receipt, **kwargs):
        assert password == (supplied or "local-secret")
        assert key == "fixture-key"
        receipt.update(success=True, code="ok")
        return {"complete": True}
    monkeypatch.setattr(query, "query", run)
    assert query.main() == 0


def test_cli_does_not_apply_bound_local_password_to_another_cpf(monkeypatch, tmp_path, capsys):
    path = tmp_path / "credentials.env"
    path.write_text("GOVBR_CPF=12345678901\nGOVBR_PASSWORD=local-secret\nOPENROUTER_API_KEY=fixture-key\n")
    for key in ("GOVBR_CPF", "GOVBR_PASSWORD", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(query.sys, "argv", ["inss-query", "--out", str(tmp_path),
        "--env-file", str(path), "--credentials-stdin"])
    monkeypatch.setattr(query.sys, "stdin", io.StringIO(json.dumps({"cpf": "98765432100"})))
    service = Mock()
    monkeypatch.setattr(query, "query", service)
    assert query.main() == 3
    service.assert_not_called()
    assert json.loads(capsys.readouterr().out)["code"] == "local_credentials_identity_mismatch"


def test_payment_and_document_requests_overlap_in_the_same_query(monkeypatch, tmp_path, login):
    rendezvous = Barrier(2, timeout=5)
    def get(url, **kwargs):
        rendezvous.wait()
        if "historicocreditos" in url:
            return response({"creditosTO": [{"idCredito": 1}]})
        return response(status=404)
    monkeypatch.setattr(session.requests, "get", get)
    data = query.query("12345678901", "fixture-secret", "fixture-key", tmp_path,
                       letter_benefit_numbers=["1234567890"])
    assert data["creditos"] == [{"idCredito": 1}]
    assert data["concession_letters"][0]["http_status"] == 404
    assert data["timings"]["payments_ms"] >= 0
    assert data["timings"]["letters_ms"] >= 0


def test_all_payment_and_letter_requests_share_a_five_worker_bound(monkeypatch, tmp_path):
    ready, release = Event(), Event()
    lock = Lock()
    active = peak = 0
    def authenticate(client, *args, receipt, **kwargs):
        client.headers.update(Authorization="Bearer " + jwt(sub="12345678901"),
            miToken=jwt(nbs=[str(i) for i in range(8)], nbsJson={"listaBeneficios": []}))
        receipt.update(success=True, elapsed_ms=0, authenticated_endpoint_http_status=200)
    def get(url, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 5:
                ready.set()
        try:
            assert release.wait(5)
            return response({"creditosTO": []}) if "historicocreditos" in url else response(status=404)
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(query, "login", authenticate)
    monkeypatch.setattr(session.requests, "get", get)
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(query.query, "12345678901", "fixture-secret", "fixture-key", tmp_path,
                               years=5, include_letters=True)
        try:
            assert ready.wait(5)
            assert peak == 5
        finally:
            release.set()
        result = future.result(timeout=10)
    assert peak == 5
    assert len(result["windows"]) == 5 and len(result["concession_letters"]) == 8
