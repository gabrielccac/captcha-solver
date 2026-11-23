"""Validate a GOV.BR login and emit API credentials to the encryption pipe."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sys

sys.path.insert(0, '/app/experiments/gha-webkit')
import smoke
from inss.session import token_validity


HEADER_NAMES = (
    'Authorization', 'miToken', 'Accept', 'Origin', 'Referer',
    'userId', 'sessionId', 'User-Agent',
)


def main():
    login_input = json.loads(sys.stdin.readline())
    client_data = {}

    def capture(session, cpf):
        headers = {name: session.headers[name] for name in HEADER_NAMES}
        client_data['api_credentials'] = dict(base_url=smoke.http_login.API.rstrip('/'), cpf=cpf, headers=headers,
            validity=token_validity(headers['Authorization'].removeprefix('Bearer '), headers['miToken']))

    try:
        result = smoke.main(on_authenticated=capture, credentials=login_input)
        receipt = result['receipt']
        if (result['code'] in {'captcha_http_not_accepted', 'read_timeout', 'connect_timeout',
                              'request_timeout', 'connection_error', 'incomplete_http_response'}
                and receipt.get('failure_stage') == 'http_consume_captcha'
                and receipt.get('http_captcha_posts') == 1
                and not receipt.get('http_password_posts')
                and not receipt.get('browser_password_posts')):
            first_elapsed_ms = result['elapsed_ms']
            first_code = result['code']
            result = smoke.main(on_authenticated=capture, credentials=login_input)
            field = 'captcha_retry' if first_code == 'captcha_http_not_accepted' else 'pre_password_retry'
            result[field] = {'first_worker_ms': first_elapsed_ms, 'recovered': result['success']}
            if field == 'pre_password_retry':
                result[field]['first_code'] = first_code
            result['elapsed_ms'] += first_elapsed_ms
    finally:
        login_input.clear()
    return {'validation': result, 'client_data': client_data if result['success'] else {}}


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            payload = main()
    except Exception as error:
        payload = {'validation': {'success': False, 'code': 'auth_worker_failed',
                                  'error_type': type(error).__name__}, 'client_data': {}}
    print(json.dumps(payload), flush=True)
    raise SystemExit(0 if payload['validation']['success'] else 3)
