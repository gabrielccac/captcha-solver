"""Real WebKit, two distinct fixture origins, no remote requests or credentials."""
import os
from tempfile import TemporaryDirectory

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from captcha.aria import AriaInteractions


pytestmark = pytest.mark.skipif(os.environ.get('CAPTCHA_BROWSER_TESTS') != '1',
    reason='Explicitly enable inside the pinned WebKit image with CAPTCHA_BROWSER_TESTS=1')


def test_cross_origin_frame_recovers_only_the_lost_toggle():
    widget = '''<h2 class="prompt-text">Fixture task</h2>
      <div class="task-grid" style="position:relative;width:380px;height:380px"></div>
      <script>
      window.events = [];
      for (let i = 1; i <= 9; i++) {
        const tile = document.createElement('div');
        tile.className = 'task';
        tile.setAttribute('role', 'button');
        tile.setAttribute('aria-label', 'Challenge Image ' + i);
        tile.setAttribute('aria-pressed', 'false');
        tile.style.cssText = `position:absolute;width:120px;height:120px;
          left:${((i-1)%3)*130}px;top:${Math.floor((i-1)/3)*130}px;background:#ddd`;
        tile.innerHTML = '<div class="image" style="width:100%;height:100%;background-image:linear-gradient(red,blue)"></div>';
        tile.addEventListener('click', event => {
          window.events.push({index:i, trusted:event.isTrusted});
          if (i === 6 && window.events.filter(e => e.index === 6).length === 1) return;
          tile.setAttribute('aria-pressed', tile.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
        });
        document.querySelector('.task-grid').append(tile);
      }
      </script>'''
    with sync_playwright() as pw, TemporaryDirectory() as profile:
        context = pw.webkit.launch_persistent_context(profile, headless=True,
            viewport={'width': 1279, 'height': 812})
        try:
            assert context.browser.version == '27.2'
            def route(request):
                body = widget if request.request.url == 'https://widget.test/challenge' else (
                    '<iframe src="https://widget.test/challenge" style="margin:35px;width:420px;height:620px"></iframe>')
                request.fulfill(status=200, content_type='text/html', body=body)
            context.route('**/*', route)
            page = context.pages[0]
            page.goto('https://parent.test/')
            frame = page.frame(url='https://widget.test/challenge')
            frame.locator('.task').nth(8).wait_for()
            assert page.evaluate("""() => {
              try { document.querySelector('iframe').contentWindow.document; return false; }
              catch (e) { return e.name === 'SecurityError'; }
            }""")
            controller = AriaInteractions(short_clicks=True, timeout_ms=500)
            assert controller.begin_round(page)
            controller.prepare(Image.new('RGB', (1, 1)), (0, 0, 1, 1), [2, 6, 8])
            for index in (2, 6, 8):
                assert controller.click_tile(page, index, -1, -1)
            assert controller.wait_ready(page, {})
            assert controller.metrics[-1]['retried_selection'] == [6]
            events = frame.evaluate('window.events')
            assert [e['index'] for e in events] == [2, 6, 8, 6]
            assert all(e['trusted'] for e in events)
            assert controller.metrics[-1]['observed_selection'] == [2, 6, 8]
            # A new task must invalidate the old model answer before another click.
            frame.locator('.prompt-text').evaluate("el => el.textContent = 'Next task'")
            assert not controller.click_tile(page, 1, 0, 0)
            assert controller._failure == 'aria_challenge_changed'
            assert len(frame.evaluate('window.events')) == 4
        finally:
            context.close()
