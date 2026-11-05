"""One diagnostic login, identical browser image/resources; credentials only on stdin."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from fast_run import IMAGE as BASE_IMAGE, KEYS, verify_bundle

PATCH_IMAGE = 'ghcr.io/gabrielccac/captcha-solver-webkit-smoke@sha256:62c0e4242870ff30eefae56bd35c446eaba02d093b5dea3f397018496e7103ba'
IMAGE = os.environ.pop('PROBE_IMAGE', BASE_IMAGE)
assert IMAGE in (BASE_IMAGE, PATCH_IMAGE)


def main():
    values = {key: os.environ.pop(key) for key in KEYS}
    token = os.environ.pop('GHCR_TOKEN')
    name = 'inss-captcha-probe'
    output = Path('captcha-diagnostics').resolve()
    output.mkdir(mode=0o777, exist_ok=False)
    output.chmod(0o777)
    result = dict(success=False, code='probe_launch_failed')
    old_config = os.environ.get('DOCKER_CONFIG')
    process = None
    timer = None
    try:
        with tempfile.TemporaryDirectory(prefix='captcha-probe-') as temporary:
            root = Path(temporary)
            auth = base64.b64encode((os.environ['GITHUB_ACTOR'] + ':' + token).encode()).decode()
            config = root / 'config.json'
            config.write_text(json.dumps({'auths': {'ghcr.io': {'auth': auth}}}))
            config.chmod(0o600)
            os.environ['DOCKER_CONFIG'] = str(root)
            def run(args):
                subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run(['docker', 'pull', IMAGE])
            extraction = name + '-bundle'
            try:
                run(['docker', 'create', '--name', extraction, '--entrypoint', '/bin/true', IMAGE])
                for filename in ('seccomp.json', 'run_container.py'):
                    run(['docker', 'cp', extraction + ':/app/experiments/gha-webkit/' + filename, str(root / filename)])
            finally:
                subprocess.run(['docker', 'rm', '-f', extraction], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            verify_bundle(root)
            command = ['docker', 'run', '--rm', '--init', '-i', '--name', name, '--log-driver', 'none',
                '--read-only', '--cpus', '1', '--memory', '1400m', '--memory-swap', '1400m',
                '--pids-limit', '320', '--shm-size', '128m', '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777',
                '--security-opt', 'seccomp=' + str(root / 'seccomp.json'),
                '--mount', 'type=bind,src=' + str(Path(__file__).with_name('captcha_probe.py').resolve()) + ',dst=/captcha_probe.py,readonly',
                '--mount', 'type=bind,src=' + str(output) + ',dst=/diagnostics',
                '--entrypoint', 'python', IMAGE, '-u', '/captcha_probe.py']
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)
            timer = threading.Timer(240, lambda: subprocess.run(['docker', 'rm', '-f', name],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            timer.start()
            raw, _ = process.communicate(json.dumps(values) + '\n', timeout=260)
            assert not any(value and value in raw for value in [token, *values.values()])
            result = json.loads(raw)
    except Exception as error:
        result.update(code='probe_launch_failed', error_type=type(error).__name__)
    finally:
        if timer:
            timer.cancel()
        if process is not None:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if old_config is None:
            os.environ.pop('DOCKER_CONFIG', None)
        else:
            os.environ['DOCKER_CONFIG'] = old_config
        result['probe_image'] = IMAGE
        rendered = json.dumps(result)
        assert not any(value and value in rendered for value in [token, *values.values()])
        values.clear()
        (output / 'probe-result.json').write_text(rendered)
        print('SMOKE_RESULT ' + rendered, flush=True)
    return 0 if result['success'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
