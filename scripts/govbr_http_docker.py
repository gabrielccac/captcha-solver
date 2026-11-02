"""Run the HTTP login in Docker/Xvfb; secrets are never placed in argv or the image."""
import argparse
import os
from pathlib import Path
import subprocess


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--out", type=Path, default=root / "runs/auth-http")
    parser.add_argument("--image", default="captcha-inss-http:local")
    args = parser.parse_args()
    credentials = args.env_file.resolve(strict=True)
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    if uid == 0:
        parser.error("Run this launcher as a non-root user.")
    command = ["docker", "run", "--rm", "--init", "--user", f"{uid}:{gid}",
        "--read-only", "--shm-size", "1g", "--tmpfs", "/tmp:rw,nosuid,nodev,size=2g,mode=1777",
        "--security-opt", f"seccomp={root / 'docker-seccomp.json'}",
        "--mount", f"type=bind,src={credentials},dst=/run/credentials.env,readonly",
        "--mount", f"type=bind,src={output},dst=/out",
        "--entrypoint", "inss-login-http", args.image,
        "--env-file", "/run/credentials.env", "--out", "/out"]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
