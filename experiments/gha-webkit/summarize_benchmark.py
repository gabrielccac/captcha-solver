"""Summarize only completed cold-runner measurements, retaining every raw sample."""
import argparse
import json
from pathlib import Path
import statistics


def summarize(root):
    rows = [json.loads(path.read_text()) for path in root.rglob('image-benchmark-result.json')]
    assert len(rows) == 20 and all(r['success'] and r['cold_image'] for r in rows)
    assert len({r['runtime_fingerprint']['sha256'] for r in rows}) == 1
    groups = {}
    for variant in ('baseline', 'clean', 'zstd', 'repack'):
        samples = sorted((r for r in rows if r['variant'] == variant), key=lambda r: r['trial'])
        assert [r['trial'] for r in samples] == [1, 2, 3, 4, 5]
        groups[variant] = dict(samples=samples)
        for metric in ('pull_seconds', 'tests_seconds', 'preflight_seconds', 'ready_seconds'):
            values = [r[metric] for r in samples]
            groups[variant][metric] = dict(median=round(statistics.median(values), 4),
                                          minimum=min(values), maximum=max(values))
    baseline = groups['baseline']['ready_seconds']['median']
    for group in groups.values():
        group['ready_median_reduction_percent'] = round(100 * (1 - group['ready_seconds']['median'] / baseline), 1)
    return dict(samples_per_variant=5, variants=groups,
                readiness_definition='docker pull + 59 offline authentication tests + WebKit offline preflight',
                limitations='Five fresh runners per variant; network and CPU variation remain. No login credentials in this benchmark.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('artifacts', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.artifacts)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: {m: v[m] for m in ('pull_seconds', 'ready_seconds', 'ready_median_reduction_percent')}
                      for k, v in result['variants'].items()}, indent=2))
