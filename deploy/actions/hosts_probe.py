"""List which hosts a page pulls from, and how much, without any credentials."""
import collections
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright

sys.path.insert(0, '/app/src')
from captcha.browser import ManagedBrowser  # noqa: E402

target = sys.argv[1] if len(sys.argv) > 1 else 'https://meu.inss.gov.br/'
blocked = os.environ.get('INSS_ALLOW_HOSTS', '').strip()
allow = tuple(part.strip() for part in blocked.split(',') if part.strip())
counts = collections.Counter()
bytes_by_host = collections.Counter()
blocked_hosts = collections.Counter()

with tempfile.TemporaryDirectory(prefix='hosts-', ignore_cleanup_errors=True) as profile:
    manager = ManagedBrowser(profile, quiet=True)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(manager.start(wait_ready=True))
            page = browser.contexts[0].pages[0]

            def on_request(request):
                from urllib.parse import urlsplit
                host = urlsplit(request.url).hostname or '?'
                counts[host] += 1
                if allow and not host.endswith(allow):
                    blocked_hosts[host] += 1
                    request.abort()
                else:
                    request.continue_()

            def on_response(response):
                from urllib.parse import urlsplit
                host = urlsplit(response.url).hostname or '?'
                try:
                    size = int(response.headers.get('content-length') or 0)
                except ValueError:
                    size = 0
                bytes_by_host[host] += size

            page.route('**/*', on_request)
            page.on('response', on_response)
            page.goto(target, wait_until='load', timeout=45000)
            page.wait_for_timeout(8000)
        finally:
            manager.stop()

print('alvo', target, 'allowlist', allow or 'nenhuma')
print('total requisicoes', sum(counts.values()), 'hosts', len(counts))
for host, value in counts.most_common(12):
    print(f'  {value:4d} reqs {bytes_by_host[host]/1048576:6.2f} MB  {host}')
print('kb declarados', round(sum(bytes_by_host.values()) / 1024), 'de', len(bytes_by_host), 'respostas')
print('bloqueados', dict(blocked_hosts.most_common(8)))
