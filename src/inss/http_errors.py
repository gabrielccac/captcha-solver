"""Sanitized HTTP diagnostics without URLs, headers or response bodies."""
from contextlib import contextmanager
from urllib.parse import urlsplit

import requests


def request_failure(error, receipt):
    kinds = (
        (requests.exceptions.JSONDecodeError, 'invalid_json_response'),
        (requests.exceptions.ConnectTimeout, 'connect_timeout'),
        (requests.exceptions.ReadTimeout, 'read_timeout'),
        (requests.exceptions.Timeout, 'request_timeout'),
        (requests.exceptions.SSLError, 'tls_error'),
        (requests.exceptions.InvalidURL, 'request_configuration_error'),
        (requests.exceptions.InvalidHeader, 'request_configuration_error'),
        (requests.exceptions.ContentDecodingError, 'response_decoding_error'),
        (requests.exceptions.ChunkedEncodingError, 'incomplete_http_response'),
        (requests.exceptions.ConnectionError, 'connection_error'),
    )
    code = next((code for kind, code in kinds if isinstance(error, kind)), 'http_request_error')
    receipt['request_error_type'] = type(error).__name__
    receipt['request_error_code'] = code
    return code


def endpoint(url):
    parts = urlsplit(url)
    if parts.hostname == 'sso.acesso.gov.br':
        return 'govbr_sso'
    if parts.hostname == 'vip-pmeuinss-api.inss.gov.br':
        if parts.path.startswith('/apis/govbrservices/token/'):
            return 'inss_code_exchange'
        if parts.path.startswith('/apis/usuarioservices/info/'):
            return 'inss_userinfo'
        return 'inss_api'
    return 'other_http_service'


@contextmanager
def observe(session, receipt):
    original = session.send

    def send(request, **kwargs):
        receipt['http_operation'] = endpoint(request.url)
        receipt['http_method'] = request.method
        receipt.pop('last_http_status', None)
        try:
            response = original(request, **kwargs)
        except requests.RequestException as error:
            request_failure(error, receipt)
            raise
        receipt['last_http_status'] = response.status_code
        return response

    session.send = send
    try:
        yield
    finally:
        session.send = original
