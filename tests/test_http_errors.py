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
    session.headers.update(Authorization='PASSWORD_PLACEHOLDER',
                           Cookie='cpf=CPF_PLACEHOLDER; token=TOKEN_PLACEHOLDER')
    def send(*args, **kwargs):
        raise requests.ReadTimeout('token-in-private-url')
    monkeypatch.setattr(session, 'send', send)
    receipt = {}
    with observe(session, receipt), pytest.raises(requests.ReadTimeout):
        session.post('https://vip-pmeuinss-api.inss.gov.br/apis/usuarioservices/info/CPF_PLACEHOLDER'
                     '?token=TOKEN_PLACEHOLDER', data={'password': 'PASSWORD_PLACEHOLDER'})
    assert receipt['http_operation'] == 'inss_userinfo'
    assert receipt['request_error_type'] == 'ReadTimeout'
    event = receipt['http_trace'][-1]
    assert event['operation'] == 'inss_userinfo'
    assert event['route'] == 'other'
    assert event['method'] == 'POST'
    assert event['http_status'] is None and event['error'] == 'read_timeout'
    assert event['request_cookie_count'] == 2
    assert event['request_content_type'] == 'form'
    assert event['request_body_bytes'] > 0
    assert event['headers_bytes'] > 0
    assert isinstance(event['elapsed_ms'], int)
    assert session.send is send
    rendered = json.dumps(receipt)
    for private in ('CPF_PLACEHOLDER', 'PASSWORD_PLACEHOLDER', 'TOKEN_PLACEHOLDER'):
        assert private not in rendered


@pytest.mark.parametrize('location,same_origin', [
    ('/authorize?cpf=CPF_PLACEHOLDER&password=PASSWORD_PLACEHOLDER&token=TOKEN_PLACEHOLDER', True),
    ('https://sso.acesso.gov.br:443/authorize?cpf=CPF_PLACEHOLDER&password=PASSWORD_PLACEHOLDER'
     '&token=TOKEN_PLACEHOLDER', True),
    ('https://operator.example.test:443/authorize?cpf=CPF_PLACEHOLDER&password=PASSWORD_PLACEHOLDER'
     '&token=TOKEN_PLACEHOLDER', False),
])
def test_observer_records_safe_redirect_metadata_without_consuming_stream(monkeypatch, location, same_origin):
    session = requests.Session()
    session.headers['Cookie'] = 'session=TOKEN_PLACEHOLDER'
    response = requests.Response()
    response.status_code = 302
    response.headers.update({
        'Content-Type': 'text/html; charset=utf-8',
        'Location': location,
        'Set-Cookie': 'session=TOKEN_PLACEHOLDER; HttpOnly',
        'Server': 'nginx/1.2',
    })
    response._content = b'<html>TOKEN_PLACEHOLDER</html>'
    monkeypatch.setattr(session, 'send', lambda request, **kwargs: response)
    receipt = {}
    with observe(session, receipt):
        session.get('https://sso.acesso.gov.br/login?cpf=CPF_PLACEHOLDER', allow_redirects=False)
    event = receipt['http_trace'][-1]
    assert event['operation'] == 'govbr_sso'
    assert event['route'] == 'login'
    assert event['redirect_route'] == 'authorize'
    assert event['redirect_same_origin'] is same_origin
    assert event['content_type'] == 'html'
    assert event['request_content_type'] == 'missing'
    assert event['request_body_bytes'] == 0
    assert event['headers_bytes'] > 0
    assert event['set_cookie_present'] is True
    assert event['response_body_bytes'] == len(response._content)
    assert event['server_family'] == 'nginx'
    rendered = json.dumps(receipt)
    for private in ('CPF_PLACEHOLDER', 'PASSWORD_PLACEHOLDER', 'TOKEN_PLACEHOLDER'):
        assert private not in rendered


def test_observer_does_not_read_streamed_response(monkeypatch):
    session = requests.Session()
    response = requests.Response()
    response.status_code = 200
    response.headers['Content-Type'] = 'application/json'
    response._content = False
    response._content_consumed = False
    monkeypatch.setattr(session, 'send', lambda request, **kwargs: response)
    receipt = {}
    with observe(session, receipt):
        returned = session.get('https://sso.acesso.gov.br/authorize', stream=True)
    assert returned is response
    assert response._content is False and not response._content_consumed
    assert receipt['http_trace'][-1]['response_body_bytes'] is None


def test_http_trace_is_bounded():
    from inss.http_errors import append_trace
    receipt = {}
    for index in range(70):
        append_trace(receipt, {'index': index})
    assert len(receipt['http_trace']) == 64
    assert receipt['http_trace'][0]['index'] == 6
