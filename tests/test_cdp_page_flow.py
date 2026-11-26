"""Realistic reproductions of the GOV.BR login steps the client must survive.

Each test is named after the regression it locks: a client that resolves only
once, that ignores OOPIF frames, that trusts a stale node, or that reports
"element not found" where Playwright reports a state, fails these even though
the isolated API tests still pass.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import urllib.request

import pytest
from PIL import Image

from captcha import cdp_page

PORT = object()  # placeholder replaced per request


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class Site:
    """Routes served on 127.0.0.1, also reachable as localhost (two sites)."""

    def __init__(self):
        self.routes = {}
        self.hits = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802 - http.server API
                owner.dispatch(self, "GET")

            def do_POST(self):  # noqa: N802 - http.server API
                owner.dispatch(self, "POST")

        class Server(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):  # noqa: D102 - aborts are expected
                pass

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self._server.server_port

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def other_origin(self):
        return f"http://localhost:{self.port}"

    def page(self, path, body, content_type="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.replace("{PORT}", str(self.port)).encode("utf-8")
        self.routes[path] = (body, content_type)

    def redirect(self, path, location, status=302):
        self.routes[path] = ("redirect", location, status)

    def slow(self, path, seconds, body, content_type="text/html; charset=utf-8"):
        self.routes[path] = ("slow", seconds, body, content_type)

    def dispatch(self, handler, method):
        parts = urlsplit(handler.path)
        self.hits.append((method, parts.path, parts.query))
        route = self.routes.get(parts.path)
        if route is None:
            body, status, content_type = b"missing", 404, "text/plain"
        elif route[0] == "slow":
            _, seconds, body, content_type = route
            time.sleep(seconds)
            status = 200
        elif route[0] == "redirect":
            _, location, status = route
            handler.send_response(status)
            handler.send_header("Location", location)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        else:
            body, content_type = route
            status = 200
        if method == "POST":
            length = int(handler.headers.get("Content-Length") or 0)
            handler.rfile.read(length)
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def seen(self, path, wait=5.0):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if any(hit[1] == path for hit in self.hits):
                return True
            time.sleep(0.05)
        return False

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _until(predicate, timeout=5.0, message="condition never became true"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail(message)


def _chromium_executable():
    override = os.environ.get("CAPY_CDP_CHROMIUM")
    if override:
        return Path(override)
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        root = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        root = Path.home() / ".cache" / "ms-playwright"
    candidates = []
    for pattern in (
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    ):
        candidates += sorted(root.glob(pattern))
    if not candidates:
        pytest.skip(f"no Playwright Chromium under {root}")
    return candidates[-1]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def launch_chrome(*extra):
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="cdp-flow-test-")
    arguments = [
        str(_chromium_executable()),
        "--disable-gpu",
        "--disable-background-networking",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--window-size=1280,900",
        *extra,
    ]
    if not os.environ.get("CAPY_CDP_HEADED"):
        arguments.insert(1, "--headless=new")
    if not extra:
        arguments.append("about:blank")
    process = subprocess.Popen(
        arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("chromium exited during startup")
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1) as response:
                response.read()
            return process, endpoint
        except OSError:
            time.sleep(0.1)
    process.kill()
    pytest.fail("chromium did not expose CDP in time")


@pytest.fixture(scope="module")
def chrome():
    process, endpoint = launch_chrome()
    try:
        yield endpoint
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        with contextlib.suppress(OSError):
            process.kill()


@pytest.fixture(scope="module")
def browser(chrome):
    instance = cdp_page.connect(chrome)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def page(browser):
    handle = browser.page()
    handle.goto("about:blank", wait_until="load")
    return handle


@pytest.fixture()
def site():
    instance = Site()
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

CPF_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>gov.br</title></head>
<body>
<h1>Identifique-se no gov.br</h1>
<label>CPF <input id="accountId" name="accountId" autocomplete="off"></label>
<button id="next" type="button" disabled>Continuar</button>
<div id="result"></div>
<script>
const field = document.getElementById('accountId');
const button = document.getElementById('next');
let pending = null;
function formatted(value) {
  const digits = value.replace(/\\D/g, '').slice(0, 11);
  let out = digits;
  if (digits.length > 9) out = digits.slice(0, 3) + '.' + digits.slice(3, 6) + '.' + digits.slice(6, 9) + '-' + digits.slice(9);
  else if (digits.length > 6) out = digits.slice(0, 3) + '.' + digits.slice(3, 6) + '.' + digits.slice(6);
  else if (digits.length > 3) out = digits.slice(0, 3) + '.' + digits.slice(3);
  return out;
}
field.addEventListener('input', () => {
  field.value = formatted(field.value);
  clearTimeout(pending);
  if (field.value.replace(/\\D/g, '').length === 11) {
    pending = setTimeout(() => { button.disabled = false; }, 800);
  }
});
button.addEventListener('click', () => {
  document.getElementById('result').textContent = field.value;
});
</script></body></html>"""

CPF_ARIA_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>gov.br</title></head>
<body>
<label>CPF <input id="accountId" name="accountId"></label>
<button id="next" type="button" aria-disabled="true">Continuar</button>
<div id="result"></div>
<script>
const field = document.getElementById('accountId');
const button = document.getElementById('next');
let pending = null;
window.__early = false;
window.__enabled_at = null;
field.addEventListener('input', () => {
  clearTimeout(pending);
  if (field.value.replace(/\\D/g, '').length === 11) {
    pending = setTimeout(() => {
      button.setAttribute('aria-disabled', 'false');
      window.__enabled_at = performance.now();
    }, 800);
  }
});
button.addEventListener('click', () => {
  if (button.getAttribute('aria-disabled') === 'true') { window.__early = true; return; }
  document.getElementById('result').textContent = field.value;
});
</script></body></html>"""


def submit_cpf_default(page, cpf_masked):
    """The default (non short_pauses) sequence from inss.session.submit_cpf."""
    selector = 'input[name="accountId"], input#accountId'
    loc = page.locator(selector).first
    box = loc.bounding_box(timeout=15000)
    page.mouse.move(box["x"] + box["width"] / 2 + 3, box["y"] + box["height"] / 2 + 1, steps=25)
    page.wait_for_timeout(150)
    loc.click(timeout=10000)
    page.wait_for_timeout(200)
    for char in cpf_masked:
        page.keyboard.type(char, delay=40)
    page.wait_for_timeout(900)
    btn = page.locator('button:has-text("Continuar")').first
    frame_box = btn.bounding_box(timeout=10000)
    page.mouse.move(
        frame_box["x"] + frame_box["width"] / 2, frame_box["y"] + frame_box["height"] / 2, steps=30
    )
    page.wait_for_timeout(300)
    page.mouse.click(frame_box["x"] + frame_box["width"] / 2, frame_box["y"] + frame_box["height"] / 2)


def submit_cpf_short_pauses(page, cpf_masked):
    """The short_pauses branch: wait visible, poll is_enabled, then click."""
    selector = 'input[name="accountId"], input#accountId'
    loc = page.locator(selector).first
    box = loc.bounding_box(timeout=15000)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
    loc.click(timeout=10000)
    page.wait_for_timeout(100)
    for char in cpf_masked:
        page.keyboard.type(char, delay=30)
    entered = page.locator(selector).first.input_value()
    assert entered == cpf_masked
    btn = page.locator('button:has-text("Continuar")').first
    btn.wait_for(state="visible", timeout=10000)
    deadline = time.monotonic() + 10
    while not btn.is_enabled():
        if time.monotonic() >= deadline:
            raise RuntimeError("cpf_submit_not_ready")
        page.wait_for_timeout(50)
    frame_box = btn.bounding_box(timeout=10000)
    page.mouse.move(
        frame_box["x"] + frame_box["width"] / 2, frame_box["y"] + frame_box["height"] / 2, steps=30
    )
    page.mouse.click(frame_box["x"] + frame_box["width"] / 2, frame_box["y"] + frame_box["height"] / 2)


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_submit_cpf_sequence_submits_masked_cpf_and_survives_a_delayed_enable(page, site):
    """Locks: bounding_box/click on a button that is disabled for 800 ms."""
    site.page("/cpf", CPF_PAGE)
    page.goto(site.url + "/cpf", wait_until="domcontentloaded")
    submit_cpf_default(page, "123.456.789-00")
    deadline = time.monotonic() + 5
    while page.locator("#result").inner_text() != "123.456.789-00":
        if time.monotonic() > deadline:
            pytest.fail("the submit click never reached the button handler")
        time.sleep(0.05)


def test_short_pauses_waits_for_aria_disabled_button_before_clicking(page, site):
    """Locks: is_enabled must honour aria-disabled, not just the disabled attribute."""
    site.page("/cpf", CPF_ARIA_PAGE)
    page.goto(site.url + "/cpf", wait_until="domcontentloaded")
    submit_cpf_short_pauses(page, "123.456.789-00")
    assert page.evaluate("window.__early") is False
    assert page.locator("#result").inner_text() == "123.456.789-00"
    assert page.evaluate("window.__enabled_at") is not None


def test_fieldset_disabled_reads_as_disabled(page, site):
    """Locks: Playwright's enablement rules (fieldset[disabled], stale attribute)."""
    site.page(
        "/states",
        """<body><fieldset disabled><button id="inside">Continuar</button></fieldset>
        <button id="free">Continuar</button>
        <button id="aria" aria-disabled="true">Continuar</button></body>""",
    )
    page.goto(site.url + "/states", wait_until="load")
    assert page.locator("#inside").is_enabled() is False
    assert page.locator("#free").is_enabled() is True
    assert page.locator("#aria").is_enabled() is False
    assert page.locator("#nope").is_enabled() is False


def test_wait_for_selector_survives_a_document_swap(page, site):
    """Locks: a wait must retry through a navigation, not raise on a dead context."""
    site.page(
        "/swap",
        """<body><div id="stage">loading</div>
        <script>setTimeout(() => { location.href = '/after'; }, 250);</script></body>""",
    )
    site.page("/after", """<body><div id="only-on-next" class="br-message">done</div></body>""")
    page.goto(site.url + "/swap", wait_until="domcontentloaded")
    handle = page.wait_for_selector("#only-on-next", timeout=15000)
    assert handle is not None
    assert page.url.endswith("/after")
    assert page.locator("#only-on-next").inner_text() == "done"


def test_locator_first_inside_cross_origin_iframe_that_mounts_late(page, site):
    """Locks: locator.first must stay inside the frame, including OOPIF sessions."""
    site.page(
        "/oopif",
        """<body style="margin:0"><div style="height:120px"></div><div id="host"></div>
        <script>setTimeout(() => {
          const frame = document.createElement('iframe');
          frame.id = 'captcha';
          frame.src = 'http://localhost:{PORT}/frame';
          frame.style = 'width:320px;height:240px';
          document.getElementById('host').appendChild(frame);
        }, 400);</script></body>""",
    )
    site.page(
        "/frame",
        """<body style="margin:8px"><div class="task-grid"
          style="width:120px;height:80px;background:#0af">tile</div>
        <script>document.querySelector('.task-grid')
          .addEventListener('click', () => fetch('/tile-clicked'));</script></body>""",
    )
    page.goto(site.url + "/oopif", wait_until="load")
    # A loaded suite starves the reader thread, so the OOPIF can take a while
    # to appear; the wait stays bounded but generous.
    deadline = time.monotonic() + 30
    frame = None
    while frame is None:
        frame = next((f for f in page.frames if "localhost" in f.url), None)
        if frame is None:
            if time.monotonic() > deadline:
                pytest.fail("cross-origin iframe frame was never registered")
            time.sleep(0.05)

    assert page.locator(".task-grid").count() == 0
    locator = frame.locator(".task-grid").first
    locator.wait_for(state="visible", timeout=15000)
    assert frame.locator(".task-grid").count() == 1
    box = locator.bounding_box(timeout=10000)
    frame_rect = page.evaluate(
        "() => { const el = document.getElementById('captcha');"
        " const r = el.getBoundingClientRect();"
        " return {x: r.x, y: r.y, border: parseFloat(getComputedStyle(el).borderLeftWidth)}; }"
    )
    assert box["width"] == 120
    assert box["x"] == pytest.approx(frame_rect["x"] + frame_rect["border"] + 8, abs=1)
    assert box["y"] == pytest.approx(frame_rect["y"] + frame_rect["border"] + 8, abs=1)
    locator.click(timeout=10000)
    assert site.seen("/tile-clicked"), "the click never reached the tile inside the iframe"


def test_wait_for_visible_ignores_hidden_then_shown_element(page, site):
    """Locks: wait_for(state='visible') waits instead of failing on a hidden node."""
    site.page(
        "/hidden",
        """<body><div id="late" style="visibility:hidden">pronto</div>
        <script>setTimeout(() => { document.getElementById('late').style.visibility = 'visible'; },
        600);</script></body>""",
    )
    page.goto(site.url + "/hidden", wait_until="load")
    started = time.monotonic()
    page.locator("#late").wait_for(state="visible", timeout=10000)
    assert time.monotonic() - started >= 0.5
    assert page.locator("#late").is_visible() is True


def test_goto_follows_a_redirect_chain_to_the_final_document(page, site):
    """Locks: goto must wait for the last document of a redirect chain."""
    site.redirect("/r1", "/r2")
    site.redirect("/r2", "/final")
    site.page("/final", "<body><title>final</title><div id='ok'>final</div></body>")
    page.goto(site.url + "/r1", wait_until="domcontentloaded", timeout=20000)
    assert page.url == site.url + "/final"
    assert page.evaluate("document.title") == "final"
    assert page.locator("#ok").inner_text() == "final"


def test_goto_reports_an_uncommitted_navigation_with_the_url(page, site):
    """Locks: a navigation that never commits names the URL and the timeout."""
    site.slow("/slow", 2.0, b"late")
    started = time.monotonic()
    with pytest.raises(cdp_page.TimeoutError) as failure:
        page.goto(site.url + "/slow", wait_until="load", timeout=700)
    assert "/slow" in str(failure.value)
    assert time.monotonic() - started < 10
    page.wait_for_timeout(1000)
    page.goto(site.url + "/slow", wait_until="load", timeout=20000)
    assert page.locator("body").inner_text() == "late"


def test_connect_without_a_page_target_opens_one():
    """Locks: connecting before Chrome publishes a tab must not break pages[0]."""
    process, endpoint = launch_chrome("--no-startup-window")
    browser = cdp_page.connect(endpoint)
    try:
        assert browser.contexts[0].pages == []
        handle = browser.page()
        handle.goto("about:blank", wait_until="load")
        assert handle.evaluate("() => 1 + 1") == 2
        assert browser.contexts[0].pages
    finally:
        browser.close()
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


# --------------------------------------------------------------------------
# the CPF step: a wait that starts during a re-render must keep polling
# --------------------------------------------------------------------------

CPF_RERENDER = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<h1>Identifique-se no gov.br</h1>
<form id="loginData" method="post" action="/handoff">
  <label>CPF <input id="accountId" name="accountId" type="tel" autocomplete="off"></label>
  <div id="slot"><button id="next" type="submit" disabled>Continuar</button></div>
</form>
<script>
const field = document.getElementById('accountId');
function formatted(value) {
  const d = value.replace(/[^0-9]/g, '').slice(0, 11);
  if (d.length > 9) return d.slice(0,3)+'.'+d.slice(3,6)+'.'+d.slice(6,9)+'-'+d.slice(9);
  if (d.length > 6) return d.slice(0,3)+'.'+d.slice(3,6)+'.'+d.slice(6);
  if (d.length > 3) return d.slice(0,3)+'.'+d.slice(3);
  return d;
}
field.addEventListener('input', () => {
  field.value = formatted(field.value);
  if (field.value.replace(/[^0-9]/g, '').length === 11) {
    document.getElementById('slot').replaceChildren();
    setTimeout(() => {
      document.getElementById('slot').innerHTML =
        '<button id="next" type="submit" disabled>Continuar</button>';
      setTimeout(() => { const b = document.getElementById('next'); if (b) b.disabled = false; }, 400);
    }, 450);
  }
});
</script></body></html>"""


CPF_SWAP = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<form id="loginData" method="post" action="/handoff">
  <label>CPF <input id="accountId" name="accountId" type="tel" autocomplete="off"></label>
  <button id="next" type="submit" disabled>Continuar</button>
</form>
<script>
const field = document.getElementById('accountId');
function formatted(value) {
  const d = value.replace(/[^0-9]/g, '').slice(0, 11);
  if (d.length > 9) return d.slice(0,3)+'.'+d.slice(3,6)+'.'+d.slice(6,9)+'-'+d.slice(9);
  if (d.length > 6) return d.slice(0,3)+'.'+d.slice(3,6)+'.'+d.slice(6);
  if (d.length > 3) return d.slice(0,3)+'.'+d.slice(3);
  return d;
}
field.addEventListener('input', () => {
  field.value = formatted(field.value);
  if (field.value.replace(/[^0-9]/g, '').length === 11) {
    sessionStorage.setItem('cpf', field.value);
    if (!sessionStorage.getItem('swapped')) {
      setTimeout(() => location.replace('/cpf-again'), 120);
    }
  }
});
</script></body></html>"""


CPF_AFTER_SWAP = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<form id="loginData" method="post" action="/handoff">
  <label>CPF <input id="accountId" name="accountId" type="tel" autocomplete="off"></label>
  <button id="next" type="submit" disabled>Continuar</button>
</form>
<script>
sessionStorage.setItem('swapped', '1');
document.getElementById('accountId').value = sessionStorage.getItem('cpf') || '';
setTimeout(() => { document.getElementById('next').disabled = false; }, 500);
</script></body></html>"""


def intercept_post(page, captured):
    def handler(route):
        if route.request.method == "POST":
            captured["body"] = route.request.post_data or ""
            route.fulfill(status=200, content_type="text/html",
                          body="<main data-auth-handoff>ready</main>")
        else:
            route.continue_()

    page.route("**/handoff", handler)


def test_cpf_wait_survives_the_panel_rebuild_that_removes_the_button(page, site):
    """Locks: a visible-state wait must poll through a re-render, not decide once.

    This is the shape of the field CPF failure: the form is rebuilt right after
    the 11th digit, so the button is absent exactly when submit_cpf starts its
    ``wait_for(state='visible')``.  A single observation would raise
    TimeoutError immediately even though the button returns 450 ms later.
    """
    site.page("/cpf-rerender", CPF_RERENDER)
    captured = {}
    intercept_post(page, captured)
    try:
        page.goto(site.url + "/cpf-rerender", wait_until="domcontentloaded")
        started = time.monotonic()
        submit_cpf_short_pauses(page, "123.456.789-00")
        assert time.monotonic() - started < 8
        _until(lambda: captured.get("body"), 5, "the rebuilt button was never clicked")
        assert "12345678900" in captured["body"].replace(".", "").replace("-", "")
    finally:
        page.unroute()
        page.locator("body").wait_for(state="attached")


def test_cpf_wait_survives_a_document_swap_after_the_last_digit(page, site):
    """Locks: locator waits re-resolve across a replaced document, not a dead context."""
    site.page("/cpf-swap", CPF_SWAP)
    site.page("/cpf-again", CPF_AFTER_SWAP)
    captured = {}
    intercept_post(page, captured)
    try:
        page.goto(site.url + "/cpf-swap", wait_until="domcontentloaded")
        submit_cpf_short_pauses(page, "123.456.789-00")
        _until(lambda: captured.get("body"), 5, "no POST after the document swap")
        assert page.evaluate("sessionStorage.getItem('swapped')") == "1"
        assert "12345678900" in captured["body"].replace(".", "").replace("-", "")
    finally:
        page.unroute()
        page.locator("body").wait_for(state="attached")



def test_locator_click_waits_for_a_button_that_enables_late(page, site):
    """Locks: Playwright's locator.click waits for enablement; a click on a disabled
    node never reaches its handler and looks like a missing navigation downstream."""
    site.page(
        "/late-enable",
        """<body><button id="go" disabled>Continuar</button><div id="out"></div>
        <script>
        window.__clicks = 0; window.__clicked_disabled = null;
        const b = document.getElementById('go');
        b.addEventListener('click', () => {
          window.__clicks += 1; window.__clicked_disabled = b.disabled;
          document.getElementById('out').textContent = 'clicked';
        });
        setTimeout(() => { b.disabled = false; }, 600);
        </script></body>""",
    )
    page.goto(site.url + "/late-enable", wait_until="load")
    started = time.monotonic()
    page.locator("#go").click(timeout=5000)
    assert time.monotonic() - started >= 0.45
    assert page.evaluate("window.__clicks") == 1
    assert page.evaluate("window.__clicked_disabled") is False
    assert page.locator("#out").inner_text() == "clicked"


def test_wait_for_timeout_zero_means_no_deadline(page, site):
    """Locks: Playwright timeout=0 disables the deadline instead of expiring at once."""
    site.page(
        "/late-node",
        """<body><div id="host"></div>
        <script>setTimeout(() => {
          document.getElementById('host').innerHTML = '<span id="late">pronto</span>';
        }, 700);</script></body>""",
    )
    page.goto(site.url + "/late-node", wait_until="load")
    page.locator("#late").wait_for(state="visible", timeout=0)
    assert page.locator("#late").inner_text() == "pronto"


def test_frame_wait_does_not_overshoot_its_timeout_while_the_frame_loads(page, site):
    """Locks: a wait inside a frame shares its deadline with the frame-context
    lookup.  The old fixed 10 s lookup made a 600 ms wait take ten seconds."""
    site.page(
        "/frame-slow",
        """<body style="margin:0">
        <iframe id="slow" src="http://localhost:{PORT}/hang" style="width:300px;height:200px"></iframe>
        </body>""",
    )
    site.slow("/hang", 12.0, b"<body>late</body>")
    page.goto(site.url + "/frame-slow", wait_until="domcontentloaded")
    frame = _until(
        lambda: next((f for f in page.frames if f.parent_frame is not None), None),
        10,
        "the cross-origin frame was never registered",
    )
    started = time.monotonic()
    with pytest.raises(cdp_page.TimeoutError) as failure:
        frame.locator("#nope").wait_for(state="visible", timeout=600)
    elapsed = time.monotonic() - started
    assert elapsed < 4, f"a 600 ms wait took {elapsed:.2f}s while the frame loaded"
    assert "600ms" in str(failure.value)


def test_wait_for_selector_attached_returns_the_handle_for_a_hidden_node(page, site):
    """Locks: attached does not require visibility, and wait_for_selector hands
    back the handle it actually matched instead of resolving a second time."""
    site.page("/hidden-node", """<body><div id="h" style="display:none">pronto</div></body>""")
    page.goto(site.url + "/hidden-node", wait_until="load")
    handle = page.wait_for_selector("#h", state="attached", timeout=3000)
    assert handle is not None
    assert handle.is_visible() is False
    assert handle.inner_text() == "pronto"
    handle.release()


def test_wait_for_selector_hidden_returns_after_the_node_is_removed(page, site):
    """Locks: state='hidden' resolves on removal and returns None like Playwright."""
    site.page(
        "/removed-node",
        """<body><div id="gone">x</div>
        <script>setTimeout(() => document.getElementById('gone').remove(), 350);</script></body>""",
    )
    page.goto(site.url + "/removed-node", wait_until="load")
    started = time.monotonic()
    assert page.wait_for_selector("#gone", state="hidden", timeout=5000) is None
    assert time.monotonic() - started < 3
    page.locator("#gone").wait_for(state="detached", timeout=1000)


def test_wait_states_distinguish_attached_from_visible_and_name_the_timeout(page, site):
    """Locks: the timeout message names selector, state and URL; attached passes
    where visible legitimately cannot."""
    site.page("/state-matrix", """<body><div id="off" style="display:none">x</div></body>""")
    page.goto(site.url + "/state-matrix", wait_until="load")
    page.locator("#off").wait_for(state="attached", timeout=500)
    with pytest.raises(cdp_page.TimeoutError) as failure:
        page.locator("#off").wait_for(state="visible", timeout=400)
    message = str(failure.value)
    assert "#off" in message and "visible" in message and "400ms" in message
    assert "/state-matrix" in message


def test_screenshot_clip_after_scroll_matches_the_element_box(page, site):
    """Locks: clip stays viewport-relative after a scroll (same pixels as Playwright)."""
    site.page(
        "/scrolled",
        """<body style="margin:0"><div style="height:900px;background:#eee"></div>
        <div id="target" style="margin:20px;width:300px;height:200px;background:#0088ff"></div>
        <div style="height:1500px;background:#ddd"></div></body>""",
    )
    page.goto(site.url + "/scrolled", wait_until="load")
    page.evaluate("window.scrollTo(0, 500)")
    clip = page.evaluate(
        "() => { const r = document.getElementById('target').getBoundingClientRect();"
        " return {x: r.x, y: r.y, width: r.width, height: r.height}; }"
    )
    assert page.evaluate("window.scrollY") == 500
    data = page.screenshot(clip=clip)
    image = Image.open(io.BytesIO(data)).convert("RGB")
    assert image.size == (300, 200)
    assert image.getpixel((150, 100)) == (0, 136, 255)


def test_screenshot_of_a_noisy_page_is_large_and_decodes(page, site):
    """Locks: a 600 KB+ capture is returned whole, not truncated by the socket path."""
    site.page(
        "/noise",
        """<body style="margin:0"><canvas id="c" width="1200" height="800"></canvas>
        <script>
        const canvas = document.getElementById('c');
        const ctx = canvas.getContext('2d');
        const image = ctx.createImageData(1200, 800);
        let seed = 987654321;
        for (let i = 0; i < image.data.length; i += 4) {
          seed = (seed * 1103515245 + 12345) & 0x7fffffff;
          image.data[i] = seed & 0xff;
          image.data[i + 1] = (seed >> 8) & 0xff;
          image.data[i + 2] = (seed >> 16) & 0xff;
          image.data[i + 3] = 255;
        }
        ctx.putImageData(image, 0, 0);
        </script></body>""",
    )
    page.goto(site.url + "/noise", wait_until="load")
    viewport = page.evaluate("() => [window.innerWidth, window.innerHeight]")
    data = page.screenshot()
    assert len(data) >= 600_000, f"only {len(data)} bytes"
    image = Image.open(io.BytesIO(data))
    assert image.size[0] == viewport[0]
    assert image.size[1] >= 600


def test_context_destroyed_mid_evaluate_raises_error_and_page_recovers(page, site):
    """Locks: a navigation mid-evaluate surfaces as Error, not Timeout, and the
    page object stays usable afterwards."""
    site.page(
        "/eval-nav",
        """<body><script>setTimeout(() => { location.href = '/eval-after'; }, 250);</script></body>""",
    )
    site.page("/eval-after", """<body><div id="ok">done</div></body>""")
    page.goto(site.url + "/eval-nav", wait_until="domcontentloaded")
    with pytest.raises(cdp_page.Error) as failure:
        page.evaluate("() => new Promise((resolve) => setTimeout(() => resolve(1), 3000))")
    assert not isinstance(failure.value, cdp_page.TimeoutError)
    page.wait_for_selector("#ok", timeout=10000)
    assert page.evaluate("() => 6 * 7") == 42


def test_locator_wait_survives_a_same_document_rewrite(page, site):
    """Locks: document.open/write destroys the execution context under the wait."""
    site.page(
        "/doc-write",
        """<body><div id="stage">loading</div>
        <script>setTimeout(() => { document.open();
          document.write('<body><div id="only">done</div></body>');
          document.close(); }, 400);</script></body>""",
    )
    page.goto(site.url + "/doc-write", wait_until="domcontentloaded")
    page.locator("#only").wait_for(state="visible", timeout=8000)
    assert page.locator("#only").inner_text() == "done"


def test_a_protocol_error_leaves_the_session_usable(page):
    """Locks: one failed CDP command is not a dead connection."""
    with pytest.raises(cdp_page.Error):
        page._send("Definitely.notAMethod")
    assert page.evaluate("() => 1 + 1") == 2
    assert page.locator("body").count() == 1
    assert page.screenshot()[:8] == b"\x89PNG\r\n\x1a\n"


def test_closing_browser_with_a_command_in_flight_raises_instead_of_returning_none():
    """Locks: pending commands fail with a closed-connection error, never a null result."""
    process, endpoint = launch_chrome()
    browser = cdp_page.connect(endpoint)
    outcome = {}

    def run():
        try:
            page = browser.page()
            outcome["value"] = page.evaluate("() => new Promise((r) => setTimeout(r, 5000))")
        except Exception as error:  # noqa: BLE001 - the error is the assertion
            outcome["error"] = error

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.8)
    browser.close()
    worker.join(15)
    process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)
    assert "value" not in outcome
    assert isinstance(outcome.get("error"), cdp_page.Error)
    assert "closed" in str(outcome["error"])


def test_connect_returns_the_tab_that_appears_after_the_endpoint():
    """Locks: the connect race when Chrome has no page target yet."""
    process, endpoint = launch_chrome("--no-startup-window")
    browser = cdp_page.connect(endpoint)
    try:
        assert browser.contexts[0].pages == []

        def open_tab():
            time.sleep(0.4)
            browser._connection.send("Target.createTarget", {"url": "about:blank"})

        threading.Thread(target=open_tab, daemon=True).start()
        pages = browser.contexts[0].pages
        assert pages, "pages did not wait for the tab Chrome was still creating"
        pages[0].goto("about:blank", wait_until="load")
        assert pages[0].evaluate("() => 21 * 2") == 42
    finally:
        browser.close()
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


def test_cross_origin_iframe_that_mounts_late_then_navigates(page, site):
    """Locks: OOPIF session tracking through its own document replacement."""
    site.page(
        "/stage-outer",
        """<body style="margin:0"><div style="height:40px"></div>
        <script>setTimeout(() => {
          const frame = document.createElement('iframe');
          frame.id = 'tile-frame';
          frame.src = 'http://localhost:{PORT}/stage-inner';
          frame.style = 'width:360px;height:240px;border:0';
          document.body.appendChild(frame);
        }, 250);</script></body>""",
    )
    site.page(
        "/stage-inner",
        """<body><script>setTimeout(() => {
          location.href = 'http://localhost:{PORT}/stage-final'; }, 300);</script></body>""",
    )
    site.page(
        "/stage-final",
        """<body style="margin:8px"><div id="tile"
          style="width:120px;height:80px;background:#0af">tile</div>
        <script>document.getElementById('tile').addEventListener('click',
          () => fetch('/tile-b', {mode: 'no-cors'}));</script></body>""",
    )
    page.goto(site.url + "/stage-outer", wait_until="load")
    frame = _until(
        lambda: next((f for f in page.frames if "stage-inner" in f.url or "stage-final" in f.url), None),
        15,
        "the cross-origin frame was never registered",
    )
    locator = frame.locator("#tile")
    locator.wait_for(state="visible", timeout=15000)
    box = locator.bounding_box(timeout=5000)
    frame_rect = page.evaluate(
        "() => { const el = document.getElementById('tile-frame');"
        " const r = el.getBoundingClientRect(); return {x: r.x, y: r.y}; }"
    )
    assert box["x"] == pytest.approx(frame_rect["x"] + 8, abs=2)
    assert box["y"] == pytest.approx(frame_rect["y"] + 8, abs=2)
    locator.click(timeout=5000)
    assert site.seen("/tile-b"), "the click never reached the tile inside the OOPIF"


def test_fetch_fulfilled_post_navigation_leaves_the_tab_usable(page, site):
    """Locks: a POST navigation served by Fetch.fulfillRequest commits and the
    session survives the document replacement."""
    site.page(
        "/post-form",
        """<body><form method="post" action="/handoff"><input name="token" value="abc">
        <button id="send" type="submit">Continuar</button></form></body>""",
    )
    captured = {}
    intercept_post(page, captured)
    try:
        page.goto(site.url + "/post-form", wait_until="load")
        page.evaluate("document.forms[0].submit()")
        assert page.wait_for_selector("[data-auth-handoff]", timeout=10000) is not None
        assert page.url.endswith("/handoff")
        assert "token=abc" in captured["body"]
        assert page.evaluate("() => document.body.innerText.trim()") == "ready"
    finally:
        page.unroute()


def test_role_and_text_locators_survive_a_re_render(page, site):
    """Locks: locator resolution is live, not pinned to the first matching node."""
    site.page(
        "/rerender",
        """<body><div id="host"><span>old</span></div>
        <script>setTimeout(() => {
          document.getElementById('host').innerHTML =
            '<button>Continuar</button><p>Servi\u00e7o: Meu INSS - Site</p>';
        }, 300);</script></body>""",
    )
    page.goto(site.url + "/rerender", wait_until="load")
    page.get_by_role("button", name="Continuar", exact=True).click(timeout=5000)
    assert page.get_by_text("Servi\u00e7o: Meu INSS - Site", exact=True).is_visible() is True


def test_nth_and_last_index_stay_inside_an_iframe(page, site):
    """Locks: first/last/nth keep the frame owner and pick the right node."""
    site.page(
        "/items-outer",
        """<body style="margin:0"><iframe src="/items" style="width:300px;height:160px;border:0"></iframe></body>""",
    )
    site.page(
        "/items",
        """<body><ul><li class="item">a</li><li class="item">b</li><li class="item">c</li></ul></body>""",
    )
    page.goto(site.url + "/items-outer", wait_until="load")
    frame = _until(
        lambda: next((f for f in page.frames if f.url.endswith("/items")), None),
        10,
        "iframe was never registered",
    )
    assert frame.locator(".item").count() == 3
    assert frame.locator(".item").first.inner_text() == "a"
    assert frame.locator(".item").last.inner_text() == "c"
    assert frame.locator(".item").nth(1).inner_text() == "b"


def test_clip_outside_the_viewport_raises_and_full_page_covers_the_document(page, site):
    """Locks: an impossible clip is an Error, while full_page captures the document."""
    site.page(
        "/tall",
        """<body style="margin:0"><div style="height:2600px;background:#123">tall</div></body>""",
    )
    page.goto(site.url + "/tall", wait_until="load")
    with pytest.raises(cdp_page.Error):
        page.screenshot(clip={"x": 5000, "y": 5000, "width": 10, "height": 10})
    data = page.screenshot(full_page=True)
    image = Image.open(io.BytesIO(data))
    assert image.size[1] >= 2600
