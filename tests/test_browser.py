import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

from captcha.browser import ManagedBrowser


class BrowserTests(unittest.TestCase):
    def test_stop_waits_for_chrome_before_stopping_display(self):
        events = []
        chrome, xvfb = Mock(), Mock()
        chrome.terminate.side_effect = lambda: events.append("chrome terminate")
        chrome.wait.side_effect = lambda **kw: events.append("chrome exited")
        xvfb.terminate.side_effect = lambda: events.append("display terminate")
        browser = ManagedBrowser("unused")
        browser.procs = [xvfb, chrome]
        browser.stop()
        self.assertEqual(events, ["chrome terminate", "chrome exited", "display terminate"])
        self.assertEqual(browser.procs, [])

    def test_stop_kills_process_that_does_not_exit(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("chrome", 5), 0]
        browser = ManagedBrowser("unused")
        browser.procs = [process]
        browser.stop()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_wayland_session_is_not_inherited_by_chrome(self):
        xvfb = Mock()
        xvfb.poll.return_value = None
        chrome = Mock()
        with tempfile.TemporaryDirectory() as profile, \
             patch.dict(os.environ, {"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-1",
                                     "XDG_SESSION_TYPE": "wayland"}), \
             patch("captcha.browser._port_open", return_value=False), \
             patch("captcha.browser.subprocess.Popen", side_effect=[xvfb, chrome]) as spawn, \
             patch("captcha.browser.open", mock_open()), \
             patch("captcha.browser.time.sleep"), \
             patch("captcha.browser.urllib.request.urlopen"):
            browser = ManagedBrowser(profile, display=198)
            browser.start()
            args, kwargs = spawn.call_args
            self.assertIn("--ozone-platform=x11", args[0])
            self.assertEqual(kwargs["env"]["DISPLAY"], ":198")
            self.assertEqual(kwargs["env"]["XDG_SESSION_TYPE"], "x11")
            self.assertNotIn("WAYLAND_DISPLAY", kwargs["env"])
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-1")
            browser.stop()

    def test_existing_cdp_listener_is_not_reused(self):
        with patch("captcha.browser._port_open", return_value=True), \
             patch("captcha.browser.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                ManagedBrowser("unused", display=198).start()
            spawn.assert_not_called()

    def test_failed_xvfb_does_not_launch_chrome(self):
        xvfb = Mock()
        xvfb.poll.return_value = 1
        with tempfile.TemporaryDirectory() as profile, \
             patch("captcha.browser._port_open", return_value=False), \
             patch("captcha.browser.subprocess.Popen", return_value=xvfb) as spawn, \
             patch("captcha.browser.open", mock_open()), \
             patch("captcha.browser.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "Xvfb exited"):
                ManagedBrowser(profile, display=198).start()
            spawn.assert_called_once()
            xvfb.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
