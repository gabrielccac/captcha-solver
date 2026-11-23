"""Sanitized HTTP diagnostics without URLs, headers or response bodies."""
from contextlib import contextmanager
from time import monotonic_ns
from urllib.parse import urljoin, urlsplit

import requests

MAX_TRACE_EVENTS = 64


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


def route(url):
    path = urlsplit(url).path.lower()
    if path == '/login' or path.endswith('/login'):
        return 'login'
    if path == '/authorize' or path.endswith('/authorize'):
        return 'authorize'
    return 'other'


def method_category(method):
    value = str(method or '').upper()
    return value if value in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'} else 'OTHER'


def content_type_category(headers):
    content_type = str(headers.get('Content-Type', '')).split(';', 1)[0].strip().lower()
    if content_type == 'text/html':
        return 'html'
    if content_type == 'application/json' or content_type.endswith('+json'):
        return 'json'
    if content_type == 'application/pdf':
        return 'pdf'
    if content_type == 'application/x-www-form-urlencoded':
        return 'form'
    if content_type.startswith('text/'):
        return 'text'
    return 'other' if content_type else 'missing'


def server_family(headers):
    server = str(headers.get('Server', '')).lower()
    if 'bigip' in server or 'big-ip' in server or 'f5' in server:
        return 'bigip'
    if 'cloudflare' in server:
        return 'cloudflare'
    if 'akamai' in server:
        return 'akamai'
    if 'nginx' in server:
        return 'nginx'
    return 'other' if server else 'unknown'


def redirect_metadata(source_url, headers):
    location = headers.get('Location')
    if not location:
        return None, None
    try:
        source, target = urlsplit(source_url), urlsplit(urljoin(source_url, str(location)))
        def origin(parts):
            scheme = parts.scheme.lower()
            return scheme, parts.hostname, parts.port or {'http': 80, 'https': 443}.get(scheme)
        same_origin = origin(source) == origin(target)
        return route(target.geturl()), same_origin
    except ValueError:
        return 'other', False


def cookie_count(headers):
    value = headers.get('Cookie', '')
    return sum(bool(part.strip()) for part in value.split(';')) if value else 0


def byte_length(value):
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode('utf-8', 'replace'))
    return None


def headers_length(headers):
    return sum(len(str(key).encode('utf-8', 'replace')) +
               len(str(value).encode('utf-8', 'replace')) for key, value in headers.items())


def buffered_length(response):
    content = getattr(response, '_content', None)
    return len(content) if isinstance(content, bytes) else None


def append_trace(receipt, event):
    trace = receipt.get('http_trace')
    if not isinstance(trace, list):
        trace = receipt['http_trace'] = []
    trace.append(event)
    del trace[:-MAX_TRACE_EVENTS]


@contextmanager
def observe(session, receipt):
    original = session.send

    def send(request, **kwargs):
        request_url = getattr(request, 'url', '') or ''
        request_headers = getattr(request, 'headers', {}) or {}
        request_method = getattr(request, 'method', '') or ''
        receipt['http_operation'] = endpoint(request_url)
        receipt['http_method'] = request_method
        receipt.pop('last_http_status', None)
        started = monotonic_ns()
        event = {'operation': receipt['http_operation'], 'route': route(request_url),
                 'method': method_category(request_method), 'elapsed_ms': 0,
                 'http_status': None, 'error': None, 'redirect_route': None,
                 'redirect_same_origin': None, 'content_type': 'missing',
                 'request_content_type': content_type_category(request_headers),
                 'request_body_bytes': byte_length(getattr(request, 'body', None)),
                 'headers_bytes': headers_length(request_headers),
                 'request_cookie_count': cookie_count(request_headers),
                 'set_cookie_present': False, 'response_body_bytes': None,
                 'server_family': 'unknown'}
        try:
            response = original(request, **kwargs)
        except Exception as error:
            event['elapsed_ms'] = max(0, round((monotonic_ns() - started) / 1_000_000))
            if isinstance(error, requests.RequestException):
                event['error'] = request_failure(error, receipt)
            else:
                event['error'] = 'unexpected_request_error'
            append_trace(receipt, event)
            raise
        status = getattr(response, 'status_code', None)
        receipt['last_http_status'] = status
        headers = getattr(response, 'headers', {}) or {}
        event.update(elapsed_ms=max(0, round((monotonic_ns() - started) / 1_000_000)),
                     http_status=status, content_type=content_type_category(headers),
                     server_family=server_family(headers),
                     set_cookie_present=bool(headers.get('Set-Cookie')),
                     response_body_bytes=buffered_length(response))
        event['redirect_route'], event['redirect_same_origin'] = redirect_metadata(request_url, headers)
        append_trace(receipt, event)
        return response

    session.send = send
    try:
        yield
    finally:
        session.send = original
