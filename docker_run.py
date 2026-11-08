#!/usr/bin/env python3
"""Run Chrome, Xvfb, and the complete workflow inside Docker."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


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
    if any(arg.split("=", 1)[0] in {"--cdp-url", "--profile-dir"} for arg in extra):
        parser.error("Docker manages its own browser and temporary profile")
    command = [
        "docker", "run", "--rm", "--init",
        "--user", f"{os.getuid() if hasattr(os, 'getuid') else 1000}:{os.getgid() if hasattr(os, 'getgid') else 1000}",
        "--security-opt", f"seccomp={ROOT / 'docker-seccomp.json'}", "--shm-size", "1g",
        "--env-file", str(ROOT / ".env"),
        "--mount", f"type=bind,src={runs},dst=/app/runs",
        "captcha-inss:local", "--cpf", args.cpf,
        "--out", str(Path("runs") / out.relative_to(runs)),
        "--profile-dir", "/tmp/chrome-profile", *extra,
    ]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
