import json

import pytest
import requests

from auth.govbr import AuthError
from inss.http_errors import observe, request_failure
from inss.http_login import response_json


@pytest.mark.parametrize('error,expected', [
    (requests.ReadTimeout('private-url-password'), 'read_timeout'),
    (requests.ConnectTimeout('private-url-password'), 'connect_timeout'),
    (requests.exceptions.SSLError('private-url-password'), 'tls_error'),
    (requests.exceptions.InvalidURL('private-url-password'), 'request_configuration_error'),
    (requests.exceptions.InvalidHeader('private-url-password'), 'request_configuration_error'),
    (requests.ConnectionError('private-url-password'), 'connection_error'),
])
def test_distinguishes_http_failures_without_leaking(error, expected):
    receipt = {}
    assert request_failure(error, receipt) == expected
    assert receipt['request_error_type'] == type(error).__name__
    assert 'private' not in json.dumps(receipt)


def test_json_error_is_not_reported_as_a_network_failure():
    response = requests.Response()
    response.status_code = 200
    response._content = b'<html>private content</html>'
    receipt = {}
    with pytest.raises(AuthError) as failure:
        response_json(response, receipt)
    assert failure.value.code == 'invalid_json_response'
    assert receipt == {'request_error_type': 'JSONDecodeError',
                       'request_error_code': 'invalid_json_response', 'last_http_status': 200}


def test_observer_preserves_endpoint_and_removes_private_identifiers(monkeypatch):
    session = requests.Session()
    def send(*args, **kwargs):
        raise requests.ReadTimeout('token-in-private-url')
    monkeypatch.setattr(session, 'send', send)
    receipt = {}
    with observe(session, receipt), pytest.raises(requests.ReadTimeout):
        session.get('https://vip-pmeuinss-api.inss.gov.br/apis/usuarioservices/info/12345678900')
    assert receipt['http_operation'] == 'inss_userinfo'
    assert receipt['request_error_type'] == 'ReadTimeout'
    assert session.send is send
    assert '12345678900' not in json.dumps(receipt)
