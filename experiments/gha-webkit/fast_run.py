"""Launch the approved immutable worker without a repository checkout or per-login CI."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

IMAGE = 'ghcr.io/gabrielccac/captcha-solver-webkit-smoke@sha256:62c0e4242870ff30eefae56bd35c446eaba02d093b5dea3f397018496e7103ba'
BUNDLE_HASHES = {
    'run_container.py': 'eb62f4b71c6016bfbd25f8ff4037c19cf5276deab89d6abb0d12d2f2449e1902',
    'seccomp.json': '5f51dd940f5e35a653476ccaf30b3b06b485e3d2d790469d00aa10f0832d2433',
}
KEYS = ('GOVBR_CPF', 'GOVBR_PASSWORD', 'OPENROUTER_API_KEY')


def verify_bundle(directory):
    for name, expected in BUNDLE_HASHES.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError('unapproved_launch_bundle')


def worker_result(output, private):
    if any(value and value in output for value in private):
        raise ValueError('unsafe_worker_output')
    lines = [line.removeprefix('SMOKE_RESULT ') for line in output.splitlines()
             if line.startswith('SMOKE_RESULT ')]
    if len(lines) != 1:
        raise ValueError('missing_or_ambiguous_worker_receipt')
    result = json.loads(lines[0])
    if result.get('success') and not (result.get('independent_api_http_status') == 200
            and result.get('access_token_present') and result.get('mi_token_present')):
        raise ValueError('unverified_worker_success')
    return result


def main():
    mode = os.environ.get('FAST_MODE', 'offline')
    legacy = os.environ.get('LEGACY_VALIDATION') == '1'
    parallelism = int(os.environ.get('PULL_PARALLELISM', '3'))
    assert mode in ('offline', 'live') and parallelism in (3, 6, 10)
    assert not legacy or parallelism == 3
    values = {key: os.environ.pop(key) for key in KEYS} if mode == 'live' else {}
    token = os.environ.pop('GHCR_TOKEN')
    actor = os.environ['GITHUB_ACTOR']
    private = [token, *values.values()]
    started = time.monotonic()
    report = dict(success=False, mode=mode, legacy=legacy, parallelism=parallelism,
                  image=IMAGE, phases_ms={}, start_unix_ms=round(time.time() * 1000),
                  source_fetch_ms=int(os.environ.get('FAST_SOURCE_FETCH_MS', '0')))
    report['runner_image'] = os.environ.get('ImageVersion')
    report['cpu_model'] = next(line.split(':', 1)[1].strip() for line in
        Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name'))
    previous_config = os.environ.get('DOCKER_CONFIG')
    live_started = False

    def run(args, *, timeout=300, **kwargs):
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, **kwargs)
        if result.returncode:
            raise RuntimeError('command_failed_' + args[0])
        return result.stdout

    def phase(name, function):
        start = time.monotonic()
        value = function()
        report['phases_ms'][name] = round((time.monotonic() - start) * 1000)
        return value

    try:
        with tempfile.TemporaryDirectory(prefix='approved-webkit-') as temporary:
            root = Path(temporary)
            registry = root / 'registry'
            registry.mkdir(mode=0o700)
            os.environ['DOCKER_CONFIG'] = str(registry)
            if legacy:
                phase('registry_login', lambda: run(['docker', 'login', 'ghcr.io', '--username', actor,
                      '--password-stdin'], input=token))
            else:
                config = registry / 'config.json'
                auth = base64.b64encode((actor + ':' + token).encode()).decode()
                config.write_text(json.dumps({'auths': {'ghcr.io': {'auth': auth}}}))
                config.chmod(0o600)
            daemon = Path('/etc/docker/daemon.json')
            configured = json.loads(daemon.read_text()) if daemon.exists() else {}
            report['downloads_before'] = configured.get('max-concurrent-downloads', 3)
            if report['downloads_before'] != parallelism:
                # Only the disposable hosted runner is reconfigured, never the VPS.
                assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
                code = ('import json,pathlib; p=pathlib.Path("/etc/docker/daemon.json"); '
                        'v=json.loads(p.read_text()) if p.exists() else {}; '
                        f'v["max-concurrent-downloads"]={parallelism}; '
                        'p.write_text(json.dumps(v))')
                def reload_downloads():
                    run(['sudo', 'python3', '-c', code])
                    run(['sudo', 'dockerd', '--validate', '--config-file=/etc/docker/daemon.json'])
                    run(['sudo', 'systemctl', 'reload', 'docker'])
                phase('download_configuration', reload_downloads)
            report['cold_image'] = subprocess.run(['docker', 'image', 'inspect', IMAGE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
            pulled = phase('pull', lambda: run(['docker', 'pull', IMAGE]))
            report['already_exists_layers'] = pulled.count('Already exists')
            bundle = root / 'experiments/gha-webkit'
            bundle.mkdir(parents=True)
            if legacy:
                for name in BUNDLE_HASHES:
                    (bundle / name).write_bytes((Path('experiments/gha-webkit') / name).read_bytes())
            else:
                name = 'webkit-launch-bundle'
                def extract():
                    try:
                        run(['docker', 'create', '--name', name, '--entrypoint', '/bin/true', IMAGE])
                        for filename in BUNDLE_HASHES:
                            run(['docker', 'cp', name + ':/app/experiments/gha-webkit/' + filename,
                                 str(bundle / filename)])
                    finally:
                        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                phase('extract_bundle', extract)
            verify_bundle(bundle)
            (bundle / 'image.ref').write_text(IMAGE + '\n')
            base = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                    '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777']
            def validate():
                tests = phase('offline_tests', lambda: run(base + ['--entrypoint', 'python', IMAGE,
                    '-m', 'pytest', 'tests/test_auth.py', 'tests/test_browser_captcha.py', '-q', '-p', 'no:cacheprovider']))
                assert '59 passed' in tests
                probe = phase('preflight', lambda: run(base + ['--init', '--cpus', '1', '--memory', '1400m',
                    '--pids-limit', '320', '--shm-size', '128m', '--security-opt',
                    'seccomp=' + str(bundle / 'seccomp.json'), IMAGE, '--offline-preflight']))
                report['preflight'] = json.loads(probe)
                assert report['preflight']['success']
            if legacy:
                validate()
            report['ready_for_login_unix_ms'] = round(time.time() * 1000)
            report['preparation_ms'] = round((time.monotonic() - started) * 1000) + report['source_fetch_ms']
            if mode == 'offline':
                if not legacy:
                    validate()  # Validation is after the preparation measurement, without credentials.
                report['success'] = True
            else:
                environment = dict(os.environ, **values)
                values.clear()
                live_started = True
                def live_worker():
                    try:
                        process = subprocess.run(['python3', str(bundle / 'run_container.py')],
                            cwd=root, env=environment, timeout=280, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                        report['worker_returncode'] = process.returncode
                        return process.stdout
                    finally:
                        environment.clear()
                raw = phase('worker', live_worker)
                report['worker'] = worker_result(raw, private)
                report['success'] = report['worker']['success'] and report['worker_returncode'] == 0
                if report['success']:
                    report['session_ready_unix_ms'] = round(time.time() * 1000)
                    report['session_ready_elapsed_ms'] = round((time.monotonic() - started) * 1000) + report['source_fetch_ms']
                    # Emitted before cleanup; no tokens or credential values are exported.
                    print('SESSION_READY ' + json.dumps({k: report[k] for k in
                          ('session_ready_unix_ms', 'session_ready_elapsed_ms', 'preparation_ms')}), flush=True)
    except Exception as error:
        report.update(code='fast_launch_failed', error_type=type(error).__name__)
    finally:
        if live_started:
            subprocess.run(['docker', 'rm', '-f', 'inss-webkit-smoke'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if previous_config is None:
            os.environ.pop('DOCKER_CONFIG', None)
        else:
            os.environ['DOCKER_CONFIG'] = previous_config
        report['total_ms'] = round((time.monotonic() - started) * 1000) + report['source_fetch_ms']
        rendered = json.dumps(report)
        assert not any(value and value in rendered for value in private)
        Path('fast-launch-result.json').write_text(rendered)
        print('FAST_RESULT ' + rendered, flush=True)
        if mode == 'live':
            worker = dict(report.get('worker', {'success': False, 'code': 'fast_launch_failed'}))
            worker['fast_pipeline'] = {k: v for k, v in report.items() if k != 'worker'}
            print('SMOKE_RESULT ' + json.dumps(worker), flush=True)
        values.clear()
        private.clear()
    return 0 if report['success'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
