import json

import pytest

from auth.page_diagnostics import classify_page


@pytest.mark.parametrize('html,key,expected_kind,matched', [
    ('<title>Request Rejected</title><p>Your support ID is SUPPORT_PLACEHOLDER</p>',
     'request_rejected', 'unknown', True),
    ('<h1>Acesso negado</h1><p>Request blocked</p>', 'access_denied_or_blocked', 'unknown', True),
    ('<title>502 Bad Gateway</title>', 'service_or_gateway_unavailable', 'unknown', True),
    ('<title>Verificação em duas etapas</title>', 'access_denied_or_blocked', 'unknown', False),
    ('<p>Habilite os cookies para continuar</p>', 'cookies_required_or_disabled', 'unknown', True),
    ('<p>Cookies desabilitados</p>', 'cookies_required_or_disabled', 'unknown', True),
    ('<p>Invalid request. Session expired.</p>', 'session_or_request_invalid', 'unknown', True),
])
def test_public_error_signals_are_categorical_and_do_not_change_kind(html, key, expected_kind, matched):
    url = ('https://sso.acesso.gov.br/login?cpf=CPF_PLACEHOLDER'
           '&password=PASSWORD_PLACEHOLDER&token=TOKEN_PLACEHOLDER')
    html += '<input value="CPF_PLACEHOLDER PASSWORD_PLACEHOLDER TOKEN_PLACEHOLDER">'
    diagnostic = classify_page(html, url, '')
    assert diagnostic['kind'] == expected_kind
    assert diagnostic['public_error_signals'][key] is matched
    assert all(isinstance(value, bool) for value in diagnostic['public_error_signals'].values())
    assert all(isinstance(value, int) for value in diagnostic['html_counts'].values())
    rendered = json.dumps(diagnostic)
    for private in ('SUPPORT_PLACEHOLDER', 'CPF_PLACEHOLDER', 'PASSWORD_PLACEHOLDER', 'TOKEN_PLACEHOLDER'):
        assert private not in rendered


def test_public_error_signals_count_structure_without_exporting_text():
    diagnostic = classify_page(
        '<title>Request Rejected</title><form><input><input type="password">'
        '<button>Continue</button><iframe></iframe><script>support token</script></form>',
        'https://sso.acesso.gov.br/login', '')
    assert diagnostic['html_counts'] == {
        'forms': 1, 'inputs': 2, 'buttons': 1, 'iframes': 1, 'scripts': 1}
    assert diagnostic['public_error_signals']['request_rejected'] is True
    assert 'support token' not in json.dumps(diagnostic)
