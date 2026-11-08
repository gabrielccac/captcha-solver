"""Send environment secrets to disposable worker stdin, then publish safe metrics."""
import json
import os
import re
from pathlib import Path
import subprocess
import threading

keys = ('GOVBR_CPF','GOVBR_PASSWORD','OPENROUTER_API_KEY')
image = Path('experiments/gha-webkit/image.ref').read_text().strip()
assert re.fullmatch(r'ghcr\.io/gabrielccac/captcha-solver-webkit-smoke@sha256:[0-9a-f]{64}',image)
values = {key:os.environ.pop(key) for key in keys}
assert all(values.values())
name = 'inss-webkit-smoke'
command = ['docker','run','--rm','--init','-i','--name',name,'--log-driver','none',
    '--read-only','--cpus','1','--memory','1400m','--memory-swap','1400m','--pids-limit','320',
    '--shm-size','128m','--tmpfs','/tmp:rw,nosuid,size=512m,mode=1777',
    '--security-opt','seccomp=experiments/gha-webkit/seccomp.json',image]
# No secrets in Docker arguments, environment, logs, output files or Actions inputs.
process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
    text=True,encoding='utf-8')
timer = threading.Timer(240,lambda:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
timer.start()
report = {'success':False,'code':'container_failed_without_receipt'}
try:
    output,_ = process.communicate(json.dumps(values)+'\n',timeout=260)
    assert not any(value and value in output for value in values.values()), 'unsafe_worker_output'
    report = json.loads(output)
    Path('webkit-smoke-result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('SMOKE_RESULT '+json.dumps(report),flush=True)
except Exception as error:
    report = {'success':False,'code':'container_wrapper_error','error_type':type(error).__name__}
    print('SMOKE_RESULT '+json.dumps(report),flush=True)
finally:
    timer.cancel()
    values.clear()
    subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
raise SystemExit(0 if report.get('success') and process.returncode==0 else 3)
