import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1] / 'deploy' / 'actions'
spec = importlib.util.spec_from_file_location('query_run', ROOT / 'query_run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


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
        monkeypatch, tmp_path, code, worker_rc, encrypt_fails, exit_code, result_class):
    monkeypatch.chdir(tmp_path)
    for key in (*runner.KEYS, 'GHCR_TOKEN', 'QUERY_RECIPIENT_CERT', 'GITHUB_ACTOR'):
        monkeypatch.setenv(key, 'fictional-' + key)
    authenticated = code == 'ok'
    bundle = {'headers': {'Authorization': 'Bearer fixture-access', 'miToken': 'fixture-mi'}}
    worker = Mock(returncode=worker_rc)
    worker.communicate.return_value = (json.dumps({
        'validation': {'success': authenticated, 'code': code,
                       'independent_api_http_status': 200 if authenticated else None},
        'client_data': {'api_credentials': bundle} if authenticated else {},
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
        assert payload['client_data'].get('api_credentials') == (bundle if result_class == 'authenticated' else None)
        assert payload['client_data']['login_diagnostics']['result_class'] == result_class
    command = runner.subprocess.Popen.call_args.args[0]
    assert f'type=bind,src={ROOT.parents[1] / "src"},dst=/app/src,readonly' in command
    assert f'type=bind,src={ROOT / "smoke.py"},dst=/app/experiments/gha-webkit/smoke.py,readonly' in command
    assert command[-1] == '/auth_worker.py'


@pytest.mark.parametrize('delivered', [True, False])
def test_sealed_query_runs_in_worker_and_posts_result_to_callback(monkeypatch, tmp_path, delivered):
    monkeypatch.chdir(tmp_path)
    for key in ('GHCR_TOKEN', 'GITHUB_ACTOR', 'OPENROUTER_API_KEY'):
        monkeypatch.setenv(key, 'fictional-' + key)
    monkeypatch.setenv('INSS_SEALED_CREDENTIALS', 'v1.sealed')
    monkeypatch.setenv('INSS_REQUEST_ID', 'a' * 32)
    monkeypatch.setenv('GITHUB_RUN_ID', '77')
    options = {'years': 2, 'protocol': '', 'medical_reports': False}
    claim = {'GOVBR_CPF': '00000000000', 'GOVBR_PASSWORD': 'fixture-password', 'result_key': 'k',
             'callback_url': 'https://n8n.example.test/webhook-waiting/1', 'options': options}

    def fake_envelope(operation, value):
        return json.dumps(claim).encode() if operation == 'open' else b'v1.n.t.body'
    monkeypatch.setattr(runner, 'envelope', fake_envelope)
    worker = Mock(returncode=0)
    worker.communicate.return_value = (json.dumps({
        'validation': {'success': True, 'code': 'ok', 'independent_api_http_status': 200},
        'client_data': {'query': {'complete': True, 'documents': []}}}).encode(), None)
    monkeypatch.setattr(runner.subprocess, 'Popen', Mock(return_value=worker))
    monkeypatch.setattr(runner.subprocess, 'run', Mock())
    monkeypatch.setattr(runner.threading, 'Timer', Mock())
    deliver = Mock(return_value=delivered)
    monkeypatch.setattr(runner, 'deliver', deliver)

    assert runner.main() == 0
    sent = json.loads(worker.communicate.call_args.args[0])
    assert sent['options'] == options and set(sent['credentials']) == set(runner.KEYS)
    url, body = deliver.call_args.args
    assert url == claim['callback_url'] and body['sealed'] == 'v1.n.t.body' and body['run_id'] == '77'
    assert body['receipt']['success'] is True and 'fixture-password' not in json.dumps(body)
    receipt = json.loads((tmp_path / 'encrypted-query' / 'receipt.json').read_text())
    assert receipt['callback_delivered'] is delivered
    assert (tmp_path / 'encrypted-query' / 'result.sealed').exists() is (not delivered)
    assert 'webhook-waiting' not in json.dumps(receipt)


def test_deliver_sends_a_named_agent_and_retries(monkeypatch):
    opener = Mock()
    opener.open.side_effect = [OSError('down'), Mock(__enter__=Mock(return_value=Mock(status=200)),
                                                     __exit__=Mock(return_value=False))]
    monkeypatch.setattr(runner, 'build_opener', Mock(return_value=opener))
    monkeypatch.setattr(runner.time, 'sleep', Mock())
    assert runner.deliver('https://n8n.example.test/webhook-waiting/1', {'a': 1}) is True
    request = opener.open.call_args.args[0]
    assert request.get_header('User-agent').startswith('captcha-solver-actions/')
    assert opener.open.call_count == 2


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
