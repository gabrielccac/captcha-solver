import base64
import json
from types import SimpleNamespace
import hashlib

from inss import actions_query

PDF = b"%PDF-1.4 fixture %%EOF"


def test_pdf_accepts_raw_bytes_and_base64_envelopes():
    encoded = base64.b64encode(PDF).decode()
    assert actions_query._pdf(SimpleNamespace(content=PDF)) == PDF
    assert actions_query._pdf(SimpleNamespace(content=json.dumps({"pdf": encoded}).encode())) == PDF
    assert actions_query._pdf(SimpleNamespace(content=json.dumps(encoded).encode())) == PDF
    assert actions_query._pdf(SimpleNamespace(content=b'{"erro":"x"}')) is None


def test_protocol_matches_only_the_requested_number(monkeypatch):
    payload = {"numeroProtocolo": "123.456.789", "status": "Concluída", "servico": {"nome": "Aposentadoria"}}
    monkeypatch.setattr(actions_query.requests, "get",
                        lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload))
    found = actions_query._protocol({}, "123456789")
    assert found["found"] and found["status"] == "Concluída" and found["service"] == "Aposentadoria"
    assert not actions_query._protocol({}, "999999999")["found"]


def test_query_keeps_history_and_pdf_when_one_letter_cannot_be_parsed(monkeypatch):
    from pathlib import Path

    session = SimpleNamespace(headers={'Authorization': 'Bearer fixture', 'miToken': 'fixture'})
    monkeypatch.setattr(actions_query, 'session_benefits', lambda _: [])
    monkeypatch.setattr(actions_query, 'credit_windows', lambda **_: [('01-01-2025', '01-01-2026')])
    monkeypatch.setattr(actions_query, 'extract', lambda *a, **k: {
        'windows': [{'status': 200}], 'creditos': [{'idCredito': 1}], 'beneficios': [], 'n_unique_credits': 1})
    monkeypatch.setattr(actions_query, '_statement', lambda *args: ({'http_status': 200}, PDF))

    def letters(auth, mi, folder, **kwargs):
        Path(folder, 'carta.pdf').write_bytes(PDF)
        return [{'benefit_number': '123', 'file': 'carta.pdf', 'parse_error': 'pdf_text_extraction_failed'},
                {'benefit_number': '456', 'file': None, 'http_status': 404}]

    monkeypatch.setattr(actions_query, 'concession_letters', letters)
    result = actions_query.run(session, '00000000000', {})
    assert result['history']['n_unique_credits'] == 1
    assert len(result['documents']) == 2
    assert result['documents'][0]['sha256'] == hashlib.sha256(PDF).hexdigest()
    assert result['concession_letters'][0]['parse_error'] == 'pdf_text_extraction_failed'
    assert not result['complete']
    assert {error['component'] for error in result['errors']} == {'concession_letter_text', 'concession_letters'}
