#!/usr/bin/env bash
# Measure the login outcome from this host's egress.
# Usage: ./scripts/egress_probe.sh [runs]   (needs Docker and a .env with the GOV.BR test account)
set -uo pipefail

runs="${1:-10}"
tag="${IMAGE_TAG:-meu-inss-probe:local}"
envfile="${ENV_FILE:-.env}"
out="${OUT_DIR:-probe-runs}"

if [ ! -f "$envfile" ]; then
  echo "missing $envfile with GOVBR_CPF, GOVBR_PASSWORD and OPENROUTER_API_KEY" >&2
  exit 2
fi

echo "egress: $(curl -fsS --max-time 8 https://ipinfo.io/json 2>/dev/null | tr -d '\n' || echo unknown)"
echo "image:  $tag"
docker build -q -t "$tag" . || exit 2
mkdir -p "$out"

for i in $(seq 1 "$runs"); do
  docker run --rm --init --user "$(id -u):$(id -g)" \
    --security-opt "seccomp=$PWD/docker-seccomp.json" --shm-size 256m \
    --tmpfs /tmp:rw,nosuid,size=1g,mode=1777 \
    --mount "type=bind,src=$PWD/$envfile,dst=/run/credentials.env,readonly" \
    --mount "type=bind,src=$PWD/$out,dst=/out" \
    --entrypoint inss-login-http "$tag" \
    --env-file /run/credentials.env --out "/out/run-$i" --fast-interactions --batch-captcha >/dev/null 2>&1
  echo "run $i: exit=$?"
done

python3 - "$out" <<'PY'
import collections
import glob
import json
import os
import sys

rows = []
for path in sorted(glob.glob(os.path.join(sys.argv[1], 'run-*', 'receipt.json'))):
    try:
        rows.append(json.load(open(path, encoding='utf-8')))
    except ValueError:
        pass
if not rows:
    raise SystemExit('no receipt written; check the image build and the credential file')
codes = collections.Counter(row.get('code') for row in rows)
ok = codes.get('ok', 0)
print(f'{len(rows)} runs, {ok} authenticated ({ok / len(rows) * 100:.0f}%)')
for code, count in codes.most_common():
    print(f'  {count:3d}  {code}')
stages = collections.Counter(row.get('failure_stage') for row in rows if row.get('code') != 'ok')
for stage, count in stages.most_common():
    print(f'  stage {count:3d}  {stage}')
PY
