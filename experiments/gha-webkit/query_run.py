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

IMAGE = 'ghcr.io/gabrielccac/captcha-solver-webkit-smoke@sha256:62c0e4242870ff30eefae56bd35c446eaba02d093b5dea3f397018496e7103ba'
SECCOMP_SHA = '5f51dd940f5e35a653476ccaf30b3b06b485e3d2d790469d00aa10f0832d2433'
KEYS = ('GOVBR_CPF', 'GOVBR_PASSWORD', 'OPENROUTER_API_KEY')


def encrypt(payload, certificate):
    process = subprocess.run(['openssl', 'cms', '-encrypt', '-binary', '-aes-256-gcm',
        '-outform', 'DER', str(certificate)], input=payload, capture_output=True, timeout=15)
    if process.returncode or not process.stdout:
        raise RuntimeError('result_encryption_failed')
    return process.stdout


def summary(payload):
    validation = payload['validation']
    data = payload.get('client_data', {})
    credits = data.get('credits', {}).get('data', {})
    result = {'success': validation.get('success', False), 'code': validation.get('code'),
        'login_ms': validation.get('receipt', {}).get('elapsed_ms'),
        'worker_ms': validation.get('elapsed_ms'),
        'inss_http_status': validation.get('independent_api_http_status'),
        'userinfo_http_status': data.get('userinfo', {}).get('http_status'),
        'credits_http_status': data.get('credits', {}).get('http_status'),
        'benefit_count': len(data.get('benefits', [])),
        'credit_count': len(credits.get('creditosTO', [])) if isinstance(credits, dict) else None,
        'encrypted_result': True, 'raw_tokens_exported': False}
    if 'captcha_selection_retry' in validation:
        result['captcha_selection_retry'] = validation['captcha_selection_retry']
    return result


def main():
    values = {key: os.environ.pop(key) for key in KEYS}
    token = os.environ.pop('GHCR_TOKEN')
    certificate = os.environ.pop('QUERY_RECIPIENT_CERT')
    worker = os.environ.pop('QUERY_WORKER_FILE', 'query_worker.py')
    mode = os.environ.pop('COMPARE_MODE', 'none')
    assert worker in ('query_worker.py', 'compare_worker.py')
    assert mode in ('none', 'screenshot', 'network')
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
            def run(args):
                subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run(['docker', 'pull', IMAGE])
            extraction = name + '-bundle'
            try:
                run(['docker', 'create', '--name', extraction, '--entrypoint', '/bin/true', IMAGE])
                run(['docker', 'cp', extraction + ':/app/experiments/gha-webkit/seccomp.json', str(root / 'seccomp.json')])
            finally:
                subprocess.run(['docker', 'rm', '-f', extraction], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert hashlib.sha256((root / 'seccomp.json').read_bytes()).hexdigest() == SECCOMP_SHA
            command = ['docker', 'run', '--rm', '--init', '-i', '--name', name, '--log-driver', 'none',
                '--read-only', '--cpus', '1', '--memory', '1400m', '--memory-swap', '1400m',
                '--pids-limit', '320', '--shm-size', '128m', '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777',
                '--security-opt', 'seccomp=' + str(root / 'seccomp.json'),
                '--env', 'COMPARE_MODE=' + mode,
                '--mount', 'type=bind,src=' + str(Path(__file__).with_name(worker).resolve()) + ',dst=/query_worker.py,readonly',
                '--entrypoint', 'python', IMAGE, '-u', '/query_worker.py']
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
            timer = threading.Timer(240, lambda: subprocess.run(['docker', 'rm', '-f', name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            timer.start()
            raw, _ = process.communicate((json.dumps(values) + '\n').encode(), timeout=260)
            assert len(raw) <= 5_000_000
            payload = json.loads(raw)
            # Only authenticated ciphertext can leave the runner as an artifact.
            (output / 'result.cms').write_bytes(encrypt(raw, recipient))
            result = summary(payload)
            result['worker_returncode'] = process.returncode
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
    return 0 if result['success'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
