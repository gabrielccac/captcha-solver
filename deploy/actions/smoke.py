"""One headless WebKit login and authenticated HTTP verification; safe receipt only."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

from playwright.sync_api import sync_playwright
import requests
from auth.govbr import AuthError
from inss import http_login
from inss.session import token_claims
from check_fonts import verify


def counters():
    root = Path('/sys/fs/cgroup')
    if (root / 'memory.current').exists():
        cpu = dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())
        return int(cpu['usage_usec']) / 1e6, int((root / 'memory.peak').read_text())
    return (int((root / 'cpuacct/cpuacct.usage').read_text()) / 1e9,
            int((root / 'memory/memory.max_usage_in_bytes').read_text()))


def main(*, on_authenticated=None, credentials=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--offline-preflight', action='store_true')
    args = parser.parse_args()
    receipt = {}
    result = dict(success=False, browser='WebKit headless', uses_xvfb=False,
                  transport='Playwright persistent context; not CDP', token_exported=False)
    active = {}
    started = time.monotonic()
    cpu_start, _ = counters()
    values = {}

    class Manager:
        def __init__(self, profile, **kwargs): self.profile = profile
        def start(self, **kwargs): active['profile'] = self.profile; return 'webkit-not-cdp'
        def stop(self): pass

    class Endpoint:
        def __init__(self, real): self.real = real
        def connect_over_cdp(self, endpoint):
            context = self.real.webkit.launch_persistent_context(active['profile'], headless=True,
                viewport={'width':1279,'height':812}, timeout=40000)
            result['browser_version'] = context.browser.version
            assert result['browser_version'] == '27.2', 'unexpected_webkit_version'
            return SimpleNamespace(contexts=[context], version=context.browser.version, close=context.close)

    class Adapter:
        def __enter__(self):
            self.manager = sync_playwright()
            self.real = self.manager.__enter__()
            return SimpleNamespace(chromium=Endpoint(self.real))
        def __exit__(self, *error): return self.manager.__exit__(*error)

    try:
        result['font_hash'] = verify()
        if args.offline_preflight:
            with sync_playwright() as pw, tempfile.TemporaryDirectory(prefix='webkit-probe-') as profile:
                context = pw.webkit.launch_persistent_context(profile,headless=True,viewport={'width':1279,'height':812})
                try:
                    assert context.browser.version == '27.2'
                    page = context.pages[0]
                    page.set_content('<button onclick="window.clicked=event.isTrusted">Probe</button>')
                    page.locator('button').click()
                    assert page.evaluate('()=>window.clicked===true')
                    assert page.screenshot()[:8] == b'\x89PNG\r\n\x1a\n'
                finally:
                    context.close()
            result.update(success=True,code='offline_preflight_passed')
        else:
            values = json.loads(sys.stdin.readline()) if credentials is None else dict(credentials)
            assert set(values) == {'GOVBR_CPF','GOVBR_PASSWORD','OPENROUTER_API_KEY'}
            cpf = ''.join(c for c in values['GOVBR_CPF'] if c in '0123456789')
            http_login.ManagedBrowser = Manager
            http_login.sync_playwright = Adapter
            with requests.Session() as client:
                http_login.login(client,cpf,values['GOVBR_PASSWORD'],values['OPENROUTER_API_KEY'],
                    receipt=receipt,optimize_interactions=True,batch_captcha=True,aria_selection=True)
                with client.get(http_login.API+f'usuarioservices/info/{cpf}',params={'refreshPedidos':'false'},
                    timeout=30,allow_redirects=False,stream=True) as response:
                    result['independent_api_http_status'] = response.status_code
                assert result['independent_api_http_status'] == 200 and receipt['api_session_verified']
                assert receipt['http_password_posts'] == 1 and receipt['browser_password_posts'] == 0
                assert receipt['token_captured_before_browser_send']
                result['access_token_present'] = bool(client.headers.get('Authorization','').startswith('Bearer '))
                result['mi_token_present'] = bool(client.headers.get('miToken'))
                claims = token_claims(client.headers['Authorization'].removeprefix('Bearer '))
                result['access_token_remaining_seconds'] = round(claims['exp']-time.time())
                assert result['access_token_remaining_seconds'] > 0
                if on_authenticated is not None:
                    on_authenticated(client, cpf)
                result.update(success=True,code='ok')
    except AuthError as error:
        result.update(code=error.code,error_type=type(error).__name__)
    except Exception as error:
        result.update(code='smoke_runtime_error',error_type=type(error).__name__)
    result.update(elapsed_ms=round((time.monotonic()-started)*1000),
        cpu_seconds=round(counters()[0]-cpu_start,3),memory_peak_mib=round(counters()[1]/1048576,1),
        receipt=receipt)
    # All receipt fields are protocol status/counters/timing; no bodies/URLs/headers.
    rendered = json.dumps(result)
    assert not any(value and value in rendered for value in values.values())
    values.clear()
    return result


if __name__ == '__main__':
    # Never forward provider/browser exception messages or page content to CI logs.
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = main()
    except BaseException as error:
        result = {'success':False,'code':'worker_terminated','error_type':type(error).__name__}
    print(json.dumps(result),flush=True)
    raise SystemExit(0 if result['success'] else 3)
