import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1] / 'deploy' / 'actions'
spec = importlib.util.spec_from_file_location('query_run', ROOT / 'query_run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize('worker_file', ['query_worker.py', 'jean_worker.py'])
@pytest.mark.parametrize('code,worker_rc,encrypt_fails,exit_code,result_class', [
    ('ok', 0, False, 0, 'authenticated'),
    ('ok', 137, False, 3, 'technical_failure'),
    ('invalid_credentials', 3, False, 0, 'client_action_required'),
    ('two_factor_required', 3, False, 0, 'client_action_required'),
    ('login_not_completed', 3, False, 3, 'technical_failure'),
    ('captcha_selection_not_confirmed', 3, False, 3, 'technical_failure'),
    ('invalid_credentials', 137, False, 3, 'technical_failure'),
    ('invalid_credentials', 3, True, 3, None),
])
def test_query_finishes_only_when_expected_and_delivered(
        monkeypatch, tmp_path, worker_file, code, worker_rc, encrypt_fails, exit_code, result_class):
    monkeypatch.chdir(tmp_path)
    for key in (*runner.KEYS, 'GHCR_TOKEN', 'QUERY_RECIPIENT_CERT', 'GITHUB_ACTOR'):
        monkeypatch.setenv(key, 'fictional-' + key)
    if worker_file == 'jean_worker.py':
        monkeypatch.setenv('QUERY_WORKER_FILE', worker_file)
    else:
        monkeypatch.delenv('QUERY_WORKER_FILE', raising=False)
    authenticated = code == 'ok'
    worker = Mock(returncode=worker_rc)
    worker.communicate.return_value = (json.dumps({
        'validation': {'success': authenticated, 'code': code}, 'client_data': {},
    }).encode(), None)
    monkeypatch.setattr(runner.subprocess, 'Popen', Mock(return_value=worker))

    monkeypatch.setattr(runner.subprocess, 'run', Mock())
    monkeypatch.setattr(runner.threading, 'Timer', Mock())
    encrypt = Mock(return_value=b'test-ciphertext', side_effect=OSError if encrypt_fails else None)
    monkeypatch.setattr(runner, 'encrypt', encrypt)

    assert runner.main() == exit_code
    receipt = json.loads((tmp_path / 'encrypted-query' / 'receipt.json').read_text())
    assert receipt['success'] is (authenticated and not encrypt_fails)
    assert receipt.get('result_class') == result_class
    if not encrypt_fails:
        assert receipt['code'] == code
        assert (tmp_path / 'encrypted-query' / 'result.cms').read_bytes() == b'test-ciphertext'
        payload = json.loads(encrypt.call_args.args[0])
        assert payload['validation']['result_class'] == result_class
        if worker_file == 'jean_worker.py':
            assert payload['client_data']['login_diagnostics']['result_class'] == result_class
    command = runner.subprocess.Popen.call_args.args[0]
    assert f'type=bind,src={ROOT.parents[1] / "src"},dst=/app/src,readonly' in command
    assert f'type=bind,src={ROOT / "smoke.py"},dst=/app/experiments/gha-webkit/smoke.py,readonly' in command
