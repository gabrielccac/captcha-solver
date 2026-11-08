"""Four sequential new profiles, same runner/image, ABBA; only ciphertext artifacts."""
import json
import os
from pathlib import Path

import query_run


def main():
    names = (*query_run.KEYS, 'GHCR_TOKEN', 'QUERY_RECIPIENT_CERT')
    private = {key: os.environ[key] for key in names}
    output = Path('comparison-results')
    output.mkdir(exist_ok=False)
    results = []
    try:
        for index, mode in enumerate(('screenshot', 'network', 'network', 'screenshot'), 1):
            os.environ.update(private, QUERY_WORKER_FILE='compare_worker.py', COMPARE_MODE=mode)
            query_run.main()
            trial = output / ('trial-' + str(index))
            Path('encrypted-query').rename(trial)
            receipt = json.loads((trial / 'receipt.json').read_text())
            results.append(dict(index=index, mode=mode, **receipt))
            print('TRIAL_RESULT ' + json.dumps(results[-1]), flush=True)
            if receipt.get('code') in ('invalid_credentials', 'two_factor_required', 'rate_limited'):
                break
    finally:
        private.clear()
        for key in names:
            os.environ.pop(key, None)
        (output / 'summary.json').write_text(json.dumps(results, indent=2))
    return 0 if len(results) == 4 and all(r['encrypted_result'] for r in results) else 3


if __name__ == '__main__':
    raise SystemExit(main())
