"""Download one diagnostic wave and reduce it to a comparable table.

Usage: python diag/collect.py --wave w1 [--out DIR] [--branch BRANCH]
Reads only artifacts produced by .github/workflows/diag-captcha.yml.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = 'gabrielccac/captcha-solver'


def gh(*args):
    result = subprocess.run(['gh', *args], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f'gh {" ".join(args)} failed: {result.stderr.strip()[:300]}')
    return result.stdout


def artifacts(run_id):
    payload = json.loads(gh('api', f'repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=50'))
    return [item['name'] for item in payload.get('artifacts', [])]


def download(run_id, name, target):
    """Extract one artifact into a private staging directory; never share state between collectors."""
    final = target / name
    if any((final / marker).exists() for marker in ('run.json', 'observations.json', 'receipt.json')):
        return
    shutil.rmtree(final, ignore_errors=True)
    gh('run', 'download', str(run_id), '-R', REPO, '-n', name, '-D', str(final))


def read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def summarize(wave, folder, receipt_folder):
    observations = read(folder / 'observations.json') or {}
    run = read(folder / 'run.json') or {}
    receipt = read(receipt_folder / 'receipt.json') or {}
    attempts = observations.get('attempts') or []
    rounds = [record for attempt in attempts for record in attempt.get('rounds') or []]
    confirmations = observations.get('selection_confirmations') or []
    handoff = observations.get('handoff') or []
    pages = observations.get('pages') or []
    model_calls = observations.get('model_calls') or []
    worker_log = folder / 'worker.log'
    return {
        'wave': wave,
        'run_id': run.get('run_id'),
        'mode': run.get('mode'),
        'sha': run.get('sha'),
        'ip': run.get('ip'),
        'org': run.get('org'),
        'asn': run.get('asn'),
        'city': run.get('city'),
        'code': receipt.get('code'),
        'stage': receipt.get('failure_stage'),
        'attempts': (receipt.get('auth_retry') or {}).get('attempts'),
        'stop_reason': (receipt.get('auth_retry') or {}).get('stop_reason'),
        'login_ms': receipt.get('login_ms'),
        'rounds': len(rounds),
        'round_info': [{key: value for key, value in record.get('info', {}).items()
                        if key in ('round', 'why', 'has_ref', 'solve_s', 'click_s', 'verdict_s',
                                   'model_picks', 'solve_error', 'stopped_at', 'state_after')}
                       for record in rounds],
        'model_calls': len(model_calls),
        'picks': [call.get('picks') for call in model_calls],
        'selection_confirmed': [item.get('confirmed') for item in confirmations],
        'handoff': handoff,
        'page_fingerprints': sorted({page.get('structure_fingerprint') for page in pages if page.get('structure_fingerprint')}),
        'page_kinds': [{'kind': page.get('kind'), 'route': page.get('route'), 'status': page.get('http_status'),
                        'rejected': (page.get('public_error_signals') or {}).get('request_rejected')}
                       for page in pages],
        'worker_log_bytes': worker_log.stat().st_size if worker_log.exists() else 0,
        'artifact': str(folder),
        'receipt': str(receipt_folder),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wave', required=True)
    parser.add_argument('--branch', default='diag/captcha-run-lab')
    parser.add_argument('--out', default=None)
    parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args()
    target = Path(args.out or Path.home() / '.capy/work/ripple' / args.wave)
    target.mkdir(parents=True, exist_ok=True)
    listing = json.loads(gh('api', f'repos/{REPO}/actions/runs?branch={args.branch}&per_page={args.limit}'))
    work = target / f'.work-{os.getpid()}'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        for run in listing.get('workflow_runs', []):
            groups = {}
            for name in artifacts(run['id']):
                for prefix in ('diag', 'receipt'):
                    head = f'{prefix}-{args.wave}-'
                    if name.startswith(head):
                        groups.setdefault(name[len(head):], {})[prefix] = name
            for suffix, group in sorted(groups.items()):
                for name in group.values():
                    download(run['id'], name, work)
                diag = next((name for name in group.values() if name.startswith('diag-')), None)
                receipt = next((name for name in group.values() if name.startswith('receipt-')), None)
                if not (diag and receipt):
                    continue
                rows.append(summarize(args.wave, work / diag, work / receipt))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    (target / 'summary.json').write_text(json.dumps(rows, indent=1), encoding='utf-8')
    print(f'{len(rows)} runs	{target / "summary.json"}')
    for row in rows:
        print(f"{str(row['mode']):7s} {str(row['code'] or '-'):26s} ip={str(row['ip']):16s} "
              f"org={str(row['org'] or '-')[:30]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
