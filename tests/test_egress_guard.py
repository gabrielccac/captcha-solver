import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1] / 'deploy' / 'actions'
spec = importlib.util.spec_from_file_location('query_run_egress', ROOT / 'query_run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize('org,blocked', [
    ('AS8075 Microsoft Corporation', True),
    ('AS14618 Amazon.com, Inc.', False),
    ('AS16509 Amazon.com, Inc.', False),
    ('', False),
])
def test_only_configured_asns_count_as_blocked_egress(monkeypatch, org, blocked):
    monkeypatch.delenv('INSS_BLOCKED_ASN', raising=False)
    assert runner.blocked_egress({'org': org}) == (blocked, ['AS8075'])


def test_blocked_asn_service_names_are_ignored(monkeypatch):
    monkeypatch.setenv('INSS_BLOCKED_ASN', ' as16509 , ')
    assert runner.blocked_egress({'org': 'AS16509 Amazon.com, Inc.'}) == (True, ['AS16509'])
    assert runner.blocked_egress({'org': 'AS8075 Microsoft Corporation'}) == (False, ['AS16509'])


def test_egress_lookup_fails_open(monkeypatch):
    class Failing:
        def __init__(self, *args, **kwargs):
            raise OSError('no network')

    monkeypatch.setattr(runner, 'urlopen', Failing)
    identity = runner.egress()
    assert identity['ip'] is None and identity['error'] == 'OSError'
    assert runner.blocked_egress(identity) == (False, ['AS8075'])
