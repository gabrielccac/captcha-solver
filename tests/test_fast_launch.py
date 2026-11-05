"""Launcher trust boundaries, without network or real credentials."""
import importlib.util
import json
import hashlib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('fast_launch',
    Path(__file__).resolve().parents[1] / 'experiments/gha-webkit/fast_run.py')
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


def test_changed_launch_bundle_is_rejected(tmp_path):
    for name in launch.BUNDLE_HASHES:
        (tmp_path / name).write_text('changed')
    with pytest.raises(ValueError, match='unapproved_launch_bundle'):
        launch.verify_bundle(tmp_path)


def test_launch_bundle_and_digest_match_reviewed_repository():
    root = Path(__file__).resolve().parents[1] / 'experiments/gha-webkit'
    assert (root / 'image.ref').read_text().strip() == launch.IMAGE
    for name, expected in launch.BUNDLE_HASHES.items():
        assert hashlib.sha256((root / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() == expected


def test_credential_in_worker_output_is_rejected():
    with pytest.raises(ValueError, match='unsafe_worker_output'):
        launch.worker_result('SMOKE_RESULT ' + json.dumps({'value': 'private-fixture'}), ['private-fixture'])


def test_success_requires_an_authenticated_endpoint_and_both_tokens():
    with pytest.raises(ValueError, match='unverified_worker_success'):
        launch.worker_result('SMOKE_RESULT {"success":true}', [])
    expected = dict(success=True, independent_api_http_status=200, access_token_present=True, mi_token_present=True)
    assert launch.worker_result('SMOKE_RESULT ' + json.dumps(expected), []) == expected
