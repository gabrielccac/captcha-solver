from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from auth.govbr import AuthError, GovBrAuth, LoginPage
from auth.captcha import NullCaptcha
from auth.handoff import password_page
from auth.page_diagnostics import classify_page
from inss.auth_retry import authenticate_with_retry
from inss.login_outcome import login_outcome


def result(code, stage='http_consume_captcha', *, elapsed=30_000, posted=0):
    return {'code': code, 'success': code == 'ok', 'elapsed_ms': elapsed,
            'receipt': {'failure_stage': stage, 'http_captcha_posts': 1,
                        'http_password_posts': posted, 'browser_password_posts': 0}}


def test_recovers_on_third_attempt_without_replaying_a_password():
    run = Mock(side_effect=[result('captcha_http_not_accepted'), result('read_timeout'), result('ok')])
    output = authenticate_with_retry(run)
    assert run.call_count == 3 and output['success']
    assert [r['code'] for r in output['auth_retry']['history']] == ['captcha_http_not_accepted', 'read_timeout', 'ok']
    assert output['auth_retry']['recovered']


def test_engine_failure_gets_a_fresh_challenge_and_stops_at_three():
    run = Mock(side_effect=lambda: result('captcha_unsolved', 'captcha_engine'))
    output = authenticate_with_retry(run)
    assert run.call_count == 3
    assert output['auth_retry']['stop_reason'] == 'attempt_limit'


def test_time_budget_prevents_starting_another_slow_attempt():
    run = Mock(side_effect=lambda: result('read_timeout', elapsed=100_000))
    output = authenticate_with_retry(run)
    assert run.call_count == 1
    assert output['auth_retry']['stop_reason'] == 'time_budget'


@pytest.mark.parametrize('code,posted', [('invalid_credentials', 0), ('two_factor_required', 0),
    ('govbr_interaction_required', 0), ('read_timeout', 1), ('captcha_http_not_accepted', 1)])
def test_nonretryable_results_do_not_repeat_authentication(code, posted):
    run = Mock(side_effect=lambda: result(code, posted=posted))
    authenticate_with_retry(run)
    assert run.call_count == 1


@pytest.mark.parametrize('html,code,kind', [
    ('<form><input name="password" type="password"><p>Captcha inválido</p></form>', 'captcha_http_not_accepted', 'captcha_error'),
    ('<h1>Verificação em duas etapas</h1><p>Use o aplicativo.</p>', 'two_factor_required', 'two_factor'),
    ('<button>Autorizar acesso e compartilhamento de dados</button>', 'govbr_interaction_required', 'consent'),
    ('<form>Atualize seus dados</form>', 'govbr_interaction_required', 'account_update'),
    ('<h1>Atualize seus dados</h1><form><input name="password"></form>', 'govbr_interaction_required', 'account_update'),
    ('<p>Sessão expirada</p>', 'login_response_unrecognized', 'session_expired'),
    ('<main>Resposta inesperada</main>', 'login_response_unrecognized', 'unknown'),
    ('<main>Serviço temporariamente indisponível</main>', 'login_response_unrecognized', 'service_error'),
    ('<meta http-equiv="refresh" content="0;url=/login">', 'login_response_unrecognized', 'script_navigation'),
])
def test_handoff_identifies_terminal_pages_without_submitting_password(html, code, kind):
    session = Mock()
    response = SimpleNamespace(status_code=200, text=html, url='https://sso.acesso.gov.br/login', headers={})
    receipt = {}
    with pytest.raises(AuthError) as error:
        password_page(session, response, diagnostics=receipt)
    assert error.value.code == code
    assert receipt['captcha_terminal']['kind'] == kind
    session.post.assert_not_called()


@pytest.mark.parametrize('status,code', [(400, 'http_error'), (403, 'http_forbidden'), (429, 'rate_limited')])
def test_generic_handoff_http_errors_keep_their_status_classification(status, code):
    response = SimpleNamespace(status_code=status, text='<main>Resposta indisponível</main>',
        url='https://sso.acesso.gov.br/login', headers={})
    with pytest.raises(AuthError) as error:
        password_page(Mock(), response, diagnostics={})
    assert error.value.code == code
    assert error.value.http_status == status


@pytest.mark.parametrize('code,retryable', [
    ('login_response_unrecognized', False),
    ('captcha_http_not_accepted', True),
    ('incomplete_http_response', True),
])
def test_login_outcome_retries_only_classified_transient_failures(code, retryable):
    outcome = login_outcome(code, {})
    assert outcome['retryable'] is retryable
    if code == 'login_response_unrecognized':
        assert outcome['next_action'] == 'review_login_flow'


def test_password_terminal_captures_account_update_instead_of_blaming_password():
    session = Mock(headers={})
    auth = GovBrAuth(session, NullCaptcha(), diagnostics={})
    auth.page = LoginPage(html='<form method="post" action="/login"><input name="password"></form>', url='https://sso.acesso.gov.br/login')
    session.post.return_value = SimpleNamespace(status_code=200, text='<button>Atualize seus dados</button>', url=auth.page.url, headers={})
    with pytest.raises(AuthError, match='govbr_interaction_required'):
        auth.submit_password('fixture', captcha_required=False)
    assert auth.diagnostics['password_terminal']['kind'] == 'account_update'
    assert login_outcome('govbr_interaction_required', auth.diagnostics)['next_action'] == 'complete_govbr_step'


def test_page_diagnostic_keeps_only_provider_codes_and_structural_signature():
    data = classify_page('<h1>Erro ERL0003800</h1><p>private-name</p><input value="private-token">',
        'https://sso.acesso.gov.br/login?private-cpf', '')
    assert data['provider_error_codes'] == ['erl0003800']
    assert len(data['structure_fingerprint']) == 16
    assert 'private' not in str(data)


def test_retry_history_does_not_copy_arbitrary_solver_details():
    data = result('ok')
    data['receipt']['rounds'] = [{'round': 0, 'solve_s': 4, 'private_body': 'private-secret'}]
    output = authenticate_with_retry(lambda: data)
    assert output['auth_retry']['history'][0]['captcha_rounds'] == [{'round': 0, 'solve_s': 4}]
    assert 'private' not in str(output['auth_retry'])


def test_password_update_form_is_not_submitted_as_a_login():
    session = Mock(headers={})
    auth = GovBrAuth(session, NullCaptcha(), diagnostics={})
    auth.page = LoginPage('https://sso.acesso.gov.br/login',
        '<h1>Cadastre nova senha</h1><form method="post"><input name="password"></form>')
    with pytest.raises(AuthError, match='govbr_interaction_required'):
        auth.submit_password('fixture', captcha_required=False)
    session.post.assert_not_called()


def test_explicit_captcha_rejection_with_http_400_is_retryable_before_password():
    response = SimpleNamespace(status_code=400, text='<p>CAPTCHA inválido</p>',
        url='https://sso.acesso.gov.br/login', headers={})
    receipt = {}
    with pytest.raises(AuthError, match='captcha_http_not_accepted'):
        password_page(Mock(), response, diagnostics=receipt)
    assert receipt['captcha_terminal']['http_status'] == 400
