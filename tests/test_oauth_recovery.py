import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from auth.captcha import NullCaptcha
from auth.govbr import AuthError, GovBrAuth
from auth.page_diagnostics import classify_page
from inss.http_login import finish_oauth
from inss.login_outcome import login_outcome


@pytest.mark.parametrize('html,expected', [
    ('<input autocomplete="one-time-code">', 'two_factor'),
    ('<input type="hidden" name="otp" value="private">', 'unknown'),
    ('<form>Autorizar acesso e compartilhamento de dados<button>Autorizar</button></form>', 'consent'),
    ('<form>Atualize seus dados</form>', 'account_update'),
    ('<h1>Sessão expirada</h1>', 'session_expired'),
    ('<h1>Serviço temporariamente indisponível</h1>', 'service_error'),
    ('<meta http-equiv="refresh" content="0;url=/private-token">', 'script_navigation'),
    ('<script>"<input name=otp>";"Autorizar acesso";</script>', 'unknown'),
])
def test_page_classification_exports_only_bounded_signals(html, expected):
    diagnostic = classify_page(html + '<p>private-name</p>',
        'https://sso.acesso.gov.br/private-account?token=private-token', 'https://meu.inss.gov.br/')
    assert diagnostic['kind'] == expected
    assert 'private' not in json.dumps(diagnostic)


def test_intermediate_oauth_page_can_resume_sso_using_only_gets():
    calls = []

    def get(url, **kwargs):
        calls.append(urlsplit(url).path)
        if urlsplit(url).path == '/authorize':
            state = parse_qs(urlsplit(url).query)['state'][0]
            return SimpleNamespace(status_code=302, text='', url=url,
                headers={'Location': 'https://meu.inss.gov.br/?code=fixture&state=' + state})
        return SimpleNamespace(status_code=200, text='<html>intermediate</html>', url=url, headers={})

    session = SimpleNamespace(get=get, post=Mock(), headers={})
    receipt = {}
    auth = GovBrAuth(session, NullCaptcha(), redirect_uri='https://meu.inss.gov.br/', diagnostics=receipt)
    auth._next_location = 'https://sso.acesso.gov.br/intermediate'
    hops = finish_oauth(auth, receipt)
    assert calls == ['/intermediate', '/authorize', '/']
    assert auth.authorization_code == 'fixture'
    assert hops[-1][1] == 200
    assert receipt['oauth_resume_retry']['recovered'] is True
    session.post.assert_not_called()


@pytest.mark.parametrize('code,kind', [
    ('invalid_credentials', 'unknown'), ('two_factor_required', 'two_factor'),
    ('state_mismatch', 'unknown'), ('login_not_completed', 'consent'),
    ('login_not_completed', 'account_update'), ('login_not_completed', 'password'),
])
def test_does_not_repeat_credentials_or_interactive_steps(code, kind):
    auth = Mock()
    auth.finish.side_effect = AuthError(code)
    receipt = {'oauth_terminal': {'kind': kind}}
    with pytest.raises(AuthError, match=code):
        finish_oauth(auth, receipt)
    auth.resume.assert_not_called()
    auth.submit_password.assert_not_called()


def test_resume_failure_is_returned_after_only_one_probe():
    auth = Mock()
    auth.finish.side_effect = AuthError('login_not_completed')
    auth.resume.side_effect = AuthError('sso_interaction_required')
    receipt = {'oauth_terminal': {'kind': 'unknown'}}
    with pytest.raises(AuthError, match='sso_interaction_required'):
        finish_oauth(auth, receipt)
    auth.resume.assert_called_once()
    assert receipt['oauth_resume_retry'] == {'first_code': 'login_not_completed',
        'first_page_kind': 'unknown', 'recovered': False, 'code': 'sso_interaction_required'}


def test_upstream_rejection_uses_only_the_existing_oauth_resume_probe():
    auth = Mock()
    auth.finish.side_effect = AuthError('upstream_request_rejected')
    auth.resume.side_effect = AuthError('upstream_request_rejected')
    receipt = {'oauth_terminal': {'kind': 'unknown',
        'public_error_signals': {'request_rejected': True}}}
    with pytest.raises(AuthError, match='^upstream_request_rejected$'):
        finish_oauth(auth, receipt)
    auth.finish.assert_called_once()
    auth.resume.assert_called_once()
    auth.submit_password.assert_not_called()
    assert receipt['oauth_resume_retry'] == {'first_code': 'upstream_request_rejected',
        'first_page_kind': 'unknown', 'recovered': False, 'code': 'upstream_request_rejected'}


@pytest.mark.parametrize('code,status,action,retryable', [
    ('ok', 'valid', 'use_credentials', False),
    ('invalid_credentials', 'invalid', 'check_credentials', False),
    ('two_factor_required', 'requires_two_factor', 'complete_two_factor', False),
    ('read_timeout', 'undetermined', 'retry_later', True),
    ('captcha_http_not_accepted', 'undetermined', 'retry_later', True),
    ('upstream_request_rejected', 'undetermined', 'review_login_flow', False),
    ('login_not_completed', 'undetermined', 'review_login_flow', False),
    ('tls_error', 'undetermined', 'review_login_flow', False),
])
def test_client_action_does_not_confuse_technical_failure_with_wrong_password(code, status, action, retryable):
    assert login_outcome(code, {}) == dict(credential_status=status, next_action=action, retryable=retryable)


def test_consent_is_a_manual_govbr_step_not_a_password_problem():
    assert login_outcome('login_not_completed', {'oauth_terminal': {'kind': 'consent'}}) == dict(
        credential_status='undetermined', next_action='complete_govbr_step', retryable=False)
