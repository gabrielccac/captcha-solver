import os
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

from browser import ManagedBrowser


class BrowserTests(unittest.TestCase):
    def test_wayland_session_is_not_inherited_by_chrome(self):
        xvfb = Mock()
        xvfb.poll.return_value = None
        chrome = Mock()
        with tempfile.TemporaryDirectory() as profile, \
             patch.dict(os.environ, {"DISPLAY": ":1", "WAYLAND_DISPLAY": "wayland-1",
                                     "XDG_SESSION_TYPE": "wayland"}), \
             patch("browser._port_open", return_value=False), \
             patch("browser.subprocess.Popen", side_effect=[xvfb, chrome]) as spawn, \
             patch("browser.open", mock_open()), \
             patch("browser.time.sleep"), \
             patch("browser.urllib.request.urlopen"):
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
        with patch("browser._port_open", return_value=True), \
             patch("browser.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                ManagedBrowser("unused", display=198).start()
            spawn.assert_not_called()

    def test_failed_xvfb_does_not_launch_chrome(self):
        xvfb = Mock()
        xvfb.poll.return_value = 1
        with tempfile.TemporaryDirectory() as profile, \
             patch("browser._port_open", return_value=False), \
             patch("browser.subprocess.Popen", return_value=xvfb) as spawn, \
             patch("browser.open", mock_open()), \
             patch("browser.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "Xvfb exited"):
                ManagedBrowser(profile, display=198).start()
            spawn.assert_called_once()
            xvfb.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
