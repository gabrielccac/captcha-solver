"""Browser lifecycle: real Chrome (user-data-dir) + CDP wait + cleanup.
No Playwright launch flags -> cleanest fingerprint; control via CDP attach.
No Linux, um Xvfb serve de display; no Windows e no macOS o Chrome usa o display
do próprio usuário, que é onde o login deve acontecer.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

CHROME_PATHS = {
    "nt": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
           r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
           r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "posix": ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"],
}


def _platform():
    return "nt" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "posix")


def _x11():
    """Xvfb só existe no Linux: Windows e macOS desenham na tela do usuário."""
    return _platform() == "posix"


def _log(name):
    return os.path.join(tempfile.gettempdir(), f"{name}.log")


def _registry_chrome():
    """Caminho instalado do Chrome, que o registro conhece mesmo sem PROGRAMFILES no ambiente."""
    if os.name != "nt":
        return None
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                path, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        if path and os.path.isfile(path):
            return path
    return None


def chrome_binary():
    """Executável do Chrome: CHROME_BIN manda, depois o registro e os caminhos do sistema."""
    explicit = os.environ.get("CHROME_BIN")
    if explicit:
        return explicit
    installed = _registry_chrome()
    if installed:
        return installed
    for candidate in CHROME_PATHS[_platform()]:
        path = os.path.expandvars(candidate)
        if os.path.isfile(path):
            return path
    raise RuntimeError("Chrome não encontrado; defina CHROME_BIN com o executável")


def _free_display(start=97):
    d = start
    while os.path.exists(f"/tmp/.X{d}-lock"):
        d += 1
    return d


def _free_port():
    """Porta livre agora, para não disputar uma fixa entre execuções vizinhas."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _port_open(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


class ManagedBrowser:
    def __init__(self, profile_dir, cdp_port=None, display=None, lang="pt-BR",
                 window="1280,900", chrome_bin=None, quiet=False, headless=False):
        self.profile_dir = profile_dir
        self.cdp_port = cdp_port
        self.display = display or _free_display()
        self.lang = lang
        self.window = window
        self.chrome_bin = chrome_bin
        self.procs = []
        self.quiet = quiet
        self.headless = headless

    def _wait_display(self, process, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Xvfb exited during startup")
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.05)
                    probe.connect(f"/tmp/.X11-unix/X{self.display}")
                return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("Xvfb not ready in time")

    def start(self, timeout=40, *, wait_ready=False):
        if self.cdp_port is None:
            self.cdp_port = _free_port()
        if _port_open(self.cdp_port):
            raise RuntimeError("CDP port already in use; choose another --cdp-port")
        os.makedirs(self.profile_dir, exist_ok=True)
        env = dict(os.environ)
        virtual = _x11()
        if virtual:
            env.update(DISPLAY=f":{self.display}", XDG_SESSION_TYPE="x11")
            env.pop("WAYLAND_DISPLAY", None)
            xv = subprocess.Popen(["Xvfb", f":{self.display}", "-screen", "0",
                                   f"{self.window.split(',')[0]}x{self.window.split(',')[1]}x24"],
                                  stdout=open(os.devnull if self.quiet
                                              else _log(f"xvfb{self.display}"), "ab"),
                                  stderr=subprocess.STDOUT)
            self.procs.append(xv)
            if wait_ready:
                self._wait_display(xv)
            else:
                time.sleep(2)
            if xv.poll() is not None:
                self.stop()
                raise RuntimeError("Xvfb exited during startup")
        command = [self.chrome_bin or chrome_binary(), f"--user-data-dir={self.profile_dir}"]
        if self.headless:
            command.append("--headless=new")
        if virtual:
            command.append("--ozone-platform=x11")
        command += [f"--remote-debugging-port={self.cdp_port}",
                    "--no-first-run", "--no-default-browser-check",
                    "--disable-dev-shm-usage", "--renderer-process-limit=4",
                    f"--window-size={self.window}",
                    f"--lang={self.lang}", "about:blank"]
        ch = subprocess.Popen(
            command, env=env,
            stdout=open(os.devnull if self.quiet else _log("cdp-chrome-run"), "ab"),
            stderr=subprocess.STDOUT)
        self.procs.append(ch)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/version",
                                       timeout=3).read()
                return f"http://127.0.0.1:{self.cdp_port}"
            except Exception:
                if ch.poll() is not None:
                    raise RuntimeError("chrome exited during startup")
                time.sleep(0.05 if wait_ready else 1)
        raise RuntimeError("CDP not ready in time")

    def alive(self):
        return _port_open(self.cdp_port)

    def stop(self):
        for pr in reversed(self.procs):
            try:
                pr.terminate()
                try:
                    pr.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pr.kill()
                    pr.wait(timeout=5)
            except Exception:
                pass
        self.procs = []
