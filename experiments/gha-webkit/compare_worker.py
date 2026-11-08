"""A/B image acquisition benchmark; original browser, model and selection guard."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, '/app/experiments/gha-webkit')
import smoke
from captcha import hcaptcha, prefetch, solver
from playwright.sync_api import Page
import requests


def main():
    mode = os.environ.pop('COMPARE_MODE')
    assert mode in ('screenshot', 'network')
    metrics = dict(mode=mode, models=[], screenshots=[], mappings=[])
    original_login = smoke.http_login.login
    original_model = solver.solve
    original_shot = Page.screenshot
    original_match = prefetch.match_tiles
    original_send = requests.Session.send

    def login(*args, **kwargs):
        kwargs['batch_captcha'] = mode == 'network'
        return original_login(*args, **kwargs)

    def model(header, tiles, *args, **kwargs):
        record = dict(image_count=len(tiles), batch=bool(kwargs.get('batch')))
        metrics['models'].append(record)
        started = time.perf_counter()
        try:
            picked = original_model(header, tiles, *args, **kwargs)
            record['selected'] = picked
            return picked
        finally:
            record['elapsed_ms'] = round((time.perf_counter()-started)*1000, 2)
            usage = kwargs.get('usage') or {}
            record['usage'] = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'cost') if k in usage}

    def send(self, request, **kwargs):
        if urlsplit(request.url).hostname == 'openrouter.ai' and metrics['models']:
            metrics['models'][-1]['payload_bytes'] = len(request.body or b'')
        return original_send(self, request, **kwargs)

    def screenshot(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return original_shot(self, *args, **kwargs)
        finally:
            metrics['screenshots'].append(round((time.perf_counter()-started)*1000, 2))

    def match(tiles, candidates):
        candidates = list(candidates)
        started = time.perf_counter()
        result = original_match(tiles, candidates)
        metrics['mappings'].append(dict(candidate_count=len(candidates), matched=result is not None,
            elapsed_ms=round((time.perf_counter()-started)*1000, 3)))
        return result

    smoke.http_login.login = login
    solver.solve = hcaptcha.solve = model
    requests.Session.send = send
    Page.screenshot = screenshot
    prefetch.match_tiles = match
    result = smoke.main()
    return {'validation': result, 'comparison': metrics, 'client_data': {}}


if __name__ == '__main__':
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = main()
    except BaseException as error:
        result = {'validation': {'success': False, 'code': 'comparison_worker_failed',
            'error_type': type(error).__name__}, 'client_data': {}}
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result['validation']['success'] else 3)
