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


def google_providers_ready(dump):
    ready = set()
    for record in re.split(r"\n\s*\* ContentProviderRecord", dump):
        if not re.search(r"\bprovider=(?!null\b)\S+", record):
            continue
        component = record.splitlines()[0]
        authority = re.search(r"(?m)^\s*authority=(.+)$", record)
        authorities = authority.group(1).strip().split(";") if authority else []
        for name in ("AccountContentProvider", "com.google.android.gms.phenotype", "com.google.android.chimera"):
            if (name == "AccountContentProvider" and name in component) or name in authorities:
                ready.add(name)
    return len(ready) == 3


def wait_for_google_providers(out):
    # boot_completed does not mean GMS has published the providers Chrome needs.
    start = time.monotonic()
    stable_pid = None
    samples = 0
    while time.monotonic() - start < 180:
        dump = adb("shell", "dumpsys", "activity", "-a", "providers", "com.google.android.gms")
        pid = adb("shell", "pidof", "com.google.android.gms.persistent", check=False)
        if pid and google_providers_ready(dump):
            samples = samples + 1 if pid == stable_pid else 1
            stable_pid = pid
            if samples >= 3:
                result = {"google_providers_ready": True, "stable_samples": samples,
                          "elapsed_s": round(time.monotonic() - start, 2)}
                (out / "google-services.json").write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result), flush=True)
                return
        else:
            stable_pid, samples = None, 0
        time.sleep(3)
    (out / "google-services-failure.txt").write_text(dump)
    raise TimeoutError("Google providers not published stably before Chrome launch")


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
    wait_for_google_providers(out)
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
            if case == "smoke" and result.returncode:
                # No credentials have been used at this gate; preserve crash evidence.
                diagnostics = {}
                for key, args in {
                    "chrome_exit_info": ("shell", "dumpsys", "activity", "exit-info", "com.android.chrome"),
                    "chrome_process": ("shell", "pidof", "com.android.chrome"),
                    "guest_memory": ("shell", "cat", "/proc/meminfo"),
                    "crash_log": ("logcat", "-b", "crash", "-d", "-t", "200"),
                    "system_log": ("logcat", "-b", "system", "-d", "-t", "300"),
                }.items():
                    try:
                        diagnostics[key] = adb(*args, check=False)
                    except (subprocess.SubprocessError, OSError) as error:
                        diagnostics[key] = type(error).__name__
                (out / "preflight-failure.json").write_text(json.dumps(diagnostics, indent=2) + "\n")
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
