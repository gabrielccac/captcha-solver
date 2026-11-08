import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1] / 'deploy' / 'actions'
spec = importlib.util.spec_from_file_location('query_run', ROOT / 'query_run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize('worker_file', ['auth_worker.py', 'jean_worker.py'])
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
    bundle = {'headers': {'Authorization': 'Bearer fixture-access', 'miToken': 'fixture-mi'}}
    worker = Mock(returncode=worker_rc)
    worker.communicate.return_value = (json.dumps({
        'validation': {'success': authenticated, 'code': code,
                       'independent_api_http_status': 200 if authenticated else None},
        'client_data': {'api_credentials': bundle} if authenticated and worker_file == 'auth_worker.py' else {},
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
    assert 'fixture-access' not in json.dumps(receipt)
    assert 'fixture-mi' not in json.dumps(receipt)
    if not encrypt_fails:
        assert receipt['code'] == code
        assert (tmp_path / 'encrypted-query' / 'result.cms').read_bytes() == b'test-ciphertext'
        payload = json.loads(encrypt.call_args.args[0])
        assert payload['validation']['result_class'] == result_class
        if worker_file == 'auth_worker.py':
            assert payload['client_data'].get('api_credentials') == (bundle if result_class == 'authenticated' else None)
        if worker_file == 'jean_worker.py':
            assert payload['client_data']['login_diagnostics']['result_class'] == result_class
    command = runner.subprocess.Popen.call_args.args[0]
    assert f'type=bind,src={ROOT.parents[1] / "src"},dst=/app/src,readonly' in command
    assert f'type=bind,src={ROOT / "smoke.py"},dst=/app/experiments/gha-webkit/smoke.py,readonly' in command


def test_relay_claim_replaces_static_credentials_without_fallback(monkeypatch):
    for key in runner.KEYS:
        monkeypatch.setenv(key, 'fictional-' + key)
    monkeypatch.setenv('INSS_JOB_ID', 'a' * 32)
    monkeypatch.setenv('INSS_CLAIM_URL', 'https://relay.example.test/claim')
    monkeypatch.setenv('INSS_CLAIM_BEARER', 'fixture-relay')
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=Mock(
        read=Mock(return_value=b'{"GOVBR_CPF":"00000000000","GOVBR_PASSWORD":"fixture-password"}')))
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(runner, 'build_opener', Mock(return_value=opener))
    result = runner.credentials()
    assert result['GOVBR_PASSWORD'] == 'fixture-password'
    request = opener.open.call_args.args[0]
    assert json.loads(request.data) == {'job_id': 'a' * 32}
    assert request.get_header('Authorization') == 'Bearer fixture-relay'
    assert 'fixture-password' not in request.data.decode()

    monkeypatch.setenv('INSS_JOB_ID', 'b' * 32)
    monkeypatch.setenv('INSS_CLAIM_URL', 'http://relay.example.test/claim')
    monkeypatch.setenv('INSS_CLAIM_BEARER', 'fixture-relay')
    with pytest.raises(ValueError, match='invalid_claim_configuration'):
        runner.credentials()
    opener.open.assert_called_once()
