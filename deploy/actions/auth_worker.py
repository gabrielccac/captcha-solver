"""Validate a GOV.BR login and emit API credentials only to the encryption pipe."""
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
    credentials = {}

    def capture(session, cpf):
        headers = {name: session.headers[name] for name in HEADER_NAMES}
        credentials.update(base_url=smoke.http_login.API.rstrip('/'), cpf=cpf, headers=headers,
            validity=token_validity(headers['Authorization'].removeprefix('Bearer '), headers['miToken']))

    result = smoke.main(on_authenticated=capture)
    return {'validation': result, 'client_data': {'api_credentials': credentials} if result['success'] else {}}


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            payload = main()
    except Exception as error:
        payload = {'validation': {'success': False, 'code': 'auth_worker_failed',
                                  'error_type': type(error).__name__}, 'client_data': {}}
    print(json.dumps(payload), flush=True)
    raise SystemExit(0 if payload['validation']['success'] else 3)
