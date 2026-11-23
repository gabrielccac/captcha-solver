"""Separate proven credential problems from inconclusive transport failures."""


def login_outcome(code, receipt):
    if code == 'ok':
        return dict(credential_status='valid', next_action='use_credentials', retryable=False)
    if code == 'invalid_credentials':
        return dict(credential_status='invalid', next_action='check_credentials', retryable=False)
    if code == 'two_factor_required':
        return dict(credential_status='requires_two_factor', next_action='complete_two_factor', retryable=False)
    kind = receipt.get('oauth_terminal', {}).get('kind')
    if kind in ('consent', 'account_update'):
        action = 'complete_govbr_step'
    elif code in {'read_timeout', 'connect_timeout', 'connection_error', 'request_timeout',
                  'captcha_http_not_accepted', 'captcha_selection_not_confirmed', 'captcha_unsolved'}:
        action = 'retry_later'
    else:
        action = 'review_login_flow'
    return dict(credential_status='undetermined', next_action=action, retryable=action == 'retry_later')
