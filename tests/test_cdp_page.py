"""Runs the CDP page client against the Chromium that Playwright installed.

No browser download: the executable is discovered under ms-playwright.  The
test proves the API surface the login flow uses (navigate, evaluate, selector
queries, selector waits, timeouts, screenshots with clip, mouse clicks,
keyboard typing, request interception) without asserting anything about a
headed Linux Chrome.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import pytest
from PIL import Image

from captcha import cdp_page

PAGE = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CDP test</title></head>
<body style="margin:0">
<div id="band" style="height:40px;background:rgb(255,0,0)"></div>
<div id="green" style="position:absolute;left:0;top:300px;width:40px;height:40px;background:rgb(0,255,0)"></div>
<h1>Login</h1>
<div class="br-message" style="display:block">ERL0003900 usuario e/ou senha invalidos</div>
<div class="br-message" style="display:none">hidden message</div>
<label>CPF <input id="accountId" name="accountId" type="text"></label>
<button id="continuar" onclick="bump()">Continuar</button>
<button id="autorizar" onclick="bump()">Autorizar</button>
<button id="disabled" disabled>Bloqueado</button>
<div id="clicks">0</div>
<div id="secret" style="display:none">oculto</div>
<img id="pixel" src="/pixel.png" alt="">
<iframe id="frame" src="/frame" style="width:300px;height:200px"></iframe>
<div style="height:2000px"></div>
<script>
function bump() {
  const el = document.getElementById('clicks');
  el.textContent = String(Number(el.textContent) + 1);
}
</script>
</body></html>"""

FRAME = b"""<!DOCTYPE html><html><body>
<div class="task-grid" style="width:120px;height:80px;background:#0af">grid</div>
</body></html>"""

FORM = b"""<!DOCTYPE html><html><body>
<form method="post" action="/sso/post">
  <input type="hidden" name="h-captcha-response" value="">
  <button type="submit">Continuar</button>
</form>
</body></html>"""


def _png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (0, 128, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - silence the test server
        pass

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path in {"/", "/index.html"}:
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path == "/frame":
            self._send(FRAME, "text/html; charset=utf-8")
        elif self.path == "/pixel.png":
            self._send(_png_bytes(), "image/png")
        elif self.path == "/form":
            self._send(FORM, "text/html; charset=utf-8")
        elif self.path == "/json":
            self._send(b'{"served":"origin"}', "application/json")
        elif self.path == "/blocked.json":
            self._send(b'{"blocked":false}', "application/json")
        else:
            self._send(b"missing", "text/plain", status=404)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._send(json.dumps({"received": body.decode()}).encode(), "application/json")


class _Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # noqa: D102 - aborted fetches are expected
        pass


@pytest.fixture(scope="session")
def site():
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


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


@pytest.fixture(scope="session")
def chrome():
    executable = _chromium_executable()
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="cdp-page-test-")
    arguments = [
        str(executable),
        "--disable-gpu",
        "--disable-background-networking",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--window-size=1280,900",
        "about:blank",
    ]
    if not os.environ.get("CAPY_CDP_HEADED"):
        arguments.insert(1, "--headless=new")
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("chromium exited during startup")
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=1) as response:
                response.read()
            break
        except OSError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("chromium did not expose CDP in time")
    try:
        yield endpoint
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        with contextlib.suppress(OSError):
            process.kill()


@pytest.fixture(scope="session")
def browser(chrome):
    instance = cdp_page.connect(chrome)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def page(browser, site):
    handle = browser.contexts[0].pages[0]
    handle.goto(site + "/", wait_until="load")
    return handle


def test_browser_version(browser):
    assert "Chrome" in browser.version


def test_goto_evaluate_and_url(page, site):
    assert page.url.startswith(site)
    assert page.evaluate("() => 1 + 1") == 2
    assert page.evaluate("(n) => n * 2", 21) == 42
    assert page.evaluate("() => null") is None
    assert page.evaluate("undefined") is None
    assert page.evaluate("document.title") == "CDP test"
    assert page.evaluate("() => ({a: [1, 2], b: 'x'})") == {"a": [1, 2], "b": "x"}
    assert page.evaluate("(obj) => obj.a + obj.b", {"a": 1, "b": 2}) == 3
    assert page.evaluate("(value) => value === null", None) is True
    with pytest.raises(cdp_page.Error) as failure:
        page.evaluate("() => { throw new Error('boom') }")
    assert "boom" in str(failure.value)


def test_goto_waits_for_document_state(page, site):
    page.goto(site + "/", wait_until="domcontentloaded")
    assert page.evaluate("document.readyState") in {"interactive", "complete"}
    page.goto(site + "/", wait_until="load")
    assert page.evaluate("document.readyState") == "complete"
    assert "<h1>Login</h1>" in page.content()
    assert page.title() == "CDP test"


def test_goto_reports_transport_errors(page):
    with pytest.raises(cdp_page.Error):
        page.goto("http://127.0.0.1:1/", wait_until="load", timeout=5000)


def test_query_selector_and_wait_for_selector(page):
    assert page.query_selector("#accountId") is not None
    assert page.query_selector("#nope") is None
    assert page.wait_for_selector(".br-message:visible", timeout=2000) is not None
    start = time.monotonic()
    with pytest.raises(cdp_page.TimeoutError):
        page.wait_for_selector("#nope", timeout=300)
    assert time.monotonic() - start >= 0.3
    assert page.locator(".br-message:visible").count() == 1
    assert page.locator(".br-message:visible").all_inner_texts()[0].startswith("ERL0003900")
    assert page.locator("#secret:visible").count() == 0
    page.locator("#secret").wait_for(state="hidden", timeout=1000)


def test_keyboard_and_locator_input(page):
    field = page.locator("#accountId").first
    field.click()
    page.keyboard.type("123.456.789-00")
    assert field.input_value() == "123.456.789-00"
    assert field.is_visible() is True
    assert field.bounding_box()["width"] > 50
    assert page.locator("#disabled").is_enabled() is False
    assert page.locator("button:has-text('Continuar')").first.is_visible() is True


def test_mouse_click_coordinates(page):
    box = page.locator("#continuar").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=12)
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    assert page.locator("#clicks").inner_text() == "1"
    page.locator("#continuar").click()
    assert page.locator("#clicks").inner_text() == "2"


def test_accessible_and_text_locators(page):
    assert page.get_by_role("heading", name="Login", exact=True).first.is_visible() is True
    page.get_by_role("button", name="Autorizar", exact=True).click()
    assert page.locator("#clicks").inner_text() == "1"
    assert page.get_by_text("ERL0003900", exact=False).is_visible() is True
    assert page.get_by_text("nao existe", exact=False).is_visible() is False


def test_screenshot_clip_is_viewport_relative(page, tmp_path):
    raw = page.screenshot()
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    viewport = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
    assert Image.open(io.BytesIO(raw)).size == (viewport["width"], viewport["height"])

    clip = {"x": 10, "y": 10, "width": 20, "height": 20}
    image = Image.open(io.BytesIO(page.screenshot(clip=clip))).convert("RGB")
    assert image.size == (20, 20)
    assert image.getpixel((10, 10)) == (255, 0, 0)

    page.evaluate("() => window.scrollTo(0, 250)")
    assert page.evaluate("() => window.scrollY") == 250
    image = Image.open(io.BytesIO(page.screenshot(clip=clip))).convert("RGB")
    assert image.getpixel((10, 10)) == (255, 255, 255)
    image = Image.open(
        io.BytesIO(page.screenshot(clip={"x": 10, "y": 50, "width": 20, "height": 20}))
    ).convert("RGB")
    assert image.getpixel((10, 10)) == (0, 255, 0)

    with pytest.raises(cdp_page.Error):
        page.screenshot(clip={"x": 0, "y": 5000, "width": 20, "height": 20})

    target = tmp_path / "shot.png"
    returned = page.screenshot(path=str(target), clip=clip)
    assert target.read_bytes() == returned


def test_request_events_and_route_interception(page, site):
    seen = []
    page.on("request", seen.append)
    page.evaluate("() => fetch('/json').then((r) => r.text())")
    deadline = time.monotonic() + 5
    while not any(request.url.endswith("/json") for request in seen):
        if time.monotonic() > deadline:
            pytest.fail("request event not delivered")
        time.sleep(0.05)

    page.route("**/blocked*", lambda route: route.abort())
    outcome = page.evaluate(
        "async () => { try { await fetch('/blocked.json'); return 'ok'; }"
        " catch (error) { return 'aborted'; } }"
    )
    assert outcome == "aborted"

    page.route(
        "**/json",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"served":"by-cdp"}'
        ),
    )
    served = page.evaluate(
        "async () => (await fetch('/json', {cache: 'no-store'})).text()"
    )
    assert served == '{"served":"by-cdp"}'

    page.evaluate(
        "() => fetch('/echo', {method: 'POST', body: 'a=1', headers: {'Content-Type':"
        " 'application/x-www-form-urlencoded'}}).then((r) => r.text())"
    )
    deadline = time.monotonic() + 5
    posted = None
    while posted is None:
        posted = next(
            (r for r in seen if r.method == "POST" and r.url.endswith("/echo")), None
        )
        if posted is None:
            if time.monotonic() > deadline:
                pytest.fail("POST request event not delivered")
            time.sleep(0.05)
    assert posted.post_data == "a=1"

    page.remove_listener("request", seen.append)
    seen.clear()
    page.evaluate("() => fetch('/json', {cache: 'no-store'})")
    time.sleep(0.3)
    assert seen == []
    page.unroute()


def test_handoff_post_is_fulfilled(page, site):
    """The flow's critical path: capture the SSO POST and render a local marker."""
    captured = {}

    def intercept(route):
        if route.request.method != "POST":
            return route.continue_()
        captured["body"] = route.request.post_data or ""
        captured["url"] = route.request.url
        route.fulfill(
            status=200, content_type="text/html", body="<main data-auth-handoff>ready</main>"
        )

    page.route("**/sso/**", intercept)
    try:
        page.goto(site + "/form", wait_until="load")
        try:
            page.evaluate(
                "() => { document.querySelector('[name=\"h-captcha-response\"]').value ="
                " 'token-123'; document.forms[0].submit(); }"
            )
        except cdp_page.Error:
            pass  # the navigation can destroy the context before the reply lands
        page.wait_for_selector("[data-auth-handoff]", timeout=10000)
        assert page.url.endswith("/sso/post")
        assert "token-123" in captured["body"]
        assert "data-auth-handoff" in page.content()
    finally:
        page.unroute()


def test_route_handles_navigations(page, site):
    page.route(
        "**/handoff",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="<main data-auth-handoff>ready</main>"
        ),
    )
    try:
        page.goto(site + "/handoff", wait_until="load")
        assert page.query_selector("[data-auth-handoff]") is not None
        assert "ready" in page.content()
    finally:
        page.unroute()

    page.route("**/refused", lambda route: route.abort())
    try:
        with pytest.raises(cdp_page.Error):
            page.goto(site + "/refused", wait_until="load", timeout=5000)
    finally:
        page.unroute()


def test_intercepted_post_still_emits_request_event(page):
    seen = []
    captured = {}

    def handler(route):
        captured.update(
            url=route.request.url,
            method=route.request.method,
            post_data=route.request.post_data,
            content_type=route.request.headers.get("content-type"),
        )
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.on("request", seen.append)
    page.route("**/echo", handler)
    try:
        served = page.evaluate(
            "async () => (await fetch('/echo', {method: 'POST', body: 'token=abc123',"
            " headers: {'Content-Type': 'application/x-www-form-urlencoded'}})).text()"
        )
        assert served == "{}"
        deadline = time.monotonic() + 5
        posted = None
        while posted is None:
            posted = next(
                (r for r in seen if r.method == "POST" and r.url.endswith("/echo")), None
            )
            if posted is None:
                if time.monotonic() > deadline:
                    pytest.fail("intercepted POST never reached page.on('request')")
                time.sleep(0.05)
        assert posted.post_data == "token=abc123"
        assert captured["method"] == "POST"
        assert captured["post_data"] == "token=abc123"
        assert captured["content_type"] == "application/x-www-form-urlencoded"
    finally:
        page.remove_listener("request", seen.append)
        page.unroute()


def test_route_continue_forwards_the_request(page, site):
    page.route("**/echo", lambda route: route.continue_())
    try:
        body = page.evaluate(
            "async () => (await fetch('/echo', {method: 'POST', body: 'keep=me',"
            " headers: {'Content-Type': 'application/x-www-form-urlencoded'}})).text()"
        )
        assert body == '{"received": "keep=me"}'
    finally:
        page.unroute()


def test_response_events_expose_body(page, site):
    captured = []
    arrived = threading.Event()

    def capture(response):
        captured.append(response)
        arrived.set()

    page.on("response", capture)
    try:
        page.evaluate("() => fetch('/pixel.png', {cache: 'no-store'})")
        deadline = time.monotonic() + 5
        while not arrived.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert arrived.is_set()
        image = next(r for r in captured if r.url.endswith("/pixel.png"))
        assert image.status == 200
        assert image.body().startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        page.remove_listener("response", capture)


def test_frames_expose_own_document(page):
    deadline = time.monotonic() + 5
    frame = None
    while frame is None:
        frame = next((f for f in page.frames if f.url.endswith("/frame")), None)
        if frame is None:
            if time.monotonic() > deadline:
                pytest.fail("child frame not registered")
            time.sleep(0.05)
    grid = frame.locator(".task-grid:visible")
    deadline = time.monotonic() + 5
    while grid.count() != 1:
        if time.monotonic() > deadline:
            pytest.fail("frame execution context never became usable")
        time.sleep(0.05)
    handles = grid.element_handles()
    assert len(handles) == 1
    assert handles[0].evaluate("(el) => el.className") == "task-grid"
    assert "grid" in frame.evaluate("() => document.body.innerText")

    frame_box = page.evaluate(
        "() => { const el = document.getElementById('frame');"
        " const r = el.getBoundingClientRect();"
        " return {x: r.x, y: r.y, border: parseFloat(getComputedStyle(el).borderLeftWidth)}; }"
    )
    box = handles[0].bounding_box()
    assert box["x"] == pytest.approx(frame_box["x"] + frame_box["border"] + 8, abs=1)
    assert box["y"] == pytest.approx(frame_box["y"] + frame_box["border"] + 8, abs=1)


def test_init_script_and_element_queries(page, site):
    identifier = page.add_init_script("() => { window.__cdp_init = 'yes'; }")
    try:
        page.goto(site + "/", wait_until="load")
        assert page.evaluate("window.__cdp_init") == "yes"
    finally:
        page.remove_init_script(identifier)

    dom = page.query_selector("body")
    assert dom is not None
    assert dom.query_selector("#accountId") is not None
    assert len(dom.query_selector_all(".br-message")) == 2
    assert dom.query_selector("#nope") is None


def test_context_cookies(page):
    page.evaluate("() => { document.cookie = 'probe=1; path=/'; }")
    names = {cookie["name"]: cookie for cookie in page.context.cookies()}
    assert names["probe"]["value"] == "1"
    assert names["probe"]["domain"] == "127.0.0.1"


def test_wait_for_url(page, site):
    page.evaluate("() => { window.location.href = '/json'; }")
    page.wait_for_url(lambda url: url.endswith("/json"), timeout=5000)
    assert page.url.endswith("/json")
    page.goto(site + "/", wait_until="load")


def test_locator_last_and_nth_pick_the_documented_nodes(page, site):
    """Locks: last/nth indexes are absolute matches, not frame-relative offsets."""
    page.goto(site + "/", wait_until="load")
    buttons = page.locator("button")
    assert buttons.count() == 3
    assert buttons.nth(1).inner_text() == "Autorizar"
    assert buttons.last.inner_text() == "Bloqueado"
    assert buttons.first.inner_text() == "Continuar"


def test_keyboard_press_tab_focuses_the_first_field(page, site):
    """Locks: press() sends real key events with the virtual key code."""
    page.goto(site + "/", wait_until="load")
    page.locator("#accountId").click(timeout=5000)
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement.id") == "continuar"


def test_wait_for_url_accepts_a_predicate_and_a_glob(page, site):
    """Locks: wait_for_url matches predicates and globs like Playwright."""
    page.goto(site + "/", wait_until="load")
    page.evaluate("() => { location.href = '/form'; }")
    page.wait_for_url(lambda url: url.endswith("/form"), timeout=10000)
    page.goto(site + "/", wait_until="load")
    page.evaluate("() => { location.href = '/form'; }")
    page.wait_for_url("**/form", timeout=10000)
    assert page.url.endswith("/form")


def test_cookies_can_be_filtered_by_url(page, site):
    """Locks: context.cookies(urls) applies domain/path/secure matching."""
    page.goto(site + "/", wait_until="load")
    page.evaluate("() => { document.cookie = 'cdp_probe=1; path=/'; }")
    cookies = page.context.cookies([site + "/"])
    assert any(cookie["name"] == "cdp_probe" for cookie in cookies)
    elsewhere = page.context.cookies(["http://example.com/"])
    assert not any(cookie["name"] == "cdp_probe" for cookie in elsewhere)


def test_evaluate_returns_unserializable_numbers(page, site):
    """Locks: NaN/Infinity/-0 travel through unserializableValue, not null."""
    page.goto(site + "/", wait_until="load")
    assert math.isnan(page.evaluate("() => NaN"))
    assert page.evaluate("() => Infinity") == math.inf
    assert str(page.evaluate("() => -0")) == "-0.0"
