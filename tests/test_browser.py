import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, mock_open, patch

from captcha.browser import ManagedBrowser, chrome_binary


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
             patch("captcha.browser._x11", return_value=True), \
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
             patch("captcha.browser._x11", return_value=True), \
             patch("captcha.browser.subprocess.Popen", return_value=xvfb) as spawn, \
             patch("captcha.browser.open", mock_open()), \
             patch("captcha.browser.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "Xvfb exited"):
                ManagedBrowser(profile, display=198).start()
            spawn.assert_called_once()
            xvfb.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class NoDisplayTests(unittest.TestCase):
    def test_chrome_runs_on_the_user_display_without_xvfb(self):
        chrome = Mock()
        with (
            tempfile.TemporaryDirectory() as profile,
            patch("captcha.browser._port_open", return_value=False),
            patch("captcha.browser._x11", return_value=False),
            patch("captcha.browser.chrome_binary", return_value="/opt/chrome"),
            patch("captcha.browser.subprocess.Popen", return_value=chrome) as spawn,
            patch("captcha.browser.open", mock_open()),
            patch("captcha.browser.urllib.request.urlopen"),
        ):
            browser = ManagedBrowser(profile, display=198)
            browser.start()
            command, kwargs = spawn.call_args
        spawn.assert_called_once()
        self.assertEqual(command[0][0], "/opt/chrome")
        self.assertNotIn("--ozone-platform=x11", command[0])
        self.assertNotEqual(kwargs["env"].get("DISPLAY"), ":198")
        self.assertEqual(browser.procs, [chrome])

    def test_chrome_binary_prefers_the_environment_override(self):
        with patch.dict(os.environ, {"CHROME_BIN": "/custom/chrome"}):
            self.assertEqual(chrome_binary(), "/custom/chrome")

    def test_chrome_binary_scans_the_system_paths_of_the_platform(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("captcha.browser._platform", return_value="nt"),
            patch("captcha.browser.os.path.isfile",
                  side_effect=lambda path: "Chrome" in path and path.endswith(".exe")),
        ):
            self.assertTrue(chrome_binary().endswith("chrome.exe"))

    def test_each_run_takes_its_own_port_instead_of_a_fixed_one(self):
        chrome = Mock()
        with (
            tempfile.TemporaryDirectory() as profile,
            patch("captcha.browser._port_open", return_value=False),
            patch("captcha.browser._x11", return_value=False),
            patch("captcha.browser.chrome_binary", return_value="/opt/chrome"),
            patch("captcha.browser._free_port", side_effect=[45123, 45124]),
            patch("captcha.browser.subprocess.Popen", return_value=chrome) as spawn,
            patch("captcha.browser.open", mock_open()),
            patch("captcha.browser.urllib.request.urlopen"),
        ):
            primeiro = ManagedBrowser(profile)
            primeiro.start()
            primeiro.stop()
            segundo = ManagedBrowser(profile)
            segundo.start()
            segundo.stop()
        portas = [chamada[0][0][2] for chamada in spawn.call_args_list]
        self.assertEqual(portas, ["--remote-debugging-port=45123", "--remote-debugging-port=45124"])
        self.assertEqual((primeiro.cdp_port, segundo.cdp_port), (45123, 45124))

    def test_missing_chrome_reports_a_clear_error(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("captcha.browser._registry_chrome", return_value=None),
            patch("captcha.browser.os.path.isfile", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "CHROME_BIN"):
                chrome_binary()

    def test_registry_install_is_used_when_the_environment_is_filtered(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("captcha.browser._registry_chrome",
                  return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ):
            self.assertTrue(chrome_binary().endswith("chrome.exe"))

    def test_known_windows_path_works_without_programfiles_in_the_environment(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("captcha.browser._platform", return_value="nt"),
            patch("captcha.browser._registry_chrome", return_value=None),
            patch("captcha.browser.os.path.isfile",
                  side_effect=lambda path: path == r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        ):
            self.assertEqual(chrome_binary(), r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def test_dynamic_display_waits_for_socket_not_fixed_delay(monkeypatch):
    from captcha import browser as module
    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock(return_value=False)
    probe.connect.side_effect = [ConnectionRefusedError(), None]
    monkeypatch.setattr(module.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(module.socket, "socket", lambda *a: probe)
    sleep = Mock()
    monkeypatch.setattr(module.time, "sleep", sleep)
    process = Mock()
    process.poll.return_value = None
    ManagedBrowser("unused", display=198)._wait_display(process)
    probe.connect.assert_called_with("/tmp/.X11-unix/X198")
    sleep.assert_called_once_with(0.02)


def test_dynamic_display_fails_if_xvfb_exits():
    import pytest
    process = Mock()
    process.poll.return_value = 1
    with pytest.raises(RuntimeError, match="Xvfb exited"):
        ManagedBrowser("unused", display=198)._wait_display(process)


def test_dynamic_display_timeout_is_bounded():
    import pytest
    with pytest.raises(RuntimeError, match="not ready"):
        ManagedBrowser("unused", display=198)._wait_display(Mock(), timeout=0)
