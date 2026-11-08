"""One Actions query with recipient-encrypted output; private stdout is never logged."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

IMAGE = 'ghcr.io/gabrielccac/captcha-solver-webkit-smoke@sha256:62e6266bad61e67eaa62eb2ca6f69493b1801d9077e2e2b117693c61c28d5571'
SECCOMP_SHA = '5f51dd940f5e35a653476ccaf30b3b06b485e3d2d790469d00aa10f0832d2433'
KEYS = ('GOVBR_CPF', 'GOVBR_PASSWORD', 'OPENROUTER_API_KEY')
CLIENT_ACTION_CODES = {'invalid_credentials', 'two_factor_required'}
SOURCE_ROOT = Path(__file__).resolve().parents[2]
RUNNER_DIR = Path(__file__).resolve().parent


def encrypt(payload, certificate):
    process = subprocess.run(['openssl', 'cms', '-encrypt', '-binary', '-aes-256-gcm',
        '-outform', 'DER', str(certificate)], input=payload, capture_output=True, timeout=15)
    if process.returncode or not process.stdout:
        raise RuntimeError('result_encryption_failed')
    return process.stdout


def summary(payload):
    validation = payload['validation']
    receipt = validation.get('receipt', {})
    data = payload.get('client_data', {})
    credits = data.get('credits', {}).get('data', {})
    result = {'success': validation.get('success', False), 'code': validation.get('code'),
        'login_ms': receipt.get('elapsed_ms'),
        'worker_ms': validation.get('elapsed_ms'),
        'inss_http_status': validation.get('independent_api_http_status'),
        'userinfo_http_status': data.get('userinfo', {}).get('http_status'),
        'credits_http_status': data.get('credits', {}).get('http_status'),
        'benefit_count': len(data.get('benefits', [])),
        'credit_count': len(credits.get('creditosTO', [])) if isinstance(credits, dict) else None,
        'encrypted_result': True, 'raw_tokens_exported': False}
    if 'captcha_selection_retry' in validation:
        result['captcha_selection_retry'] = validation['captcha_selection_retry']
    for key in ('failure_stage', 'failure_http_status'):
        if key in receipt:
            result[key] = receipt[key]
    if validation.get('code') == 'captcha_selection_not_confirmed' and receipt.get('selection_waits'):
        selection = receipt['selection_waits'][-1]
        result['captcha_selection'] = {key: selection[key] for key in
            ('failure_phase', 'expected_selection', 'observed_selection') if key in selection}
    return result


def main():
    values = {key: os.environ.pop(key) for key in KEYS}
    token = os.environ.pop('GHCR_TOKEN')
    certificate = os.environ.pop('QUERY_RECIPIENT_CERT')
    worker = os.environ.pop('QUERY_WORKER_FILE', 'query_worker.py')
    assert worker in ('query_worker.py', 'jean_worker.py')
    previous_config = os.environ.get('DOCKER_CONFIG')
    name, process, timer = 'inss-private-query', None, None
    result = {'success': False, 'code': 'query_launch_failed', 'encrypted_result': False}
    output = Path('encrypted-query')
    output.mkdir(exist_ok=False)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='private-query-') as temporary:
            root = Path(temporary)
            os.environ['DOCKER_CONFIG'] = str(root)
            auth = base64.b64encode((os.environ['GITHUB_ACTOR'] + ':' + token).encode()).decode()
            config = root / 'config.json'
            config.write_text(json.dumps({'auths': {'ghcr.io': {'auth': auth}}}))
            config.chmod(0o600)
            recipient = root / 'recipient.pem'
            recipient.write_text(certificate)
            seccomp = (RUNNER_DIR.parent / 'seccomp.json').read_text(encoding='utf-8').encode()
            assert hashlib.sha256(seccomp).hexdigest() == SECCOMP_SHA
            (root / 'seccomp.json').write_bytes(seccomp)
            subprocess.run(['docker', 'pull', IMAGE], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            command = ['docker', 'run', '--rm', '--init', '-i', '--name', name, '--log-driver', 'none',
                '--read-only', '--cpus', '1', '--memory', '1400m', '--memory-swap', '1400m',
                '--pids-limit', '320', '--shm-size', '128m', '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777',
                '--security-opt', 'seccomp=' + str(root / 'seccomp.json')]
            mounts = {SOURCE_ROOT / 'src': '/app/src'}
            mounts[RUNNER_DIR / 'smoke.py'] = '/app/experiments/gha-webkit/smoke.py'
            mounts[RUNNER_DIR / worker] = '/query_worker.py'
            if worker == 'jean_worker.py':
                mounts[RUNNER_DIR / 'query_worker.py'] = '/query_support.py'
            for source, target in mounts.items():
                command += ['--mount', f'type=bind,src={source},dst={target},readonly']
            command += ['--entrypoint', 'python', IMAGE, '-u', '/query_worker.py']
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
            timer = threading.Timer(240, lambda: subprocess.run(['docker', 'rm', '-f', name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            timer.start()
            raw, _ = process.communicate((json.dumps(values) + '\n').encode(), timeout=260)
            assert len(raw) <= 5_000_000
            payload = json.loads(raw)
            report = summary(payload)
            report['worker_returncode'] = process.returncode
            if process.returncode == 0 and report['success']:
                report['result_class'] = 'authenticated'
            elif process.returncode == 3 and not report['success'] and report['code'] in CLIENT_ACTION_CODES:
                report['result_class'] = 'client_action_required'
            else:
                report['result_class'] = 'technical_failure'
            payload['validation']['result_class'] = report['result_class']
            if worker == 'jean_worker.py':
                payload['client_data']['login_diagnostics'] = {key: report[key] for key in (
                    'result_class', 'failure_stage', 'failure_http_status', 'captcha_selection', 'captcha_selection_retry',
                ) if key in report}
            raw = json.dumps(payload).encode()
            # Only authenticated ciphertext can leave the runner as an artifact.
            (output / 'result.cms').write_bytes(encrypt(raw, recipient))
            result = report
    except Exception as error:
        result['error_type'] = type(error).__name__
    finally:
        if timer:
            timer.cancel()
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if previous_config is None:
            os.environ.pop('DOCKER_CONFIG', None)
        else:
            os.environ['DOCKER_CONFIG'] = previous_config
        result['pipeline_ms'] = round((time.monotonic() - started) * 1000)
        rendered = json.dumps(result)
        assert not any(value and value in rendered for value in [token, *values.values()])
        (output / 'receipt.json').write_text(rendered)
        values.clear()
        print('SMOKE_RESULT ' + rendered, flush=True)
    completed = result['success'] and result.get('worker_returncode') == 0
    return 0 if completed or result.get('result_class') == 'client_action_required' else 3


if __name__ == '__main__':
    raise SystemExit(main())
