import base64
import json
from types import SimpleNamespace

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
