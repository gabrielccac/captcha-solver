"""One cold runner, one image: pull + offline checks + browser readiness."""
import json
import os
from pathlib import Path
import subprocess
import time


def timed(command, **kwargs):
    start = time.perf_counter()
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=300, **kwargs)
    if result.returncode:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError('benchmark_command_failed: ' + command[0])
    return round(time.perf_counter() - start, 4), result.stdout


def main():
    image = os.environ['WEBKIT_IMAGE']
    report = dict(variant=os.environ['VARIANT'], trial=int(os.environ['TRIAL']), image=image,
                  success=False, runner_image=os.environ.get('ImageVersion'),
                  run_id=os.environ['GITHUB_RUN_ID'], run_attempt=os.environ['GITHUB_RUN_ATTEMPT'])
    try:
        assert subprocess.run(['docker', 'image', 'inspect', image], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode != 0, 'image_already_cached'
        report['cold_image'] = True
        report['docker_version'] = subprocess.check_output(['docker', '--version'], text=True).strip()
        report['cpu_model'] = next(line.split(':', 1)[1].strip() for line in
            Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name'))
        start = time.perf_counter()
        report['pull_seconds'], pull = timed(['docker', 'pull', image])
        report['already_exists_layers'] = pull.count('Already exists')
        base = ['docker', 'run', '--rm', '--network', 'none', '--read-only',
                '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777']
        report['tests_seconds'], tests = timed(base + ['--entrypoint', 'python', image, '-m', 'pytest',
            'tests/test_auth.py', 'tests/test_browser_captcha.py', '-q', '-p', 'no:cacheprovider'])
        assert '59 passed' in tests, 'unexpected_test_count'
        report['offline_tests'] = tests.strip().splitlines()[-1]
        report['preflight_seconds'], output = timed(base + ['--init', '--cpus', '1', '--memory', '1400m',
            '--pids-limit', '320', '--shm-size', '128m', '--security-opt',
            'seccomp=experiments/gha-webkit/seccomp.json', image, '--offline-preflight'])
        report['ready_seconds'] = round(time.perf_counter() - start, 4)
        report['preflight'] = json.loads(output)
        assert report['preflight']['success']
        _, output = timed(base + ['-i', '--user', '0:0', '--entrypoint', 'python', image, '-'],
            input=Path('experiments/gha-webkit/runtime_fingerprint.py').read_text())
        report['runtime_fingerprint'] = json.loads(output)
        assert report['runtime_fingerprint']['sha256'] == os.environ['EXPECTED_FINGERPRINT']
        report['success'] = True
    finally:
        Path('image-benchmark-result.json').write_text(json.dumps(report, indent=2))
        print('PULL_BENCHMARK ' + json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
