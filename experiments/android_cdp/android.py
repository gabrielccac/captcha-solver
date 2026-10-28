"""Gate one native Android login on a synthetic CDP check, with fresh profiles."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen


def adb(*args, check=True):
    result = subprocess.run(["adb", "-s", "emulator-5554", *args], capture_output=True,
                            text=True, timeout=30, check=check)
    return result.stdout.strip()


def main():
    out = Path("runs/android-cdp-login")
    out.mkdir(parents=True, exist_ok=False)
    facts = {"android_api": adb("shell", "getprop", "ro.build.version.sdk"),
             "emulator": adb("shell", "getprop", "ro.kernel.qemu") == "1",
             "chrome": re.search(r"versionName=(\S+)", adb("shell", "dumpsys", "package", "com.android.chrome")).group(1)}
    assert facts == {"android_api":"36", "emulator":True, "chrome":"133.0.6943.137"}
    (out / "android.json").write_text(json.dumps(facts, indent=2) + "\n")
    adb("root")
    adb("wait-for-device")
    adb("shell", "settings", "put", "global", "auto_time_zone", "0")
    adb("shell", "setprop", "persist.sys.timezone", "America/Sao_Paulo")
    try:
        for case in ("smoke", "login"):
            assert adb("shell", "pm", "clear", "com.android.chrome") == "Success"
            adb("shell", "am", "set-debug-app", "--persistent", "com.android.chrome")
            adb("shell", 'echo "chrome --disable-fre --no-default-browser-check --no-first-run" > /data/local/tmp/chrome-command-line')
            adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
            adb("shell", "am", "start", "-W", "-n", "com.android.chrome/com.google.android.apps.chrome.Main",
                "-a", "android.intent.action.VIEW", "-d", "about:blank")
            adb("forward", "tcp:9229", "localabstract:chrome_devtools_remote")
            for attempt in range(60):
                try:
                    with urlopen("http://127.0.0.1:9229/json/version", timeout=2) as response:
                        assert "Android" in json.load(response).get("User-Agent", "")
                    break
                except Exception:
                    if attempt == 59:
                        raise
                    time.sleep(.5)
            command = [sys.executable, "-m", "experiments.android_cdp." + case,
                       "--output", str(out / (case + ".json"))]
            if case == "smoke":
                command += ["--page-host", "10.0.2.2", "--require-android"]
            result = subprocess.run(command, timeout=180)
            adb("shell", "am", "force-stop", "com.android.chrome")
            if result.returncode:
                return result.returncode
        return 0
    finally:
        adb("forward", "--remove", "tcp:9229", check=False)
        adb("shell", "am", "force-stop", "com.android.chrome", check=False)
        adb("shell", "am", "clear-debug-app", check=False)


if __name__ == "__main__":
    raise SystemExit(main())
