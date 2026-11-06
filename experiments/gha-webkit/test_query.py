import json
from unittest.mock import MagicMock

from query_run import summary
from query_worker import get_json, redact


def test_recursive_redaction_and_embedded_secret_values():
    data = {'name': 'fixture-person', 'miToken': 'private-mi',
            'nested': [{'access_token': 'private-access', 'note': 'prefix private-mi'}]}
    assert redact(data, ('private-mi',)) == {'name': 'fixture-person', 'nested': [{'note': 'prefix [redacted]'}]}


def test_query_is_bounded_and_does_not_follow_redirects():
    response = MagicMock(status_code=200)
    response.__enter__.return_value = response
    response.iter_content.return_value = [b'{"name":"fixture","miToken":"private-mi"}']
    session = MagicMock()
    session.get.return_value = response
    assert get_json(session, '/usuarioservices/info/fixture', ('private-mi',)) == {
        'http_status': 200, 'data': {'name': 'fixture'}}
    assert session.get.call_args.kwargs['allow_redirects'] is False
    response.iter_content.return_value = [b'x' * 2_000_001]
    assert get_json(session, '/fixture', ())['error'] == 'response_limit'


def test_public_summary_has_only_statuses_counts_and_timings():
    payload = {'validation': {'success': True, 'code': 'ok', 'receipt': {'elapsed_ms': 20}},
        'client_data': {'benefits': [{'name': 'fixture-person'}],
            'userinfo': {'http_status': 200, 'data': {'name': 'fixture-person'}},
            'credits': {'http_status': 200, 'data': {'creditosTO': [{'idCredito': 1}]}}}}
    result = summary(payload)
    assert result['benefit_count'] == result['credit_count'] == 1
    assert 'fixture-person' not in json.dumps(result)
    assert 'client_data' not in result and result['raw_tokens_exported'] is False
