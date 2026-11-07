"""Use the approved login unchanged; return private query data only to a pipe."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
import re
import sys

sys.path.insert(0, '/app/experiments/gha-webkit')
import smoke
from inss.session import session_benefits, token_validity


def redact(value, secrets=()):
    private = {'accesstoken', 'refreshtoken', 'idtoken', 'mitoken', 'token', 'jwt',
               'authorization', 'cookie', 'cookies', 'setcookie', 'password'}
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()
                if re.sub('[^a-z]', '', key.lower()) not in private}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[redacted]')
    return value


def get_json(session, path, secrets):
    try:
        with session.get(smoke.http_login.API.rstrip('/') + path, timeout=(5, 30),
                         allow_redirects=False, stream=True) as response:
            result = {'http_status': response.status_code}
            if response.status_code != 200:
                return dict(result, error='upstream_http_error')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 2_000_000:
                    return dict(result, error='response_limit')
                chunks.append(chunk)
            try:
                return dict(result, data=redact(json.loads(b''.join(chunks)), secrets))
            except (ValueError, UnicodeError):
                return dict(result, error='invalid_json')
    except Exception as error:
        return {'error': 'query_failed', 'error_type': type(error).__name__}


def login_with_selection_retry(run, input_line):
    """Reopen a fresh profile only if visual verification stopped before any handoff."""
    original_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(input_line)
        first = run()
        receipt = first.get('receipt') or {}
        posts = ('http_captcha_posts', 'http_password_posts', 'browser_password_posts')
        if (first.get('success') or first.get('code') != 'captcha_selection_not_confirmed'
                or any(receipt.get(key) != 0 for key in posts)):
            return first

        sys.stdin = io.StringIO(input_line)
        final = run()
        waits = receipt.get('selection_waits') or []
        final['captcha_selection_retry'] = {
            'first_code': first['code'],
            'first_elapsed_ms': receipt.get('elapsed_ms'),
            'first_failure_phase': waits[-1].get('failure_phase') if waits else None,
            'first_password_posts': receipt['http_password_posts'] + receipt['browser_password_posts'],
            'first_captcha_posts': receipt['http_captcha_posts'],
            'recovered': bool(final.get('success')),
        }
        if isinstance(first.get('elapsed_ms'), int) and isinstance(final.get('elapsed_ms'), int):
            final['elapsed_ms'] += first['elapsed_ms']
        return final
    finally:
        sys.stdin = original_stdin


def main():
    collected = {}
    original_login = smoke.http_login.login

    def login(session, cpf, password, api_key, **kwargs):
        receipt = original_login(session, cpf, password, api_key, **kwargs)
        access = session.headers['Authorization'].removeprefix('Bearer ')
        mi = session.headers['miToken']
        private = (access, mi, password, api_key)
        collected['benefits'] = session_benefits(mi)
        collected['token_validity'] = token_validity(access, mi)
        collected['userinfo'] = get_json(session, f'/usuarioservices/info/{cpf}?refreshPedidos=false', private)
        today = date.today()
        start, end = date(today.year, 1, 1).strftime('%d-%m-%Y'), today.strftime('%d-%m-%Y')
        collected['credits_period'] = {'start': start, 'end': end}
        collected['credits'] = get_json(session,
            f'/hiscreServices/historicocreditos/{cpf.lstrip("0") or "0"}/{start}/{end}', private)
        return receipt

    smoke.http_login.login = login
    try:
        result = login_with_selection_retry(smoke.main, sys.stdin.readline())
    finally:
        smoke.http_login.login = original_login
    return {'validation': result, 'client_data': collected}


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = main()
    except BaseException as error:
        result = {'validation': {'success': False, 'code': 'query_worker_failed',
                                 'error_type': type(error).__name__}, 'client_data': {}}
    # The host captures this private stdout; Docker logging is disabled.
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result['validation']['success'] else 3)
