"""The observation layer preserves decisions and exports challenge-only pixels."""
import importlib.util
import json
import io
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

spec = importlib.util.spec_from_file_location('captcha_probe',
    Path(__file__).resolve().parents[1] / 'experiments/gha-webkit/captcha_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_probe_keeps_original_detection_and_crops_outside_pixels(tmp_path):
    probe.rounds.clear()
    probe.models.clear()
    before = Image.new('RGB', (400, 600), 'red')
    grid = (10, 130, 380, 380)
    before.paste('white', (10, 130, 390, 510))
    controller = probe.interactions.DynamicInteractions()
    probe.prepare(controller, before, grid, [1, 3])
    for _ in range(20):
        assert probe.marks(before, before, grid) == probe.original_marks(before, before, grid)
    metadata = probe.save(tmp_path)
    assert len(metadata['rounds'][0]['frames']) == 16
    for path in tmp_path.glob('*.png'):
        image = Image.open(path)
        assert image.size == (380, 380)
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_model_probe_preserves_answer_without_recording_credentials(tmp_path, monkeypatch):
    probe.rounds.clear()
    probe.models.clear()
    original = Mock(return_value=[1, 3])
    monkeypatch.setattr(probe, 'original_model', original)
    image = Image.new('RGB', (64, 64), 'blue')
    assert probe.model(image, [image] * 3, 'fixture-model', api_key='private-key-fixture') == [1, 3]
    original.assert_called_once()
    metadata = probe.save(tmp_path)
    assert metadata['model_calls'][0]['selected'] == [1, 3]
    assert 'private-key-fixture' not in json.dumps(metadata)


def test_late_frames_never_override_failure_or_click_again(monkeypatch):
    probe.rounds.clear()
    before = Image.new('RGB', (400, 600), 'white')
    controller = probe.interactions.DynamicInteractions()
    probe.prepare(controller, before, (10, 130, 380, 380), [1])
    raw = io.BytesIO()
    before.save(raw, format='PNG')
    page = Mock()
    page.screenshot.return_value = raw.getvalue()
    monkeypatch.setattr(probe, 'original_wait', Mock(return_value=False))
    assert not probe.wait_ready(controller, page, {})
    assert len(probe.rounds[-1]['frames']) == 3
    assert all(frame['late'] for frame in probe.rounds[-1]['frames'])
    page.mouse.click.assert_not_called()
