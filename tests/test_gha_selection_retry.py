"""Exercise the Actions retry without a browser or real credentials."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setitem(sys.modules, 'smoke', SimpleNamespace())
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    file = Path(__file__).parents[1] / 'experiments/gha-webkit/query_worker.py'
    spec = importlib.util.spec_from_file_location('gha_query_worker', file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


POSTS = {'http_captcha_posts': 0, 'http_password_posts': 0, 'browser_password_posts': 0}


@pytest.mark.parametrize('final_code', ['ok', 'captcha_selection_not_confirmed'])
def test_retry_only_once_before_handoff(worker, final_code):
    results = iter([
        {'success': False, 'code': 'captcha_selection_not_confirmed', 'elapsed_ms': 1500,
         'receipt': {**POSTS, 'elapsed_ms': 1200, 'selection_waits': [{'failure_phase': 'marks'}]}},
        {'success': final_code == 'ok', 'code': final_code, 'elapsed_ms': 2300},
    ])
    lines = []
    before = sys.stdin

    def run():
        lines.append(sys.stdin.readline())
        return next(results)

    final = worker.login_with_selection_retry(run, 'fictitious\n')
    assert sys.stdin is before
    assert lines == ['fictitious\n', 'fictitious\n']
    assert final['code'] == final_code
    assert final['elapsed_ms'] == 3800
    assert final['captcha_selection_retry']['first_failure_phase'] == 'marks'
    assert final['captcha_selection_retry']['recovered'] == (final_code == 'ok')


@pytest.mark.parametrize('code,receipt', [
    ('ok', POSTS), ('invalid_credentials', POSTS), ('captcha_http_not_accepted', POSTS),
    ('captcha_selection_not_confirmed', {}),
    ('captcha_selection_not_confirmed', {**POSTS, 'http_captcha_posts': 1}),
    ('captcha_selection_not_confirmed', {**POSTS, 'http_password_posts': 1}),
    ('captcha_selection_not_confirmed', {**POSTS, 'browser_password_posts': 1}),
])
def test_leaves_other_outcomes_alone(worker, code, receipt):
    outcomes = iter([{'success': code == 'ok', 'code': code, 'receipt': receipt}])
    assert worker.login_with_selection_retry(lambda: next(outcomes), 'fictitious')['code'] == code
