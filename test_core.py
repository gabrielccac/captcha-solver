import io
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import Mock, patch

from PIL import Image

import runner
import session
import solver


class CoreTests(unittest.TestCase):
    def test_credit_windows_extend_without_changing_old_ranges(self):
        self.assertEqual(session.credit_windows(date(2026, 10, 11)), session.WINDOWS)
        self.assertEqual(session.credit_windows(date(2027, 11, 1))[-2:], [
            ("01-11-2026", "31-10-2027"),
            ("01-11-2027", "31-10-2028"),
        ])

    def test_grid_difference_detects_a_new_grid(self):
        old = Image.new("RGB", (30, 30), "black")
        new = Image.new("RGB", (30, 30), "white")
        self.assertEqual(runner.grid_difference(old, old, (0, 0, 30, 30)), 0)
        self.assertGreater(runner.grid_difference(old, new, (0, 0, 30, 30)), 40)

    def test_new_grid_ends_verdict_wait_with_same_frame_count(self):
        old = Image.effect_noise((400, 582), 50).convert("RGB")
        blank = Image.new("RGB", (400, 582), "#f4f4f4")
        new = Image.new("RGB", (400, 582), "white")
        encoded = io.BytesIO()
        new.save(encoded, format="PNG")

        class Page:
            captures = 0

            def evaluate(self, js):
                if js == runner.VIEWPORT_JS:
                    return {"width": 1280, "height": 582}
                if js == runner.IFRAME_JS:
                    return [{"x": 0, "y": 0, "w": 400, "h": 600}]
                return {"tok": 0, "pwd": False, "frames": 2}

            def screenshot(self, path=None, **kwargs):
                if path:
                    self.captures += 1
                    (blank if self.captures == 1 else old).save(path)
                else:
                    return encoded.getvalue()

            def wait_for_timeout(self, ms):
                pass

        page = Page()
        with tempfile.TemporaryDirectory() as out, \
             patch.object(runner, "solve", return_value=[1]), \
             patch.object(runner, "click"):
            done, info = runner.solve_round(page, out, 0, "test")
        self.assertFalse(done)
        self.assertEqual(page.captures, 2)
        self.assertTrue(info["next_round_ready"])
        self.assertTrue(info["submitted"])

    def test_empty_model_picks_do_not_submit(self):
        ready = Image.effect_noise((400, 582), 50).convert("RGB")

        class Page:
            def evaluate(self, js):
                if js == runner.VIEWPORT_JS:
                    return {"width": 1280, "height": 582}
                return [{"x": 0, "y": 0, "w": 400, "h": 582}]

            def screenshot(self, path=None, **kwargs):
                ready.save(path)

            def wait_for_timeout(self, ms):
                pass

        with tempfile.TemporaryDirectory() as out, \
             patch.object(runner, "solve", return_value=[]), \
             patch.object(runner, "click") as click:
            done, info = runner.solve_round(Page(), out, 0, "test")
        self.assertFalse(done)
        self.assertEqual(info["why"], "empty-picks")
        click.assert_not_called()

    def test_capture_retry_does_not_repeat_partial_clicks(self):
        page = Mock()
        browser = Mock(contexts=[Mock(pages=[page])])
        with tempfile.TemporaryDirectory() as out, \
             patch.object(sys, "argv", ["runner.py", "--cpf", "12345678901", "--out", out]), \
             patch.dict(os.environ, {"GOVBR_PASSWORD": "test", "OPENROUTER_API_KEY": "test"}), \
             patch.object(runner, "load_dotenv"), \
             patch.object(runner.os, "umask"), \
             patch.object(runner, "ManagedBrowser"), \
             patch.object(runner, "sync_playwright") as playwright, \
             patch.object(runner, "goto_login"), \
             patch.object(runner, "submit_cpf"), \
             patch.object(runner, "vis_modal", return_value=[{}]), \
             patch.object(runner, "find_page", return_value=None), \
             patch.object(runner, "page_state", return_value={"pwd": False}), \
             patch.object(runner, "log"), \
             patch("builtins.print"), \
             patch.object(runner, "solve_round", side_effect=[
                 (False, {"why": "shot-fail"}),
                 (False, {"why": "interrupted-clicks"}),
             ]) as solve:
            playwright.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value = browser
            self.assertEqual(runner.main(), 2)
        self.assertEqual(solve.call_count, 2)
        self.assertEqual([call.args[2] for call in solve.call_args_list], [0, 0])
        browser.close.assert_called_once()

    def test_verdict_waits_for_password_after_frame_disappears(self):
        ready = Image.effect_noise((400, 582), 50).convert("RGB")

        class Page:
            state_calls = 0

            def evaluate(self, js):
                if js == runner.VIEWPORT_JS:
                    return {"width": 1280, "height": 582}
                if js == runner.IFRAME_JS:
                    return [] if self.state_calls >= 2 else [{"x": 0, "y": 0, "w": 400, "h": 600}]
                self.state_calls += 1
                return {"tok": 0, "pwd": self.state_calls >= 3,
                        "frames": 2 if self.state_calls == 1 else 0}

            def screenshot(self, path=None, **kwargs):
                ready.save(path)

            def wait_for_timeout(self, ms):
                pass

        page = Page()
        with tempfile.TemporaryDirectory() as out, \
             patch.object(runner, "solve", return_value=[1]), \
             patch.object(runner, "click"):
            done, info = runner.solve_round(page, out, 0, "test")
        self.assertTrue(done)
        self.assertTrue(info["state_after"]["pwd"])
        self.assertEqual(page.state_calls, 3)

    def test_solver_collects_billed_usage(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "[1]"}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                  "cost": 0.001}}

        image = Image.new("RGB", (2, 2), "black")
        usage = {}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test"}), \
             patch.object(solver.requests, "post", return_value=Response()) as post:
            picks = solver.solve(image, [image] * 9, "test", usage=usage)
        self.assertEqual(picks, [1])
        self.assertEqual(usage["cost"], 0.001)
        self.assertTrue(post.call_args.kwargs["json"]["usage"]["include"])

    def test_screenshot_retry_refreshes_clip(self):
        class Page:
            calls = 0

            def evaluate(self, js):
                return {"width": 1280, "height": 582}

            def screenshot(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("moved")
                self.clip = kwargs["clip"]

            def wait_for_timeout(self, ms):
                pass

        page = Page()
        moved = {"x": 15, "y": 20, "w": 400, "h": 582}
        with patch.object(runner, "vis_modal", return_value=[moved]):
            clip = runner.shot(page, "unused.png", {"x": 0, "y": 0,
                                                     "width": 400, "height": 582})
        self.assertEqual(page.calls, 2)
        self.assertEqual(clip, page.clip)
        self.assertEqual(clip["x"], 15)
        self.assertEqual(clip["height"], 562)

    def test_parallel_extraction_keeps_window_order_and_deduplication(self):
        class Response:
            status_code = 200

            def __init__(self, body=None):
                self.body = body or {}

            def json(self):
                return self.body

        class Session:
            headers = {"User-Agent": "test"}

            def get(self, *args, **kwargs):
                return Response()

        def get(url, **kwargs):
            first = "/01-01-2026/" in url
            return Response({"creditosTO": [
                {"idCredito": 1 if first else 3},
                {"idCredito": 2, "from": "first" if first else "second"},
            ], "listaNbHiscreTO": ["first" if first else "second"]})

        windows = [("01-01-2026", "31-01-2026"),
                   ("01-02-2026", "28-02-2026")]
        with patch.object(session, "api_session", return_value=(Session(), {})), \
             patch.object(session.requests, "get", side_effect=get):
            out = session.extract("12345678901", "auth", "mi", windows)
        self.assertEqual([w["window"] for w in out["windows"]],
                         ["/".join(w) for w in windows])
        self.assertEqual([c["idCredito"] for c in out["creditos"]], [1, 2, 3])
        self.assertEqual(out["creditos"][1]["from"], "second")


if __name__ == "__main__":
    unittest.main()
