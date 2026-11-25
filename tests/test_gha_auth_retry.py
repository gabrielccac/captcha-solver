import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('first_code,receipt_override,second_code,expected_calls', [
    ('captcha_http_not_accepted', {}, 'ok', 2),
    ('captcha_http_not_accepted', {}, 'captcha_http_not_accepted', 3),
    ('captcha_http_not_accepted', {'failure_stage': 'http_password'}, 'ok', 1),
    ('captcha_http_not_accepted', {'http_password_posts': 1}, 'ok', 1),
    ('captcha_http_not_accepted', {'browser_password_posts': 1}, 'ok', 1),
    ('captcha_http_not_accepted', {'http_captcha_posts': 0}, 'ok', 1),
    ('invalid_credentials', {}, 'ok', 1),
    ('two_factor_required', {}, 'ok', 1),
    ('http_error', {}, 'ok', 1),
    ('read_timeout', {}, 'ok', 2),
    ('connect_timeout', {}, 'ok', 2),
    ('connection_error', {}, 'read_timeout', 3),
    ('read_timeout', {'http_password_posts': 1}, 'ok', 1),
    ('read_timeout', {'failure_stage': 'http_oauth_redirect'}, 'ok', 1),
    ('ok', {}, 'ok', 1),
    ('upstream_request_rejected', {}, 'ok', 2),
    ('upstream_request_rejected', {}, 'upstream_request_rejected', 3),
    ('upstream_request_rejected', {'failure_stage': 'http_password', 'http_password_posts': 1}, 'ok', 2),
    ('upstream_request_rejected', {'failure_stage': 'http_oauth_redirect'}, 'ok', 2),
    ('upstream_request_rejected', {'failure_stage': 'http_authenticated'}, 'ok', 1),
])
def test_auth_worker_retries_only_before_password_with_bounded_attempts(monkeypatch, first_code, receipt_override,
                                                    second_code, expected_calls):
    worker_path = Path(__file__).resolve().parents[1] / 'deploy/actions/auth_worker.py'
    spec = importlib.util.spec_from_file_location('auth_worker', worker_path)
    worker = importlib.util.module_from_spec(spec)
    login_input = {'GOVBR_CPF': '00000000000', 'GOVBR_PASSWORD': 'fixture', 'OPENROUTER_API_KEY': 'fixture'}
    receipt = {'failure_stage': 'http_consume_captcha', 'http_captcha_posts': 1} | receipt_override
    codes = iter((first_code, second_code, second_code))

    def authenticate(*, on_authenticated, credentials):
        assert credentials == login_input
        code = next(codes)
        if code == 'ok':
            headers = dict.fromkeys(worker.HEADER_NAMES, 'fixture-header')
            on_authenticated(SimpleNamespace(headers=headers), login_input['GOVBR_CPF'])
        return {'success': code == 'ok', 'code': code, 'receipt': receipt, 'elapsed_ms': 100}

    authenticate = Mock(side_effect=authenticate)
    monkeypatch.setitem(sys.modules, 'smoke', SimpleNamespace(main=authenticate,
                         http_login=SimpleNamespace(API='https://example.test/')))
    monkeypatch.setitem(sys.modules, 'inss.session', SimpleNamespace(token_validity=lambda *_: {}))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(login_input) + '\n'))
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    spec.loader.exec_module(worker)

    output = worker.main()
    result = output['validation']
    final_code = second_code if expected_calls > 1 else first_code
    assert authenticate.call_count == expected_calls
    assert result['code'] == final_code
    assert result['elapsed_ms'] == 100 * expected_calls
    assert bool(output['client_data']) is (final_code == 'ok')
    if first_code == 'upstream_request_rejected':
        field = 'rejection_retry'
    elif first_code == 'captcha_http_not_accepted':
        field = 'captcha_retry'
    else:
        field = 'pre_password_retry'
    expected = {'first_worker_ms': 100, 'recovered': final_code == 'ok'}
    if field == 'pre_password_retry':
        expected['first_code'] = first_code
    assert result.get(field) == (expected if expected_calls > 1 else None)
    assert result['auth_retry']['attempts'] == expected_calls
    if final_code == 'ok':
        assert set(output['client_data']) == {'api_credentials'}
