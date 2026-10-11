#!/usr/bin/env python3
"""Run the CLI in Docker while Chrome stays in the working host environment."""
import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from captcha.browser import ManagedBrowser


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpf", required=True)
    parser.add_argument("--out", required=True)
    args, extra = parser.parse_known_args()
    digits = "".join(c for c in args.cpf if c.isdigit())
    if len(digits) != 11:
        parser.error("CPF must have 11 digits")
    runs = ROOT / "runs"
    out = (ROOT / args.out).resolve()
    if not out.is_relative_to(runs) or out == runs:
        parser.error("--out must be inside runs/")
    if not (ROOT / ".env").is_file():
        parser.error(".env is required for Docker credentials")

    os.umask(0o077)
    runs.mkdir(exist_ok=True)
    default_profile = ROOT / "profiles" / "default"
    owner = hashlib.sha256(digits.encode()).hexdigest()
    marker = default_profile / ".cpf-owner"
    profile = (default_profile if marker.exists() and marker.read_text().strip() == owner
               else ROOT / "profiles" / owner[:16])
    browser = ManagedBrowser(str(profile), cdp_port=9224)
    try:
        browser.start()
        command = [
            "docker", "run", "--rm", "--init", "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", "host", "--env-file", str(ROOT / ".env"),
            "--mount", f"type=bind,src={runs},dst=/app/runs",
            "captcha-inss:local", "--cpf", args.cpf,
            "--out", str(Path("runs") / out.relative_to(runs)), *extra,
            "--cdp-url", "http://127.0.0.1:9224",
        ]
        return subprocess.call(command, cwd=ROOT)
    finally:
        browser.stop()


if __name__ == "__main__":
    sys.exit(main())
