"""Playwright-shaped page API over a raw Chrome DevTools Protocol socket.

This module replaces the slice of the Playwright page API that the GOV.BR /
Meu INSS login flow uses, without the Playwright driver process (the Node
sidecar that costs ~121 MB PSS).  Only ``websocket-client`` is required.

Design
------
One :class:`Connection` per browser endpoint owns the websocket.  A reader
thread receives frames and resolves command replies; a dispatcher thread runs
event subscribers (``page.on(...)`` handlers and ``page.route(...)``
callbacks), so a handler may itself send CDP commands without deadlocking the
socket.  Every public method is synchronous, like the Playwright sync API.

Element lookups, visibility checks and clicks are executed with explicit CDP
commands (``Runtime.evaluate``, ``Runtime.callFunctionOn``,
``Input.dispatchMouseEvent``, ``Input.dispatchKeyEvent``) rather than through
injected helper scripts.

Deliberate differences from Playwright are documented in
``docs/cdp-page-client.md`` and summarised in the class docstrings:
no actionability heuristics, no strict mode, ``evaluate`` returns JSON values
only, and ``:visible`` / ``:has-text`` are emulated by filtering.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit

import websocket

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30_000
DEFAULT_NAVIGATION_TIMEOUT = 30_000
_POLL = 0.05
_POLL_S = 0.05


class Error(Exception):
    """Playwright ``Error`` counterpart: protocol failures and page errors."""


class TimeoutError(Error):
    """Playwright ``TimeoutError`` counterpart: an explicit deadline expired."""


def _deadline(timeout):
    """Monotonic deadline for a Playwright timeout; 0 means no deadline."""
    return None if timeout == 0 else time.monotonic() + timeout / 1000


def _expired(deadline):
    return deadline is not None and time.monotonic() >= deadline


# --------------------------------------------------------------------------
# websocket transport
# --------------------------------------------------------------------------

class Connection:
    """JSON-RPC over a CDP websocket with background event dispatch."""

    def __init__(self, ws_url, *, timeout=DEFAULT_TIMEOUT / 1000):
        self._ws = websocket.create_connection(
            ws_url, suppress_origin=True, timeout=timeout, enable_multithread=True,
            # Screenshots arrive as megabytes of base64; validating UTF-8 costs
            # 479 ms of CPU per 665 KB frame against 16 ms without it.
            skip_utf8_validation=True,
        )
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._next_id = 0
        self._subscribers = {}
        self._subscribers_lock = threading.Lock()
        self._events = queue.Queue()
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="cdp-dispatcher", daemon=True
        )
        self._reader.start()
        self._dispatcher.start()

    # -- public ------------------------------------------------------------
    def send(self, method, params=None, *, session_id=None, timeout=DEFAULT_TIMEOUT / 1000):
        if self._closed:
            raise Error(f"{method}: CDP connection is closed")
        entry = [threading.Event(), None, None]
        with self._pending_lock:
            self._next_id += 1
            message_id = self._next_id
            self._pending[message_id] = entry
        payload = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        try:
            with self._send_lock:
                self._ws.send(json.dumps(payload))
        except Exception as error:  # noqa: BLE001 - transport errors all surface as Error
            with self._pending_lock:
                self._pending.pop(message_id, None)
            raise Error(f"{method}: send failed: {error}") from None
        if not entry[0].wait(timeout):
            with self._pending_lock:
                self._pending.pop(message_id, None)
            raise TimeoutError(f"{method}: CDP command timed out after {timeout:.3f}s")
        if entry[2] is not None:
            raise Error(entry[2])
        return entry[1]

    def subscribe(self, method, handler):
        with self._subscribers_lock:
            self._subscribers.setdefault(method, []).append(handler)

    def unsubscribe(self, method, handler):
        with self._subscribers_lock:
            handlers = self._subscribers.get(method)
            if handlers and handler in handlers:
                handlers.remove(handler)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._events.put(None)
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - already broken sockets do not matter here
            pass
        self._fail_pending(Error("CDP connection closed"))
        self._reader.join(timeout=2)
        self._dispatcher.join(timeout=2)

    # -- internals ---------------------------------------------------------
    def _fail_pending(self, error):
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        # Publish the error before waking the waiters: setting the event
        # first let a waiter read a null result/error pair and return None
        # as if the command had succeeded.
        for entry in pending:
            entry[2] = str(error)
        for event, _result, _error in pending:
            event.set()

    def _read_loop(self):
        while not self._closed:
            try:
                message = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as error:  # noqa: BLE001 - socket teardown
                if not self._closed:
                    self._closed = True
                    self._events.put(None)
                    self._fail_pending(Error(f"CDP connection closed: {error}"))
                return
            if not message:
                continue
            if isinstance(message, bytes):
                message = message.decode("utf-8", "replace")
            try:
                data = json.loads(message)
            except ValueError:
                log.debug("dropping non-JSON CDP frame")
                continue
            message_id = data.get("id")
            if message_id is not None:
                with self._pending_lock:
                    entry = self._pending.pop(message_id, None)
                if entry is not None:
                    if "error" in data:
                        error = data["error"]
                        entry[2] = error.get("message", "CDP error") + (
                            f" ({error['data']})" if error.get("data") else ""
                        )
                    else:
                        entry[1] = data.get("result", {})
                    entry[0].set()
                continue
            method = data.get("method")
            if method:
                self._events.put((method, data.get("params") or {}, data.get("sessionId")))

    def _dispatch_loop(self):
        while True:
            item = self._events.get()
            if item is None:
                return
            method, params, session_id = item
            with self._subscribers_lock:
                handlers = list(self._subscribers.get(method, ()))
            for handler in handlers:
                try:
                    handler(params, session_id)
                except Exception:  # noqa: BLE001 - a handler must not kill dispatch
                    log.exception("unhandled error in CDP event handler for %s", method)


# --------------------------------------------------------------------------
# selector translation
# --------------------------------------------------------------------------

_JS_PRELUDE = r"""
const __norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
const __vis = (el) => {
  if (!el || !el.getClientRects) return false;
  if (!el.getClientRects().length) return false;
  const style = getComputedStyle(el);
  return style.visibility !== 'hidden' && style.visibility !== 'collapse'
      && style.display !== 'none';
};
const __disabled = (el) => {
  if (!el) return true;
  if (el.disabled === true) return true;
  for (let node = el; node; node = node.parentElement) {
    if (node.getAttribute && node.getAttribute('aria-disabled') === 'true') return true;
    if (node.tagName === 'FIELDSET' && node.disabled === true) return true;
  }
  return false;
};
"""

_HAS_TEXT = re.compile(r":has-text\(\s*(?P<quote>['\"])(?P<text>.*?)(?P=quote)\s*\)", re.S)


def _split_selector(selector):
    """Translate Playwright-only pseudo classes into client-side filters."""
    visible = ":visible" in selector
    text = [match.group("text") for match in _HAS_TEXT.finditer(selector)]
    css = _HAS_TEXT.sub("", selector.replace(":visible", "")).strip()
    return css, visible, text


def _css_array_js(selector):
    css, visible, texts = _split_selector(selector)
    steps = []
    if visible:
        steps.append("__nodes = __nodes.filter((el) => __vis(el));")
    for text in texts:
        steps.append(
            "__nodes = __nodes.filter((el) => __norm(el.innerText || el.textContent || '')"
            f".toLowerCase().includes(__norm({json.dumps(text)}).toLowerCase()));"
        )
    return (
        "(() => {"
        + _JS_PRELUDE
        + f"let __nodes = Array.from(document.querySelectorAll({json.dumps(css or '*')}));"
        + "".join(steps)
        + "return __nodes;})()"
    )


_ROLE_SELECTORS = {
    "button": 'button, [role="button"], input[type="button"], input[type="submit"], '
              'input[type="reset"]',
    "heading": 'h1, h2, h3, h4, h5, h6, [role="heading"]',
    "link": 'a[href], [role="link"]',
    "textbox": 'input:not([type]), input[type="text"], input[type="email"], '
               'input[type="password"], input[type="tel"], textarea, [role="textbox"]',
    "checkbox": 'input[type="checkbox"], [role="checkbox"]',
    "radio": 'input[type="radio"], [role="radio"]',
    "img": 'img, [role="img"]',
    "listitem": 'li, [role="listitem"]',
    "paragraph": 'p, [role="paragraph"]',
}


def _role_array_js(role, name, exact):
    selector = _ROLE_SELECTORS.get(role, f'[role="{role}"]')
    name_js = "null" if name is None else json.dumps(name)
    return (
        "(() => {"
        + _JS_PRELUDE
        + f"""
  const __name = (el) => {{
    const labelled = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelled) {{
      const parts = labelled.split(/\\s+/).map((id) => {{
        const node = document.getElementById(id);
        return node ? (node.innerText || node.textContent || '') : '';
      }});
      const joined = __norm(parts.join(' '));
      if (joined) return joined;
    }}
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return __norm(aria);
    if (el.tagName === 'INPUT') return __norm(el.value || el.getAttribute('value') || '');
    return __norm(el.innerText || el.textContent || '');
  }};
  const __target = {name_js};
  return Array.from(document.querySelectorAll({json.dumps(selector)})).filter((el) => {{
    if (__target === null) return true;
    const value = __name(el);
    return {('value === __target' if exact else 'value.toLowerCase().includes(String(__target).toLowerCase())')};
  }});}})()"""
    )


def _text_array_js(text, exact):
    return (
        "(() => {"
        + _JS_PRELUDE
        + f"""
  const __target = __norm({json.dumps(text)});
  return Array.from(document.querySelectorAll('body *')).filter((el) => {{
    const value = __norm(el.innerText);
    return {('value === __target' if exact else 'value.toLowerCase().includes(__target.toLowerCase())')};
  }});}})()"""
    )


class _Query:
    """A deferred element set: renders to a JS expression returning an array."""

    def __init__(self, array_js, description):
        self.array_js = array_js
        self.description = description

    @classmethod
    def css(cls, selector):
        return cls(_css_array_js(selector), selector)

    @classmethod
    def role(cls, role, name, exact):
        label = f'role={role} name={name!r} exact={exact}'
        return cls(_role_array_js(role, name, exact), label)

    @classmethod
    def text(cls, text, exact):
        return cls(_text_array_js(text, exact), f"text={text!r} exact={exact}")

    def element_js(self, index):
        if index >= 0:
            pick = f"__nodes[{index}]"
        else:
            pick = f"__nodes[__nodes.length + ({index})]"
        return f"(() => {{ const __nodes = {self.array_js}; return {pick}; }})()"

    def count_js(self):
        return f"(() => {{ const __nodes = {self.array_js}; return __nodes.length; }})()"

    def texts_js(self):
        return (
            f"(() => {{ const __nodes = {self.array_js};"
            " return __nodes.map((el) => el.innerText); })()"
        )


def _looks_like_function(expression):
    stripped = expression.strip()
    if stripped.startswith("function"):
        return True
    if stripped.startswith("async"):
        return True
    if "=>" not in stripped:
        return False
    return bool(re.match(r"^\s*(async\s*)?\(?[\w$,\s{}[\]:='\"]*\)?\s*=>", stripped))


def _wrap_expression(expression, arg):
    if _looks_like_function(expression):
        return f"({expression})({json.dumps(arg)})"
    return expression


def _unserializable(value):
    table = {
        "NaN": float("nan"),
        "-0": -0.0,
        "Infinity": float("inf"),
        "-Infinity": float("-inf"),
    }
    if value in table:
        return table[value]
    if value.endswith("n"):
        return int(value[:-1])
    raise Error(f"unsupported unserializable value: {value}")


def _exception_text(response):
    details = response.get("exceptionDetails") or {}
    exception = details.get("exception") or {}
    message = exception.get("description") or details.get("text") or "evaluation failed"
    line = details.get("lineNumber")
    if line is not None:
        return f"{message} (line {line})"
    return message


# --------------------------------------------------------------------------
# network objects
# --------------------------------------------------------------------------

class Request:
    """Playwright ``Request`` counterpart (read-only, event payload)."""

    def __init__(self, url, method, headers, post_data, resource_type, frame_url=None):
        self.url = url
        self.method = method
        self._headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._post_data = post_data
        self.resource_type = resource_type
        self.frame_url = frame_url

    @property
    def headers(self):
        return dict(self._headers)

    @property
    def post_data(self):
        return self._post_data

    def __repr__(self):
        return f"<Request {self.method} {self.url}>"


class Response:
    """Playwright ``Response`` counterpart; ``body()`` reads over CDP."""

    def __init__(self, page, request, request_id, status, headers, url):
        self._page = page
        self.request = request
        self._request_id = request_id
        self.status = status
        self.url = url
        self._headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._body = None

    @property
    def headers(self):
        return dict(self._headers)

    def body(self, *, timeout=DEFAULT_TIMEOUT):
        """Return the response body as bytes.

        Playwright keeps the body in its own buffer; here it is fetched with
        ``Network.getResponseBody``, which fails once Chrome evicts the
        resource, so this polls until the body is readable.
        """
        if self._body is not None:
            return self._body
        deadline = time.monotonic() + timeout / 1000
        last = None
        while True:
            try:
                result = self._page._send(
                    "Network.getResponseBody", {"requestId": self._request_id}
                )
            except Error as error:
                last = error
            else:
                body = result.get("body", "")
                raw = base64.b64decode(body) if result.get("base64Encoded") else body.encode()
                self._body = raw
                return raw
            if time.monotonic() >= deadline:
                raise Error(f"response body unavailable: {last}") from None
            time.sleep(_POLL_S)

    def text(self, **kwargs):
        return self.body(**kwargs).decode("utf-8", "replace")

    def __repr__(self):
        return f"<Response {self.status} {self.url}>"


class NavigationResponse:
    """Minimal stand-in for the Response returned by ``page.goto``."""

    def __init__(self, url, loader_id, status=None):
        self.url = url
        self.status = status
        self.loader_id = loader_id

    def __repr__(self):
        return f"<NavigationResponse {self.status} {self.url}>"


# --------------------------------------------------------------------------
# input devices
# --------------------------------------------------------------------------

class Mouse:
    def __init__(self, page):
        self._page = page
        self._x = 0.0
        self._y = 0.0

    def move(self, x, y, *, steps=1, timeout=DEFAULT_TIMEOUT):
        steps = max(1, int(steps))
        start = (self._x, self._y)
        for step in range(1, steps + 1):
            ratio = step / steps
            self._dispatch(
                "mouseMoved",
                start[0] + (x - start[0]) * ratio,
                start[1] + (y - start[1]) * ratio,
                button="none",
                buttons=0,
                click_count=0,
            )
        self._x, self._y = x, y

    def click(self, x, y, *, button="left", click_count=1, delay=0, timeout=DEFAULT_TIMEOUT):
        self.move(x, y)
        self.down(button=button, click_count=click_count)
        if delay:
            time.sleep(delay / 1000)
        self.up(button=button, click_count=click_count)

    def down(self, *, button="left", click_count=1):
        self._dispatch(
            "mousePressed", self._x, self._y, button=button,
            buttons=_BUTTON_MASK.get(button, 1), click_count=click_count,
        )

    def up(self, *, button="left", click_count=1):
        self._dispatch(
            "mouseReleased", self._x, self._y, button=button,
            buttons=0, click_count=click_count,
        )

    def _dispatch(self, event_type, x, y, *, button, buttons, click_count):
        self._page._send(
            "Input.dispatchMouseEvent",
            {
                "type": event_type,
                "x": float(x),
                "y": float(y),
                "button": button,
                "buttons": buttons,
                "clickCount": click_count,
            },
        )


_BUTTON_MASK = {"left": 1, "right": 2, "middle": 4}


class Keyboard:
    def __init__(self, page):
        self._page = page

    def type(self, text, *, delay=0, timeout=DEFAULT_TIMEOUT):
        for char in text:
            self._page._send(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "text": char, "unmodifiedText": char, "key": char},
            )
            self._page._send("Input.dispatchKeyEvent", {"type": "keyUp", "key": char})
            if delay:
                time.sleep(delay / 1000)

    def press(self, key, *, delay=0, timeout=DEFAULT_TIMEOUT):
        mapping = {"Enter": ("\r", 13), "Tab": ("\t", 9), "Escape": ("", 27)}
        text, code = mapping.get(key, (key if len(key) == 1 else "", 0))
        params = {"key": key}
        if text:
            params.update(type="keyDown", text=text, unmodifiedText=text)
        else:
            params["type"] = "rawKeyDown"
        if code:
            params.update(windowsVirtualKeyCode=code, nativeVirtualKeyCode=code)
        self._page._send("Input.dispatchKeyEvent", params)
        self._page._send(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": key, **({"windowsVirtualKeyCode": code,
                                               "nativeVirtualKeyCode": code} if code else {})},
        )
        if delay:
            time.sleep(delay / 1000)


# --------------------------------------------------------------------------
# elements and locators
# --------------------------------------------------------------------------

class ElementHandle:
    """Playwright ``ElementHandle`` counterpart backed by a CDP RemoteObject.

    Handles are session-scoped: one resolved inside a cross-origin iframe uses
    that frame's child session.  Coordinates are reported in main-frame
    viewport pixels like Playwright, so a box or click inside an iframe carries
    the frame offset.  A detached or replaced node surfaces as ``Error``; the
    :class:`Locator` methods retry instead of trusting a stale handle.
    """

    def __init__(self, page, object_id, description="element", frame=None, session=None):
        self._page = page
        self._object_id = object_id
        self.description = description
        self._frame = frame
        self._session = session or page._session

    def __bool__(self):
        return True

    def __repr__(self):
        return f"<ElementHandle {self.description}>"

    def release(self):
        """Drop the CDP RemoteObject reference held for this handle."""
        if self._object_id is None:
            return
        object_id, self._object_id = self._object_id, None
        try:
            self._page._send("Runtime.releaseObject", {"objectId": object_id},
                             session=self._session)
        except Error:
            pass

    def _call(self, body, *, by_value=True, timeout=DEFAULT_TIMEOUT):
        if self._object_id is None:
            raise Error(f"{self.description}: handle has been released")
        response = self._page._send(
            "Runtime.callFunctionOn",
            {
                "objectId": self._object_id,
                "functionDeclaration": f"function() {{ {_JS_PRELUDE} {body} }}",
                "returnByValue": by_value,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
            session=self._session,
        )
        if response.get("exceptionDetails"):
            raise Error(_exception_text(response))
        result = response.get("result", {})
        if not by_value:
            return result
        if "unserializableValue" in result:
            return _unserializable(result["unserializableValue"])
        return result.get("value")

    def evaluate(self, expression, arg=None, *, timeout=DEFAULT_TIMEOUT):
        if _looks_like_function(expression):
            body = f"return ({expression}).call(this, this, {json.dumps(arg)});"
        else:
            body = f"return ({expression});"
        return self._call(body, timeout=timeout)

    def bounding_box(self, *, timeout=DEFAULT_TIMEOUT):
        box = self._call(
            "if (!__vis(this)) return null;"
            "const rect = this.getBoundingClientRect();"
            "return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};"
        )
        if not box:
            return None
        offset = self._frame_offset()
        box["x"] += offset[0]
        box["y"] += offset[1]
        return box

    def _frame_offset(self):
        return self._frame._viewport_offset() if self._frame is not None else (0.0, 0.0)

    def is_visible(self):
        return bool(self._call("return __vis(this);"))

    def is_enabled(self):
        return bool(self._call("return !__disabled(this);"))

    def inner_text(self, *, timeout=DEFAULT_TIMEOUT):
        return self._call("return this.innerText;")

    def text_content(self, *, timeout=DEFAULT_TIMEOUT):
        return self._call("return this.textContent;")

    def input_value(self, *, timeout=DEFAULT_TIMEOUT):
        return self._call("return this.value === undefined ? '' : String(this.value);")

    def get_attribute(self, name):
        return self._call(f"return this.getAttribute({json.dumps(name)});")

    def query_selector(self, selector):
        return self.query_selector_all(selector)[:1] or None

    def query_selector_all(self, selector):
        array = self._call(
            f"return Array.from(this.querySelectorAll({json.dumps(selector)}));",
            by_value=False,
        )
        return _handles_from_array(
            self._page, array, f"{self.description} {selector}", self._frame, self._session
        )

    def click(self, *, timeout=DEFAULT_TIMEOUT):
        center = self._call(
            "this.scrollIntoView({block: 'center', inline: 'center'});"
            "if (!__vis(this)) return null;"
            "const rect = this.getBoundingClientRect();"
            "return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2};"
        )
        if not center:
            raise Error(f"{self.description}: element is not visible and cannot be clicked")
        offset = self._frame_offset()
        self._page.mouse.click(center["x"] + offset[0], center["y"] + offset[1])


def _handles_from_array(page, array, description, frame=None, session=None):
    object_id = array.get("objectId")
    if not object_id:
        return []
    session = session or (frame._session() if frame is not None else page._session)
    length = page._send(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": "function() { return this.length; }",
            "returnByValue": True,
        },
        session=session,
    ).get("result", {}).get("value", 0)
    handles = []
    try:
        for index in range(int(length)):
            item = page._send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": f"function() {{ return this[{index}]; }}",
                    "returnByValue": False,
                },
                session=session,
            ).get("result", {})
            if item.get("objectId"):
                handles.append(
                    ElementHandle(page, item["objectId"], f"{description}[{index}]", frame, session)
                )
    finally:
        try:
            page._send("Runtime.releaseObject", {"objectId": object_id}, session=session)
        except Error:
            pass
    return handles


class Locator:
    """Playwright ``Locator`` counterpart with the subset the flow calls.

    Semantics kept from Playwright: resolving a locator is retried while the
    document or frame is being replaced, ``first``/``last``/``nth`` stay inside
    their frame, clicks retry when the node is detached between resolution and
    input, and ``is_enabled`` follows the ``disabled`` / ``aria-disabled`` /
    ``fieldset[disabled]`` rules.  Missing elements are reported as
    ``TimeoutError`` (waits) or ``False`` (state checks), never as a crash.
    """

    def __init__(self, page, query, index=0, frame=None):
        self._page = page
        self._query = query
        self._index = index
        self._owner = frame
        self._last_error = None

    # -- selection ---------------------------------------------------------
    def _with_index(self, index):
        return Locator(self._page, self._query, index, self._owner)

    @property
    def first(self):
        return self._with_index(0)

    @property
    def last(self):
        return self._with_index(-1)

    def nth(self, index):
        return self._with_index(index)

    def __repr__(self):
        return f"<Locator {self._query.description} #{self._index}>"

    # -- resolution --------------------------------------------------------
    def _session(self):
        return self._owner._session() if self._owner is not None else self._page._session

    def _context_args(self, timeout=DEFAULT_TIMEOUT):
        if self._owner is None:
            return {}, self._page._session
        session, context_id = self._owner._context(timeout)
        return {"context_id": context_id}, session

    def _evaluate(self, expression, *, return_by_value=True, timeout=DEFAULT_TIMEOUT):
        context, session = self._context_args(timeout)
        return self._page._evaluate_raw(
            expression, return_by_value=return_by_value, session=session, **context
        )

    def _resolve(self, deadline=None):
        """Return a handle for the indexed match, or None.

        A destroyed execution context (document swap, frame navigation) or an
        unavailable frame context means "not there yet", not a hard failure.
        When ``deadline`` is given, the frame-context lookup is bounded by the
        caller's remaining budget instead of a fixed 10 s, so a wait never
        overshoots the timeout it was given while a frame is being replaced.
        """
        remaining = DEFAULT_TIMEOUT
        if deadline is not None:
            remaining = max(0.0, (deadline - time.monotonic()) * 1000)
        try:
            context, session = self._context_args(remaining)
            result = self._page._evaluate_raw(
                self._query.element_js(self._index),
                return_by_value=False,
                session=session,
                **context,
            )
        except Error as error:
            self._last_error = error
            return None
        object_id = result.get("objectId")
        if not object_id:
            return None
        return ElementHandle(
            self._page, object_id, self._query.description, self._owner, self._session()
        )

    def _timeout_message(self, state, timeout):
        message = (
            f"locator({self._query.description}): timeout {timeout}ms exceeded waiting for "
            f"{state}; url={self._page.url}"
        )
        if self._last_error is not None:
            message += f"; last error: {self._last_error}"
        return message

    def _poll_until(self, action, state, timeout, deadline):
        while True:
            result = action()
            if result is not None:
                return result
            if _expired(deadline):
                raise TimeoutError(self._timeout_message(state, timeout))
            time.sleep(_POLL_S)

    def _wait_attached(self, timeout):
        timeout = self._page.default_timeout if timeout is None else timeout
        deadline = _deadline(timeout)
        return self._poll_until(
            lambda: self._resolve(deadline), "attached", timeout, deadline
        )

    # -- actions -----------------------------------------------------------
    def count(self):
        try:
            return self._evaluate(self._query.count_js())
        except Error as error:
            self._last_error = error
            return 0

    def element_handles(self):
        array = self._evaluate(self._query.array_js, return_by_value=False)
        return _handles_from_array(
            self._page, array, self._query.description, self._owner, self._session()
        )

    def click(self, *, timeout=DEFAULT_TIMEOUT):
        timeout = self._page.default_timeout if timeout is None else timeout
        deadline = _deadline(timeout)
        state = "click"
        while True:
            handle = self._resolve(deadline)
            if handle is not None:
                try:
                    if handle.is_visible():
                        # Playwright's locator.click waits for enablement;
                        # clicking a disabled node silently does nothing and
                        # looks like a missing navigation downstream.
                        if handle.is_enabled():
                            handle.click(timeout=timeout)
                            return
                        state = "click (element is not enabled)"
                except Error as error:
                    # The node can be detached between resolution and input;
                    # Playwright re-resolves, so do the same.
                    self._last_error = error
                finally:
                    handle.release()
            if _expired(deadline):
                raise TimeoutError(self._timeout_message(state, timeout))
            time.sleep(_POLL_S)

    def bounding_box(self, *, timeout=DEFAULT_TIMEOUT):
        timeout = self._page.default_timeout if timeout is None else timeout
        deadline = _deadline(timeout)
        while True:
            handle = self._resolve(deadline)
            if handle is not None:
                try:
                    return handle.bounding_box(timeout=timeout)
                except Error as error:
                    self._last_error = error
                finally:
                    handle.release()
            if _expired(deadline):
                raise TimeoutError(self._timeout_message("bounding_box", timeout))
            time.sleep(_POLL_S)

    def _read(self, method, timeout, state):
        timeout = self._page.default_timeout if timeout is None else timeout
        deadline = _deadline(timeout)
        while True:
            handle = self._resolve(deadline)
            if handle is not None:
                try:
                    return getattr(handle, method)(timeout=timeout)
                except Error as error:
                    self._last_error = error
                finally:
                    handle.release()
            if _expired(deadline):
                raise TimeoutError(self._timeout_message(state, timeout))
            time.sleep(_POLL_S)

    def input_value(self, *, timeout=DEFAULT_TIMEOUT):
        return self._read("input_value", timeout, "input_value")

    def inner_text(self, *, timeout=DEFAULT_TIMEOUT):
        return self._read("inner_text", timeout, "inner_text")

    def text_content(self, *, timeout=DEFAULT_TIMEOUT):
        return self._read("text_content", timeout, "text_content")

    def get_attribute(self, name, *, timeout=DEFAULT_TIMEOUT):
        timeout = self._page.default_timeout if timeout is None else timeout
        deadline = _deadline(timeout)
        while True:
            handle = self._resolve(deadline)
            if handle is not None:
                try:
                    return handle.get_attribute(name)
                except Error as error:
                    self._last_error = error
                finally:
                    handle.release()
            if _expired(deadline):
                raise TimeoutError(self._timeout_message(f"get_attribute({name})", timeout))
            time.sleep(_POLL_S)

    def is_visible(self):
        handle = self._resolve()
        if handle is None:
            return False
        try:
            return handle.is_visible()
        except Error as error:
            self._last_error = error
            return False
        finally:
            handle.release()

    def is_enabled(self):
        handle = self._resolve()
        if handle is None:
            return False
        try:
            return handle.is_enabled()
        except Error as error:
            self._last_error = error
            return False
        finally:
            handle.release()

    def all_inner_texts(self):
        try:
            return self._evaluate(self._query.texts_js())
        except Error as error:
            self._last_error = error
            return []

    def evaluate(self, expression, arg=None, *, timeout=DEFAULT_TIMEOUT):
        handle = self._wait_attached(timeout)
        try:
            return handle.evaluate(expression, arg, timeout=timeout)
        finally:
            handle.release()

    def _wait_for_handle(self, state, timeout):
        """Wait for a state and return the handle that satisfied it.

        The handle is owned by the caller. ``visible``/``attached`` return a
        handle; ``hidden``/``detached`` return ``None``.  Every poll re-resolves
        through document and frame replacement, so a wait that starts during a
        re-render (the CPF step) ends when the element comes back instead of
        raising on the first transient miss.
        """
        if state not in {"attached", "detached", "visible", "hidden"}:
            raise ValueError(f"unsupported state: {state}")
        timeout = self._page.default_timeout if timeout is None else timeout
        deadline = _deadline(timeout)
        while True:
            handle = self._resolve(deadline)
            if state == "attached" and handle is not None:
                return handle
            if state in {"detached", "hidden"} and handle is None:
                return None
            if handle is not None:
                try:
                    visible = handle.is_visible()
                except Error as error:
                    self._last_error = error
                    visible = False
                if state == "visible" and visible:
                    return handle
                if state == "hidden" and not visible:
                    handle.release()
                    return None
                handle.release()
            if _expired(deadline):
                raise TimeoutError(self._timeout_message(state, timeout))
            time.sleep(_POLL_S)

    def wait_for(self, *, state="visible", timeout=DEFAULT_TIMEOUT):
        handle = self._wait_for_handle(state, timeout)
        if handle is not None:
            handle.release()


class FrameLocator(Locator):
    def __init__(self, frame, query, index=0):
        super().__init__(frame._page, query, index, frame)

    def _with_index(self, index):
        return FrameLocator(self._owner, self._query, index)


# --------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------

class Frame:
    """Playwright ``Frame`` counterpart for main, same-process and OOPIF frames.

    Cross-origin iframes run in their own target; their execution contexts are
    discovered through the child CDP session, and the owning session is used
    for every evaluation and RemoteObject call inside that frame.
    """

    def __init__(self, page, frame_id, parent=None, url="", session=None):
        self._page = page
        self._frame_id = frame_id
        self.parent_frame = parent
        self._url = url
        self._session_id = session
        self.child_frames = []

    @property
    def url(self):
        return self._url

    def __repr__(self):
        return f"<Frame {self._frame_id} {self._url}>"

    def _session(self):
        return self._session_id or self._page._session

    def _context(self, timeout=10000):
        timeout = max(0.0, timeout)
        deadline = time.monotonic() + timeout / 1000
        while True:
            entry = self._page._contexts.get(self._frame_id)
            if entry is not None:
                return entry
            if time.monotonic() >= deadline:
                raise Error(
                    f"frame {self._frame_id} ({self._url or 'about:blank'}): "
                    f"execution context not available after {timeout:.0f}ms"
                )
            time.sleep(_POLL_S)

    def evaluate(self, expression, arg=None, *, timeout=DEFAULT_TIMEOUT):
        session, context_id = self._context()
        return self._page._evaluate_raw(
            _wrap_expression(expression, arg),
            return_by_value=True,
            context_id=context_id,
            session=session,
            timeout=timeout,
        )

    def _viewport_offset(self):
        """Sum the iframe content-box offsets up to the main frame, in CSS px.

        Playwright reports element boxes in main-frame viewport coordinates;
        CDP reports them in the owning frame's document, so each ancestor
        iframe's content origin is added back.  ``DOM.getFrameOwner`` must run
        in the parent frame's session.
        """
        offset_x = offset_y = 0.0
        frame = self
        while frame.parent_frame is not None:
            parent = frame.parent_frame
            try:
                owner = frame._page._send(
                    "DOM.getFrameOwner", {"frameId": frame._frame_id}, session=parent._session()
                )
                model = frame._page._send(
                    "DOM.getBoxModel",
                    {"backendNodeId": owner["backendNodeId"]},
                    session=parent._session(),
                )["model"]
                content = model["content"]
            except (Error, KeyError) as error:
                log.warning("frame %s: cannot read iframe offset (%s)", frame._frame_id, error)
                return offset_x, offset_y
            offset_x += float(content[0])
            offset_y += float(content[1])
            frame = parent
        return offset_x, offset_y

    def locator(self, selector):
        return FrameLocator(self, _Query.css(selector))

    def get_by_role(self, role, *, name=None, exact=False):
        return FrameLocator(self, _Query.role(role, name, exact))

    def get_by_text(self, text, *, exact=False):
        return FrameLocator(self, _Query.text(text, exact))

    def query_selector(self, selector):
        return self.locator(selector)._resolve()

    def wait_for_selector(self, selector, *, state="visible", timeout=DEFAULT_TIMEOUT):
        return self.locator(selector)._wait_for_handle(state, timeout)

    def wait_for_timeout(self, timeout):
        time.sleep(timeout / 1000)


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

_ABORT_REASONS = {
    "aborted": "Aborted",
    "accessdenied": "AccessDenied",
    "addressunreachable": "AddressUnreachable",
    "blockedbyclient": "BlockedByClient",
    "blockedbyresponse": "BlockedByResponse",
    "connectionaborted": "ConnectionAborted",
    "connectionclosed": "ConnectionClosed",
    "connectionfailed": "ConnectionFailed",
    "connectionrefused": "ConnectionRefused",
    "connectionreset": "ConnectionReset",
    "internetdisconnected": "InternetDisconnected",
    "namenotresolved": "NameNotResolved",
    "timedout": "TimedOut",
    "failed": "Failed",
}


class Route:
    """Playwright ``Route`` counterpart over ``Fetch.requestPaused``.

    Exactly one of ``continue_``/``abort``/``fulfill`` must be called; the
    request stays paused otherwise, which stalls the page just like an
    unhandled Playwright route.
    """

    def __init__(self, page, request_id, request):
        self._page = page
        self._request_id = request_id
        self.request = request
        self._handled = False

    def _send(self, method, params):
        if self._handled:
            raise Error("route has already been handled")
        self._handled = True
        return self._page._send(method, params)

    def continue_(self, *, url=None, method=None, headers=None, post_data=None,
                  timeout=DEFAULT_TIMEOUT):
        params = {"requestId": self._request_id}
        if url is not None:
            params["url"] = url
        if method is not None:
            params["method"] = method
        if headers is not None:
            params["headers"] = [{"name": str(k), "value": str(v)} for k, v in headers.items()]
        if post_data is not None:
            raw = post_data.encode() if isinstance(post_data, str) else bytes(post_data)
            params["postData"] = base64.b64encode(raw).decode()
        return self._send("Fetch.continueRequest", params)

    def abort(self, error_code="failed", *, timeout=DEFAULT_TIMEOUT):
        return self._send(
            "Fetch.failRequest",
            {
                "requestId": self._request_id,
                "errorReason": _ABORT_REASONS.get(str(error_code).lower(), "Failed"),
            },
        )

    def fulfill(self, *, status=200, headers=None, body=None, content_type=None,
                timeout=DEFAULT_TIMEOUT):
        raw = b"" if body is None else (body.encode() if isinstance(body, str) else bytes(body))
        merged = dict(headers or {})
        if content_type and not any(k.lower() == "content-type" for k in merged):
            merged["Content-Type"] = content_type
        params = {
            "requestId": self._request_id,
            "responseCode": int(status),
            "responseHeaders": [
                {"name": str(k), "value": str(v)} for k, v in merged.items()
            ],
            "body": base64.b64encode(raw).decode(),
        }
        return self._send("Fetch.fulfillRequest", params)


def _glob_to_regex(pattern):
    out = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                out.append(".*")
                index += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append(".")
        else:
            out.append(re.escape(char))
        index += 1
    out.append("$")
    return re.compile("".join(out))


def _cdp_pattern(pattern):
    if "**" not in pattern:
        return None
    return pattern.replace("**", "*")


class _UrlMatcher:
    def __init__(self, url):
        self._spec = url
        self._callable = None
        self._regex = None
        self._glob = None
        if callable(url) and not hasattr(url, "match"):
            self._callable = url
        elif hasattr(url, "match"):
            self._regex = url
        else:
            self._glob = str(url)
            self._regex = _glob_to_regex(self._glob)

    @property
    def cdp_pattern(self):
        return _cdp_pattern(self._glob) if self._glob else None

    def __call__(self, url):
        if self._callable is not None:
            return bool(self._callable(url))
        return bool(self._regex.match(url))

    def same_spec(self, spec):
        if self._spec is spec:
            return True
        return isinstance(spec, str) and self._glob == spec


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

class Page:
    """Playwright ``Page`` counterpart for one CDP page target."""

    def __init__(self, browser, target_id):
        self._browser = browser
        self._conn = browser._connection
        self._target_id = target_id
        response = self._conn.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self._session = response["sessionId"]
        self._listeners = {}
        self._routes = []
        self._paused = set()
        self._markers = set()
        self._main_loaders = set()
        self._requests = {}
        self._contexts = {}
        self._sessions = {}
        self._frames = {}
        self._main_frame_id = None
        self._url = "about:blank"
        self.default_timeout = DEFAULT_TIMEOUT
        self.default_navigation_timeout = DEFAULT_NAVIGATION_TIMEOUT
        self._mouse = Mouse(self)
        self._keyboard = Keyboard(self)
        self._state_lock = threading.RLock()
        self._subscriptions = [
            ("Page.lifecycleEvent", self._on_lifecycle),
            ("Page.frameAttached", self._on_frame_attached),
            ("Page.frameNavigated", self._on_frame_navigated),
            ("Page.frameDetached", self._on_frame_detached),
            ("Page.navigatedWithinDocument", self._on_navigated_within_document),
            ("Runtime.executionContextCreated", self._on_context_created),
            ("Runtime.executionContextDestroyed", self._on_context_destroyed),
            ("Runtime.executionContextsCleared", self._on_contexts_cleared),
            ("Network.requestWillBeSent", self._on_request),
            ("Network.responseReceived", self._on_response),
            ("Fetch.requestPaused", self._on_request_paused),
            ("Target.attachedToTarget", self._on_attached_to_target),
            ("Target.detachedFromTarget", self._on_detached_from_target),
            ("Target.targetInfoChanged", self._on_target_info_changed),
        ]
        for method, handler in self._subscriptions:
            self._conn.subscribe(method, handler)
        self._enable()
        self._load_frame_tree()

    # -- setup -------------------------------------------------------------
    def _send(self, method, params=None, *, timeout=DEFAULT_TIMEOUT, session=None):
        # Playwright's timeout=0 disables the deadline; the CDP command still
        # needs a real wall clock bound, so fall back to the default.
        timeout = DEFAULT_TIMEOUT if not timeout else timeout
        return self._conn.send(
            method, params, session_id=session or self._session, timeout=timeout / 1000
        )

    def _enable(self):
        self._send("Page.enable")
        self._send("Page.setLifecycleEventsEnabled", {"enabled": True})
        self._send("Runtime.enable")
        self._send(
            "Network.enable",
            {
                "maxTotalBufferSize": 20 * 1024 * 1024,
                "maxResourceBufferSize": 8 * 1024 * 1024,
                "maxPostDataSize": 1024 * 1024,
            },
        )
        # Cross-origin iframes live in their own target (OOPIF); without
        # auto-attach their documents are invisible to this session.
        self._send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            timeout=DEFAULT_TIMEOUT,
        )

    def _load_frame_tree(self):
        tree = self._send("Page.getFrameTree")["frameTree"]
        self._url = tree["frame"]["url"]
        self._main_frame_id = tree["frame"]["id"]
        self._frames[self._main_frame_id] = Frame(
            self, self._main_frame_id, None, tree["frame"]["url"]
        )
        for child in tree.get("childFrames", []):
            self._register_child(child, self._frames[self._main_frame_id])

    def _register_child(self, node, parent):
        frame = Frame(self, node["frame"]["id"], parent, node["frame"]["url"])
        parent.child_frames.append(frame)
        self._frames[frame._frame_id] = frame
        for child in node.get("childFrames", []):
            self._register_child(child, frame)

    # -- properties --------------------------------------------------------
    @property
    def context(self):
        return self._browser.contexts[0]

    @property
    def url(self):
        return self._url

    @property
    def main_frame(self):
        return self._frames.get(self._main_frame_id)

    @property
    def frames(self):
        with self._state_lock:
            return list(self._frames.values())

    @property
    def mouse(self):
        return self._mouse

    @property
    def keyboard(self):
        return self._keyboard

    # -- event plumbing ----------------------------------------------------
    def on(self, event, handler):
        if event not in {"request", "response"}:
            raise ValueError(f"unsupported event: {event}")
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        handlers = self._listeners.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def _emit(self, event, payload):
        for handler in list(self._listeners.get(event, ())):
            try:
                handler(payload)
            except Exception:  # noqa: BLE001 - listener failures must not stall the page
                log.exception("unhandled error in page.on(%r) listener", event)

    def _is_child_session(self, session_id):
        return session_id is not None and session_id in self._sessions

    def _on_lifecycle(self, params, session_id):
        if session_id != self._session:
            return
        if params.get("frameId") != self._main_frame_id:
            return
        self._markers.add((params.get("loaderId"), params.get("name")))

    def _on_frame_attached(self, params, session_id):
        if session_id != self._session:
            return
        with self._state_lock:
            parent = self._frames.get(params.get("parentFrameId"))
            frame_id = params.get("frameId")
            if parent is not None and frame_id and frame_id not in self._frames:
                frame = Frame(self, frame_id, parent, "")
                parent.child_frames.append(frame)
                self._frames[frame_id] = frame

    def _drop_child_frames(self):
        """Forget child frames of a replaced document.

        Chrome does not emit ``frameDetached`` for the children of a replaced
        main document, so same-session frames are pruned here.  Frames that
        live in their own target (OOPIF) are kept: their lifetime is tracked
        through ``Target.detachedFromTarget``, and a late main-frame
        announcement must not drop a cross-origin iframe that just attached.
        """
        main = self._frames.get(self._main_frame_id)
        if main is None:
            return
        for frame in list(self._frames.values()):
            if frame is main:
                continue
            if frame._session_id and frame._session_id in self._sessions:
                continue
            del self._frames[frame._frame_id]
            if frame in main.child_frames:
                main.child_frames.remove(frame)

    def _on_frame_detached(self, params, session_id):
        if session_id != self._session:
            return
        if params.get("reason") == "swap":
            # The frame was moved to its own process (OOPIF), not removed; its
            # lifetime from here is tracked by the child target session.
            return
        with self._state_lock:
            frame = self._frames.pop(params.get("frameId"), None)
            if frame is not None and frame.parent_frame is not None:
                try:
                    frame.parent_frame.child_frames.remove(frame)
                except ValueError:
                    pass

    def _on_frame_navigated(self, params, session_id):
        if session_id != self._session and not self._is_child_session(session_id):
            return
        frame_info = params.get("frame") or {}
        frame_id = frame_info.get("id")
        if not frame_id:
            return
        with self._state_lock:
            frame = self._frames.get(frame_id)
            if frame is None:
                parent = self._frames.get(frame_info.get("parentId")) or self._frames.get(
                    self._main_frame_id
                )
                frame = Frame(
                    self, frame_id, parent, frame_info.get("url", ""), session=session_id
                )
                self._frames[frame_id] = frame
                if parent is not None and frame not in parent.child_frames:
                    parent.child_frames.append(frame)
            if session_id == self._session and not (
                frame._session_id and frame._session_id in self._sessions
            ):
                # A page-session announcement for an OOPIF frame must not strip
                # its child session; that session is the only way in.
                frame._session_id = None
            frame._url = frame_info.get("url", frame._url)
            if frame_id == self._main_frame_id:
                self._url = frame._url
                if frame_info.get("loaderId"):
                    self._main_loaders.add(frame_info["loaderId"])
                if not frame_info.get("parentId"):
                    # A new main document replaces every child frame; Chrome
                    # does not emit frameDetached for them.
                    self._drop_child_frames()

    def _on_navigated_within_document(self, params, session_id):
        if session_id != self._session:
            return
        if params.get("frameId") != self._main_frame_id:
            return
        self._url = params.get("url", self._url)

    def _on_attached_to_target(self, params, session_id):
        """Attach to a cross-origin iframe target (OOPIF).

        Its document is not part of this session's frame tree, so its
        execution contexts only show up once the child session is enabled.
        """
        if session_id != self._session:
            return
        info = params.get("targetInfo") or {}
        child = params.get("sessionId")
        if info.get("type") != "iframe" or not child:
            return
        frame_id = info.get("targetId")
        with self._state_lock:
            self._sessions[child] = frame_id
        for method in ("Runtime.enable", "Page.enable"):
            try:
                self._send(method, {}, session=child)
            except Error:
                pass
        try:
            self._send(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                session=child,
            )
        except Error:
            pass
        parent_id = None
        url = info.get("url") or ""
        try:
            node = self._send("Page.getFrameTree", session=child)["frameTree"]["frame"]
            parent_id = node.get("parentId")
            url = node.get("url") or url
        except Error:
            pass
        with self._state_lock:
            frame = self._frames.get(frame_id)
            parent = self._frames.get(parent_id) or self._frames.get(self._main_frame_id)
            if frame is None:
                frame = Frame(self, frame_id, parent, url, session=child)
                self._frames[frame_id] = frame
                if parent is not None:
                    parent.child_frames.append(frame)
            else:
                frame._session_id = child
                frame._url = url or frame._url
                if parent is not None and frame.parent_frame is not parent:
                    if frame.parent_frame is not None:
                        try:
                            frame.parent_frame.child_frames.remove(frame)
                        except ValueError:
                            pass
                    frame.parent_frame = parent
                    if frame not in parent.child_frames:
                        parent.child_frames.append(frame)

    def _on_detached_from_target(self, params, session_id):
        if session_id != self._session:
            return
        child = params.get("sessionId")
        if not child:
            return
        with self._state_lock:
            frame_id = self._sessions.pop(child, None)
            frame = self._frames.get(frame_id)
            if frame is not None and frame._session_id == child:
                del self._frames[frame_id]
                if frame.parent_frame is not None:
                    try:
                        frame.parent_frame.child_frames.remove(frame)
                    except ValueError:
                        pass
            for ctx_frame in [
                key for key, value in self._contexts.items() if value[0] == child
            ]:
                del self._contexts[ctx_frame]

    def _on_target_info_changed(self, params, session_id):
        if session_id != self._session:
            return
        info = params.get("targetInfo") or {}
        frame = self._frames.get(info.get("targetId"))
        if frame is not None and info.get("url"):
            frame._url = info["url"]

    def _on_context_created(self, params, session_id):
        if session_id != self._session and not self._is_child_session(session_id):
            return
        context = params.get("context") or {}
        aux = context.get("auxData") or {}
        if not aux.get("isDefault") or not aux.get("frameId"):
            return
        with self._state_lock:
            self._contexts[aux["frameId"]] = (session_id, context.get("id"))
            frame = self._frames.get(aux["frameId"])
            if frame is not None and session_id != self._session:
                frame._session_id = session_id

    def _on_context_destroyed(self, params, session_id):
        if session_id != self._session and not self._is_child_session(session_id):
            return
        with self._state_lock:
            for frame_id, (owner, context_id) in list(self._contexts.items()):
                if owner == session_id and context_id == params.get("executionContextId"):
                    del self._contexts[frame_id]

    def _on_contexts_cleared(self, params, session_id):
        if session_id != self._session and not self._is_child_session(session_id):
            return
        with self._state_lock:
            for frame_id, (owner, _context) in list(self._contexts.items()):
                if owner == session_id:
                    del self._contexts[frame_id]

    def _on_request(self, params, session_id):
        if session_id != self._session:
            return
        payload = params.get("request") or {}
        request = Request(
            payload.get("url", ""),
            payload.get("method", "GET"),
            payload.get("headers"),
            payload.get("postData"),
            params.get("type"),
            (params.get("frame") or {}).get("url"),
        )
        if len(self._requests) >= 2048:
            self._requests.pop(next(iter(self._requests)))
        self._requests[params.get("requestId")] = request
        self._emit("request", request)

    def _on_response(self, params, session_id):
        if session_id != self._session:
            return
        payload = params.get("response") or {}
        request = self._requests.get(params.get("requestId"))
        if request is None:
            request = Request(payload.get("url", ""), "GET", {}, None, params.get("type"))
        response = Response(
            self,
            request,
            params.get("requestId"),
            payload.get("status"),
            payload.get("headers"),
            payload.get("url", "") or request.url,
        )
        self._emit("response", response)

    def _on_request_paused(self, params, session_id):
        if session_id != self._session:
            return
        request_id = params.get("requestId")
        if request_id is None:
            return
        if params.get("responseStatusCode") is not None:
            self._send("Fetch.continueRequest", {"requestId": request_id})
            return
        payload = params.get("request") or {}
        request = Request(
            payload.get("url", ""),
            payload.get("method", "GET"),
            payload.get("headers"),
            payload.get("postData"),
            params.get("resourceType"),
        )
        self._paused.add(request_id)
        for matcher, handler in list(self._routes):
            if matcher(request.url):
                route = Route(self, request_id, request)
                try:
                    handler(route)
                except Exception:  # noqa: BLE001 - handler failure must not hang the request
                    log.exception("route handler failed for %s", request.url)
                    if not route._handled:
                        try:
                            route.abort()
                        except Error:
                            pass
                finally:
                    self._paused.discard(request_id)
                return
        self._paused.discard(request_id)
        self._send("Fetch.continueRequest", {"requestId": request_id})

    # -- routing -----------------------------------------------------------
    def route(self, url, handler):
        self._routes.append((_UrlMatcher(url), handler))
        self._sync_fetch()

    def unroute(self, url=None, handler=None):
        kept = []
        for matcher, registered in self._routes:
            if handler is not None and registered is not handler:
                kept.append((matcher, registered))
                continue
            if url is not None and not matcher.same_spec(url):
                kept.append((matcher, registered))
                continue
        self._routes = kept
        self._sync_fetch()

    def _sync_fetch(self):
        if self._routes:
            patterns = []
            needs_all = False
            for matcher, _handler in self._routes:
                cdp_pattern = matcher.cdp_pattern
                if cdp_pattern is None:
                    needs_all = True
                    break
                patterns.append({"urlPattern": cdp_pattern, "requestStage": "Request"})
            if needs_all or not patterns:
                patterns = [{"urlPattern": "*", "requestStage": "Request"}]
            self._send("Fetch.enable", {"patterns": patterns})
            return
        for request_id in list(self._paused):
            try:
                self._send("Fetch.continueRequest", {"requestId": request_id})
            except Error:
                pass
        self._paused.clear()
        try:
            self._send("Fetch.disable")
        except Error:
            pass

    # -- navigation --------------------------------------------------------
    def goto(self, url, *, wait_until="load", timeout=None):
        if wait_until not in {"commit", "domcontentloaded", "load", "networkidle"}:
            raise ValueError(f"unsupported wait_until: {wait_until}")
        timeout = self.default_navigation_timeout if timeout is None else timeout
        marker_name = {
            "domcontentloaded": "DOMContentLoaded",
            "load": "load",
            "networkidle": "networkIdle",
        }.get(wait_until)
        self._markers.clear()
        self._main_loaders.clear()
        try:
            result = self._send("Page.navigate", {"url": url}, timeout=timeout)
        except TimeoutError:
            raise TimeoutError(
                f"page.goto: navigation to {url} was not committed within {timeout}ms "
                "(Page.navigate did not answer)"
            ) from None
        if result.get("errorText"):
            raise Error(f"page.goto: {result['errorText']} at {url}")
        loader_id = result.get("loaderId")
        if result.get("isDownload"):
            return NavigationResponse(url, loader_id)
        if wait_until != "commit":
            if loader_id or self._main_loaders:
                self._wait_lifecycle(loader_id, marker_name, timeout, url)
            else:
                # Same-document navigation: no new loader, so readyState is the
                # only signal available.
                self._wait_ready_state(wait_until, timeout, url)
            try:
                self._url = self._evaluate_raw(
                    "location.href", return_by_value=True, timeout=timeout
                ) or self._url
            except Error:
                pass
        return NavigationResponse(url, loader_id)

    def _wait_lifecycle(self, loader_id, name, timeout, url):
        """Wait for a lifecycle milestone of this navigation.

        A redirect chain commits several documents; Chrome reports each with
        its own loaderId, so accept the milestone for any main-frame loader
        observed since the call started (the one returned by ``Page.navigate``
        is only the first hop).
        """
        deadline = time.monotonic() + timeout / 1000
        while not self._lifecycle_reached(loader_id, name):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"page.goto: Timeout {timeout}ms exceeded waiting for {name} at {url}; "
                    f"url={self._url}"
                )
            time.sleep(_POLL_S)

    def _lifecycle_reached(self, loader_id, name):
        if loader_id and (loader_id, name) in self._markers:
            return True
        return any((candidate, name) in self._markers for candidate in self._main_loaders)

    def _wait_ready_state(self, wait_until, timeout, url):
        wanted = "complete" if wait_until == "load" else "interactive"
        deadline = time.monotonic() + timeout / 1000
        while True:
            try:
                state = self._evaluate_raw("document.readyState", return_by_value=True)
            except Error:
                state = None
            if state == "complete" or (wanted == "interactive" and state == "interactive"):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"page.goto: Timeout {timeout}ms exceeded waiting for {wait_until} at {url}"
                )
            time.sleep(_POLL_S)

    def wait_for_url(self, url, *, timeout=None, wait_until=None):
        timeout = self.default_navigation_timeout if timeout is None else timeout
        matcher = _UrlMatcher(url) if not isinstance(url, str) else None
        deadline = _deadline(timeout)
        while True:
            current = self.url
            if matcher is not None:
                matched = matcher(current)
            elif url == current:
                matched = True
            else:
                matched = _glob_to_regex(url).match(current) is not None
            if matched:
                if wait_until in {"domcontentloaded", "load", "networkidle"}:
                    self._wait_ready_state(wait_until, timeout, current)
                return
            if _expired(deadline):
                raise TimeoutError(f"page.wait_for_url: Timeout {timeout}ms exceeded, url={current}")
            time.sleep(_POLL_S)

    def wait_for_timeout(self, timeout):
        time.sleep(timeout / 1000)

    # -- evaluation --------------------------------------------------------
    def _evaluate_raw(self, expression, *, return_by_value=True, context_id=None,
                      timeout=None, session=None):
        params = {
            "expression": expression,
            "returnByValue": return_by_value,
            "awaitPromise": True,
            "userGesture": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
        response = self._send(
            "Runtime.evaluate",
            params,
            timeout=self.default_timeout if timeout is None else timeout,
            session=session,
        )
        if response.get("exceptionDetails"):
            raise Error(_exception_text(response))
        result = response.get("result", {})
        if return_by_value:
            if "unserializableValue" in result:
                return _unserializable(result["unserializableValue"])
            return result.get("value")
        return result

    def evaluate(self, expression, arg=None, *, timeout=None):
        return self._evaluate_raw(
            _wrap_expression(expression, arg), return_by_value=True, timeout=timeout
        )

    def query_selector(self, selector):
        return self.locator(selector)._resolve()

    def wait_for_selector(self, selector, *, state="visible", timeout=None):
        timeout = self.default_timeout if timeout is None else timeout
        return self.locator(selector)._wait_for_handle(state, timeout)

    def content(self):
        return self._evaluate_raw(
            "(() => { const d = document.doctype;"
            " const dt = d ? '<!DOCTYPE ' + d.name + '>' : '';"
            " return dt + document.documentElement.outerHTML; })()",
            return_by_value=True,
        )

    def title(self):
        return self._evaluate_raw("document.title", return_by_value=True)

    # -- elements ----------------------------------------------------------
    def locator(self, selector):
        return Locator(self, _Query.css(selector))

    def get_by_role(self, role, *, name=None, exact=False):
        return Locator(self, _Query.role(role, name, exact))

    def get_by_text(self, text, *, exact=False):
        return Locator(self, _Query.text(text, exact))

    def add_init_script(self, script=None, *, path=None):
        """``Page.addScriptToEvaluateOnNewDocument``; returns the script id."""
        if script is None:
            if path is None:
                raise ValueError("add_init_script needs a script or a path")
            with open(path, encoding="utf-8") as handle:
                script = handle.read()
        source = f"({script})()" if _looks_like_function(script) else script
        return self._send("Page.addScriptToEvaluateOnNewDocument", {"source": source}).get(
            "identifier"
        )

    def remove_init_script(self, identifier):
        self._send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})

    # -- screenshot --------------------------------------------------------
    def screenshot(self, path=None, *, clip=None, timeout=None, full_page=False, type="png"):
        """PNG bytes of the page.

        ``clip`` is in viewport CSS pixels, exactly like Playwright (call sites
        feed it ``getBoundingClientRect()`` boxes).  CDP wants document
        coordinates, so the current scroll offset is added and the capture is
        limited to the viewport.  A clip that lies entirely outside the
        viewport raises ``Error``, as Playwright does.
        """
        timeout = self.default_timeout if timeout is None else timeout
        params = {"format": type}
        if clip is not None:
            viewport = self._evaluate_raw(
                "({x: window.scrollX, y: window.scrollY,"
                " width: window.innerWidth, height: window.innerHeight})",
                return_by_value=True,
                timeout=timeout,
            )
            left = max(0.0, float(clip["x"]))
            top = max(0.0, float(clip["y"]))
            right = min(float(viewport["width"]), float(clip["x"]) + float(clip["width"]))
            bottom = min(float(viewport["height"]), float(clip["y"]) + float(clip["height"]))
            if right <= left or bottom <= top:
                raise Error("page.screenshot: clip is outside the viewport")
            params["clip"] = {
                "x": left + float(viewport["x"]),
                "y": top + float(viewport["y"]),
                "width": right - left,
                "height": bottom - top,
                "scale": float(clip.get("scale", 1)),
            }
            params["captureBeyondViewport"] = False
        elif full_page:
            metrics = self._send("Page.getLayoutMetrics")["cssContentSize"]
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": float(metrics["width"]),
                "height": float(metrics["height"]),
                "scale": 1,
            }
            params["captureBeyondViewport"] = True
        else:
            params["captureBeyondViewport"] = False
        result = self._send("Page.captureScreenshot", params, timeout=timeout)
        raw = base64.b64decode(result["data"])
        if path:
            with open(path, "wb") as handle:
                handle.write(raw)
        return raw

    # -- lifecycle ---------------------------------------------------------
    def close(self):
        for method, handler in self._subscriptions:
            self._conn.unsubscribe(method, handler)
        self._browser._pages.pop(self._target_id, None)
        try:
            self._send("Target.closeTarget", {"targetId": self._target_id})
        except Error:
            pass


# --------------------------------------------------------------------------
# browser / context
# --------------------------------------------------------------------------

def _cookie_applies(cookie, url):
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    domain = str(cookie.get("domain", "")).lstrip(".").lower()
    if not domain or not host.endswith(domain):
        return False
    path = cookie.get("path", "/") or "/"
    if not (parts.path or "/").startswith(path):
        return False
    if cookie.get("secure") and parts.scheme != "https":
        return False
    return True


class BrowserContext:
    def __init__(self, browser):
        self._browser = browser

    @property
    def pages(self):
        return self._browser._pages_for_targets(self._browser.page_wait_ms)

    def page(self):
        return self._browser.page(self._browser.page_wait_ms)

    def new_page(self):
        return self._browser.new_page()

    def cookies(self, urls=None):
        data = self._browser._connection.send("Storage.getCookies", {})
        cookies = []
        for cookie in data.get("cookies", []):
            if urls and not any(_cookie_applies(cookie, url) for url in urls):
                continue
            cookies.append(
                {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "domain": cookie["domain"],
                    "path": cookie["path"],
                    "expires": cookie.get("expires", -1),
                    "httpOnly": bool(cookie.get("httpOnly")),
                    "secure": bool(cookie.get("secure")),
                    "sameSite": cookie.get("sameSite", "Lax"),
                }
            )
        return cookies


class Browser:
    """Playwright ``Browser`` counterpart attached over CDP.

    ``page_wait_ms`` is how long ``contexts[0].pages`` waits for Chrome to
    publish its first tab; without it a connect that wins the race against tab
    creation would hand back an empty list.
    """

    def __init__(self, cdp_url, *, timeout=DEFAULT_TIMEOUT / 1000, page_wait_ms=1500):
        info = _browser_info(cdp_url)
        self._info = info
        self._connection = Connection(info["webSocketDebuggerUrl"], timeout=timeout)
        self._pages = {}
        self._requests = {}
        self.page_wait_ms = page_wait_ms
        self._contexts = [BrowserContext(self)]

    @classmethod
    def connect(cls, cdp_url, *, timeout=DEFAULT_TIMEOUT / 1000):
        return cls(cdp_url, timeout=timeout)

    @property
    def version(self):
        return self._info.get("Browser")

    @property
    def contexts(self):
        return self._contexts

    def new_page(self):
        target = self._connection.send("Target.createTarget", {"url": "about:blank"})
        page = Page(self, target["targetId"])
        self._pages[target["targetId"]] = page
        return page

    def _pages_for_targets(self, wait=0):
        """Page targets, creating wrapper objects on demand.

        Chrome can be connected before its first tab exists (``--no-startup-window``
        or a slow profile boot), so a caller that needs a page may wait briefly
        for one instead of indexing an empty list.
        """
        deadline = time.monotonic() + wait / 1000
        while True:
            targets = self._connection.send("Target.getTargets").get("targetInfos", [])
            pages = []
            for target in targets:
                if target.get("type") != "page":
                    continue
                target_id = target["targetId"]
                try:
                    page = self._pages.get(target_id)
                    if page is None:
                        page = Page(self, target_id)
                        self._pages[target_id] = page
                except Error:
                    # The tab can disappear between getTargets and attach;
                    # that is the connect race, not a fatal failure.
                    self._pages.pop(target_id, None)
                    continue
                pages.append(page)
            if pages or wait <= 0 or time.monotonic() >= deadline:
                return pages
            time.sleep(_POLL_S)

    def page(self, wait=DEFAULT_TIMEOUT):
        """First page of the default context, opening one if the browser has none."""
        pages = self._pages_for_targets(wait)
        if pages:
            return pages[0]
        return self.new_page()

    def close(self):
        self._connection.close()


def _browser_info(cdp_url):
    url = cdp_url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        return {"webSocketDebuggerUrl": url}
    with urllib.request.urlopen(f"{url}/json/version", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def connect(cdp_url, *, timeout=DEFAULT_TIMEOUT / 1000):
    """Attach to a running Chrome and return its ``Browser``.

    Mirrors ``playwright.chromium.connect_over_cdp(cdp_url)``; obtain the page
    with ``browser.contexts[0].pages[0]``.
    """
    return Browser.connect(cdp_url, timeout=timeout)
