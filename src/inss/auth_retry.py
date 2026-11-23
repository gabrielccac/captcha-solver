"""Bounded fresh-session recovery before any password has been submitted."""
import time

MAX_ATTEMPTS = 3
RETRY_BUDGET_MS = 200_000
HANDOFF_ERRORS = {'captcha_http_not_accepted', 'upstream_request_rejected', 'read_timeout', 'connect_timeout',
                  'request_timeout', 'connection_error', 'incomplete_http_response'}
ENGINE_ERRORS = {'captcha_unsolved', 'captcha_selection_not_confirmed', 'captcha_images_changed',
                 'captcha_token_not_captured'}


def retry_allowed(result):
    receipt = result.get('receipt', {})
    if result.get('success') or receipt.get('http_password_posts') or receipt.get('browser_password_posts'):
        return False
    if receipt.get('captcha_terminal', {}).get('kind') in ('two_factor', 'consent', 'account_update'):
        return False
    if receipt.get('failure_stage') == 'captcha_engine':
        return result.get('code') in ENGINE_ERRORS
    return (result.get('code') in HANDOFF_ERRORS
            and receipt.get('failure_stage') == 'http_consume_captcha'
            and receipt.get('http_captcha_posts') == 1)


def authenticate_with_retry(authenticate):
    started = time.monotonic()
    history = []
    while True:
        result = authenticate()
        receipt = result.get('receipt', {})
        record = {'attempt': len(history) + 1, 'code': result['code'],
                  'elapsed_ms': result['elapsed_ms'], 'success': result['success']}
        for key in ('failure_stage', 'request_error_type', 'last_http_status', 'http_captcha_posts',
                    'http_password_posts', 'browser_password_posts', 'token_age_at_http_post_ms',
                    'captcha_terminal', 'password_terminal', 'oauth_terminal', 'oauth_resume_retry'):
            if key in receipt:
                record[key] = receipt[key]
        if receipt.get('http_trace'):
            record['http_trace'] = receipt['http_trace'][-64:]
        if receipt.get('rounds'):
            timing_keys = ('round', 'modal_wait_s', 'capture_s', 'extract_s', 'solve_s', 'click_s', 'verdict_s')
            record['captcha_rounds'] = [{k: row[k] for k in timing_keys
                if isinstance(row.get(k), (int, float))} for row in receipt['rounds'][:3]]
        if receipt.get('selection_waits'):
            last = receipt['selection_waits'][-1]
            record['captcha_selection'] = {k: last[k] for k in
                ('failure_phase', 'expected_selection', 'observed_selection') if k in last}
        history.append(record)
        spent = max(round((time.monotonic() - started) * 1000), sum(r['elapsed_ms'] for r in history))
        eligible = retry_allowed(result)
        estimated_next = max(50_000, result['elapsed_ms'] + 15_000)
        if not eligible:
            stop = 'success' if result['success'] else 'not_retryable'
            break
        if len(history) >= MAX_ATTEMPTS:
            stop = 'attempt_limit'
            break
        if spent + estimated_next > RETRY_BUDGET_MS:
            stop = 'time_budget'
            break
    result['elapsed_ms'] = spent
    result['auth_retry'] = dict(attempts=len(history), max_attempts=MAX_ATTEMPTS,
        budget_ms=RETRY_BUDGET_MS, stop_reason=stop,
        recovered=len(history) > 1 and result['success'], history=history)
    if len(history) > 1 and history[0].get('failure_stage') == 'http_consume_captcha':
        first = history[0]
        field = 'captcha_retry' if first['code'] == 'captcha_http_not_accepted' else 'pre_password_retry'
        result[field] = dict(first_worker_ms=first['elapsed_ms'], recovered=result['success'])
        if field == 'pre_password_retry':
            result[field]['first_code'] = first['code']
    return result
