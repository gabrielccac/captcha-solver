"""Roda o fluxo do CadÚnico em Docker com os passos aparecendo ao vivo.

As credenciais vão por entrada padrão, nunca em argv nem na imagem. O progresso
sai em stderr, uma linha por etapa, então basta acompanhar o terminal.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpf", default="", help="CPF; sem ele, usa GOVBR_CPF do --env-file")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--out", default="runs/dataprev-docker", help="Destino, dentro de runs/")
    parser.add_argument("--document", choices=("comprovante", "consulta", "all"), default="all")
    parser.add_argument("--image", default="captcha-dataprev:local")
    parser.add_argument("--rebuild", action="store_true", help="Reconstrói a imagem antes de rodar")
    args = parser.parse_args()

    values = {}
    env_file = args.env_file.resolve()
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()

    cpf = re.sub(r"\D", "", args.cpf or values.get("GOVBR_CPF", ""))
    if len(cpf) != 11:
        parser.error("informe --cpf ou GOVBR_CPF com 11 dígitos no --env-file")
    for key in ("GOVBR_PASSWORD", "OPENROUTER_API_KEY"):
        if not values.get(key):
            parser.error(f"{key} ausente no {env_file}")

    runs = root / "runs"
    destino = (root / args.out).resolve()
    if not destino.is_relative_to(runs) or destino == runs:
        parser.error("--out precisa ficar dentro de runs/")
    runs.mkdir(exist_ok=True)

    if args.rebuild:
        print("[docker] reconstruindo a imagem", flush=True)
        subprocess.run(["docker", "build", "-t", args.image, "."], cwd=root, check=True)

    segredo = json.dumps({"cpf": cpf, "password": values["GOVBR_PASSWORD"],
                          "api_key": values["OPENROUTER_API_KEY"]}) + "\n"
    comando = [
        "docker", "run", "--rm", "-i", "--init",
        "--user", f"{os.getuid() if hasattr(os, 'getuid') else 1000}:"
                  f"{os.getgid() if hasattr(os, 'getgid') else 1000}",
        "--security-opt", f"seccomp={root / 'docker-seccomp.json'}",
        "--shm-size", "1g",
        "--mount", f"type=bind,src={runs},dst=/app/runs",
        "--entrypoint", "python", args.image,
        "-m", "dataprev.flow", "--credentials-stdin",
        "--out", str(Path("runs") / destino.relative_to(runs)), "--document", args.document,
    ]
    try:
        return subprocess.run(comando, input=segredo, text=True, cwd=root).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
