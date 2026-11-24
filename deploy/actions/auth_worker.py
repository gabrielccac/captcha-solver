"""Validate a GOV.BR login and emit API credentials to the encryption pipe."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, '/app/experiments/gha-webkit')
import smoke
from inss.session import token_validity
from inss.login_outcome import login_outcome
from inss.auth_retry import authenticate_with_retry


HEADER_NAMES = (
    'Authorization', 'miToken', 'Accept', 'Origin', 'Referer',
    'userId', 'sessionId', 'User-Agent',
)


def query_diagnostics(session, cpf):
    """Request the real documents once and publish their status and hashes, never the PDFs."""
    directory = os.environ.get('INSS_DIAG_DIR')
    if not directory or os.environ.get('INSS_DIAG_QUERY') != '1':
        return
    from inss import actions_query
    started = time.monotonic()
    record = {}
    try:
        result = actions_query.run(session, cpf, {'years': 1, 'medical_reports': False})
        record.update(complete=result.get('complete'), statement=result.get('statement'),
                      history_windows=[{key: window.get(key) for key in ('window', 'http_status', 'error')}
                                       for window in (result.get('history') or {}).get('windows', [])],
                      documents=[{key: document.get(key) for key in ('kind', 'filename', 'bytes', 'sha256')}
                                 for document in result.get('documents', [])],
                      benefits=len(result.get('benefits') or []),
                      letters=len(result.get('concession_letters') or []),
                      errors=result.get('errors'))
    except Exception as error:
        record['error'] = f'{type(error).__name__}: {str(error)[:200]}'
    record['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    (Path(directory) / 'query.json').write_text(json.dumps(record, indent=1), encoding='utf-8')


def main():
    login_input = json.loads(sys.stdin.readline())
    client_data = {}

    def capture(session, cpf):
        headers = {name: session.headers[name] for name in HEADER_NAMES}
        client_data['api_credentials'] = dict(base_url=smoke.http_login.API.rstrip('/'), cpf=cpf, headers=headers,
            validity=token_validity(headers['Authorization'].removeprefix('Bearer '), headers['miToken']))
        query_diagnostics(session, cpf)

    def authenticate():
        client_data.clear()
        return smoke.main(on_authenticated=capture, credentials=login_input)

    try:
        result = authenticate_with_retry(authenticate)
    finally:
        login_input.clear()
    result.update(login_outcome(result['code'], result['receipt']))
    return {'validation': result, 'client_data': client_data if result['success'] else {}}


if __name__ == '__main__':
    probe = None
    try:
        if os.environ.get('INSS_DIAG_DIR'):
            sys.path.insert(0, '/diag-deps')
            import captcha_probe
            probe = captcha_probe.install(os.environ['INSS_DIAG_DIR'])
            payload = main()
        else:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                payload = main()
    except Exception as error:
        if os.environ.get('INSS_DIAG_DIR'):
            import traceback
            traceback.print_exc()
        payload = {'validation': {'success': False, 'code': 'auth_worker_failed',
                                  'error_type': type(error).__name__}, 'client_data': {}}
    finally:
        if probe is not None:
            probe.flush()
    print(json.dumps(payload), flush=True)
    raise SystemExit(0 if payload['validation']['success'] else 3)
