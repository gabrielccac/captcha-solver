import io
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from captcha import hcaptcha, solver
from inss import session
from inss import workflow as runner


class CoreTests(unittest.TestCase):
    def test_harvest_waits_for_usable_tokens(self):
        page = Mock(url="https://meu.inss.gov.br/")
        page.evaluate.side_effect = [
            {"auth": "expired", "mi": "old"},
            {"auth": "fresh", "mi": "new"},
        ]
        api = Mock()
        api.get.side_effect = [Mock(status_code=401), Mock(status_code=200)]
        with patch.object(session, "api_session", return_value=(api, {})) as connect:
            self.assertEqual(session.harvest_tokens(page, "12345678901"),
                             {"auth": "fresh", "mi": "new"})
        self.assertEqual(connect.call_count, 2)
        page.wait_for_timeout.assert_called_once_with(1000)

    def test_credit_windows_cover_five_years_without_overlap(self):
        self.assertEqual(session.credit_windows(date(2026, 10, 12)), [
            ("01-11-2025", "12-10-2026"),
            ("01-11-2024", "31-10-2025"),
            ("01-11-2023", "31-10-2024"),
            ("01-11-2022", "31-10-2023"),
            ("01-11-2021", "31-10-2022"),
        ])
        self.assertEqual(session.credit_windows(date(2024, 2, 29), years=2), [
            ("01-03-2023", "29-02-2024"),
            ("01-03-2022", "28-02-2023"),
        ])
        self.assertEqual(session.credit_windows(date(2027, 1, 1), years=1),
                         [("01-02-2026", "01-01-2027")])

    def test_pale_loaded_tiles_are_not_loading_placeholders(self):
        crop = Image.new("RGB", (300, 300), "white")
        grid = (0, 0, 300, 300)
        self.assertFalse(hcaptcha.tiles_ready(crop, grid))
        for row in range(3):
            for col in range(3):
                crop.paste((200, 200, 200), (col * 100, row * 100 + 50,
                                           (col + 1) * 100, row * 100 + 51))
        self.assertTrue(hcaptcha.tiles_ready(crop, grid))

    def test_grid_difference_detects_a_new_grid(self):
        old = Image.new("RGB", (30, 30), "black")
        new = Image.new("RGB", (30, 30), "white")
        self.assertEqual(hcaptcha.grid_difference(old, old, (0, 0, 30, 30)), 0)
        self.assertGreater(hcaptcha.grid_difference(old, new, (0, 0, 30, 30)), 40)

    def test_new_grid_ends_verdict_wait_with_same_frame_count(self):
        old = Image.effect_noise((400, 582), 50).convert("RGB")
        blank = Image.new("RGB", (400, 582), "#f4f4f4")
        new = Image.new("RGB", (400, 582), "white")
        encoded = io.BytesIO()
        new.save(encoded, format="PNG")

        class Page:
            captures = 0

            def evaluate(self, js, *args):
                if js == hcaptcha.VIEWPORT_JS:
                    return {"width": 1280, "height": 582}
                if js == hcaptcha.IFRAME_JS:
                    return [{"x": 0, "y": 0, "w": 400, "h": 600}]
                return {"tok": 0, "done": False, "frames": 2}

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
             patch.object(hcaptcha, "solve", return_value=[1]), \
             patch.object(hcaptcha, "click"):
            done, info = hcaptcha.solve_round(page, out, 0, "test")
        self.assertFalse(done)
        self.assertEqual(page.captures, 2)
        self.assertTrue(info["next_round_ready"])
        self.assertTrue(info["submitted"])

    def test_empty_model_picks_do_not_submit(self):
        ready = Image.effect_noise((400, 582), 50).convert("RGB")

        class Page:
            def evaluate(self, js, *args):
                if js == hcaptcha.VIEWPORT_JS:
                    return {"width": 1280, "height": 582}
                return [{"x": 0, "y": 0, "w": 400, "h": 582}]

            def screenshot(self, path=None, **kwargs):
                ready.save(path)

            def wait_for_timeout(self, ms):
                pass

        with tempfile.TemporaryDirectory() as out, \
             patch.object(hcaptcha, "solve", return_value=[]), \
             patch.object(hcaptcha, "click") as click:
            done, info = hcaptcha.solve_round(Page(), out, 0, "test")
        self.assertFalse(done)
        self.assertEqual(info["why"], "empty-picks")
        click.assert_not_called()

    def test_capture_retry_does_not_repeat_partial_clicks(self):
        page = Mock()
        browser = Mock(contexts=[Mock(pages=[page])])
        with tempfile.TemporaryDirectory() as out, \
             patch.object(sys, "argv", ["runner.py", "--cpf", "12345678901", "--out", out,
                                        "--cdp-url", "http://127.0.0.1:9224"]), \
             patch.dict(os.environ, {"GOVBR_PASSWORD": "test", "OPENROUTER_API_KEY": "test"}), \
             patch.object(runner, "load_dotenv"), \
             patch.object(runner.os, "umask"), \
             patch.object(runner, "ManagedBrowser") as managed_browser, \
             patch.object(runner, "sync_playwright") as playwright, \
             patch.object(runner, "goto_login"), \
             patch.object(runner, "submit_cpf"), \
             patch.object(runner, "vis_modal", return_value=[{}]), \
             patch.object(runner, "find_page", return_value=None), \
             patch.object(runner, "page_state", return_value={"done": False}), \
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
        managed_browser.assert_not_called()
        playwright.return_value.__enter__.return_value.chromium.connect_over_cdp.assert_called_once_with(
            "http://127.0.0.1:9224")
        browser.close.assert_called_once()

    def test_runner_rejects_unauthorized_follow_up_requests(self):
        page = Mock()
        page.evaluate.return_value = True
        browser = Mock(contexts=[Mock(pages=[page])])
        with tempfile.TemporaryDirectory() as out, \
             patch.object(sys, "argv", ["runner.py", "--cpf", "12345678901", "--out", out]), \
             patch.dict(os.environ, {"GOVBR_PASSWORD": "test", "OPENROUTER_API_KEY": "test"}), \
             patch.object(runner, "load_dotenv"), \
             patch.object(runner, "ManagedBrowser"), \
             patch.object(runner, "sync_playwright") as playwright, \
             patch.object(runner, "goto_login"), \
             patch.object(runner, "submit_cpf"), \
             patch.object(runner, "submit_password"), \
             patch.object(runner, "vis_modal", return_value=[{}]), \
             patch.object(runner, "find_page", return_value=None), \
             patch.object(runner, "page_state", return_value={"done": False}), \
             patch.object(runner, "solve_round", return_value=(True, {"round": 0})), \
             patch.object(runner, "harvest_tokens", return_value={"auth": "x", "mi": "y"}), \
             patch.object(runner, "extract", return_value={
                 "userinfo_status": 401,
                 "windows": [{"window": "test", "status": 401}],
             }), \
             patch.object(runner, "log"), \
             patch("builtins.print"):
            playwright.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value = browser
            self.assertEqual(runner.main(), 4)
            self.assertFalse(list(Path(out).glob("extratos-*.json")))

    def test_verdict_waits_for_password_after_frame_disappears(self):
        ready = Image.effect_noise((400, 582), 50).convert("RGB")

        class Page:
            state_calls = 0

            def evaluate(self, js, *args):
                if js == hcaptcha.VIEWPORT_JS:
                    return {"width": 1280, "height": 582}
                if js == hcaptcha.IFRAME_JS:
                    return [] if self.state_calls >= 2 else [{"x": 0, "y": 0, "w": 400, "h": 600}]
                self.assert_success_selector(args)
                self.state_calls += 1
                return {"tok": 0, "done": self.state_calls >= 3,
                        "frames": 2 if self.state_calls == 1 else 0}

            def assert_success_selector(self, args):
                assert args == ('input[type="password"]',)

            def screenshot(self, path=None, **kwargs):
                ready.save(path)

            def wait_for_timeout(self, ms):
                pass

        page = Page()
        with tempfile.TemporaryDirectory() as out, \
             patch.object(hcaptcha, "solve", return_value=[1]), \
             patch.object(hcaptcha, "click"):
            done, info = hcaptcha.solve_round(
                page, out, 0, "test", success_selector='input[type="password"]')
        self.assertTrue(done)
        self.assertTrue(info["state_after"]["done"])
        self.assertEqual(page.state_calls, 3)

    def test_solver_collects_billed_usage(self):
        class Response:
            def close(self):
                pass

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

            def evaluate(self, js, *args):
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
        with patch.object(hcaptcha, "vis_modal", return_value=[moved]):
            clip = hcaptcha.shot(page, "unused.png", {"x": 0, "y": 0,
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

            def close(self):
                pass

        class Session:
            headers = {"User-Agent": "test"}

            def close(self):
                pass

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
