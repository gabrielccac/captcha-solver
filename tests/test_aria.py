"""Selection recovery never toggles confirmed cells or changes the challenge."""
from unittest.mock import Mock

import pytest
from PIL import Image

from captcha.aria import AriaInteractions


@pytest.fixture
def rig(monkeypatch):
    from captcha import aria
    clock = [0.0]
    monkeypatch.setattr(aria.time, 'monotonic', lambda: clock[0])
    state = {'signature': 'fixture-challenge',
             'labels': [f'Challenge Image {i}' for i in range(1, 10)],
             'roles': ['button'] * 9, 'ordered': True,
             'states': ['false'] * 9}
    grid = Mock()
    grid.evaluate.side_effect = lambda script: dict(state)
    tasks = [Mock() for _ in range(9)]
    for index, task in enumerate(tasks):
        task.bounding_box.return_value = dict(x=index * 100, y=0, width=100, height=100)
    grid.query_selector_all.return_value = tasks
    page = Mock()
    frame = Mock()
    page.frames = [frame]
    frame.locator.return_value.count.return_value = 1
    frame.locator.return_value.element_handles.return_value = [grid]
    page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
    controller = AriaInteractions(timeout_ms=500)
    assert controller.begin_round(page)
    controller.prepare(Image.new('RGB', (1, 1)), (0, 0, 1, 1), [2, 6, 8])
    clicks = []
    def click(page, x, y):
        index = int(x // 100)
        clicks.append(index + 1)
        state['states'][index] = 'false' if state['states'][index] == 'true' else 'true'
    controller.click = click
    return controller, page, state, clicks, grid


def test_retries_only_missing_cell(rig):
    controller, page, state, clicks, _ = rig
    state['states'][1] = state['states'][7] = 'true'
    assert controller.wait_ready(page, {})
    assert clicks == [6]
    assert controller.metrics[-1]['retried_selection'] == [6]
    assert controller.metrics[-1]['observed_selection'] == [2, 6, 8]
    page.screenshot.assert_not_called()


def test_confirmed_tiles_are_not_clicked_again(rig):
    controller, page, state, clicks, _ = rig
    for index in (2, 6, 8):
        state['states'][index - 1] = 'true'
        assert controller.click_tile(page, index, -100, -100)
    assert controller.wait_ready(page, {})
    assert clicks == []


def test_delayed_selection_waits_without_retry(rig):
    controller, page, state, clicks, _ = rig
    original = page.wait_for_timeout.side_effect
    def wait(ms):
        original(ms)
        for index in (2, 6, 8):
            state['states'][index - 1] = 'true'
    page.wait_for_timeout.side_effect = wait
    assert controller.wait_ready(page, {})
    assert clicks == []


def test_missing_click_is_retried_only_once(rig):
    controller, page, state, clicks, _ = rig
    controller.click = lambda page, x, y: clicks.append(int(x // 100) + 1)
    state['states'][1] = state['states'][7] = 'true'
    assert not controller.wait_ready(page, {})
    assert clicks == [6]
    assert controller.metrics[-1]['failure_phase'] == 'aria_selection_missing'


@pytest.mark.parametrize('fault,reason', [
    ('extra', 'aria_extra_selection'),
    ('changed', 'aria_challenge_changed'),
    ('missing_attribute', 'aria_state_unrecognized'),
    ('detached', 'aria_grid_detached'),
])
def test_ambiguous_or_changed_challenge_never_reclicks(rig, fault, reason):
    controller, page, state, clicks, grid = rig
    if fault == 'extra':
        state['states'][0] = 'true'
    elif fault == 'changed':
        state['signature'] = 'different-challenge'
    elif fault == 'missing_attribute':
        state['states'][0] = None
    else:
        grid.evaluate.side_effect = lambda script: None
    assert not controller.wait_ready(page, {})
    assert clicks == []
    assert controller.metrics[-1]['failure_phase'] == reason


def test_challenge_change_between_model_and_click_stops(rig):
    controller, page, state, clicks, _ = rig
    state['signature'] = 'next-round'
    assert not controller.click_tile(page, 2, 0, 0)
    assert not clicks


def test_late_toggle_during_target_lookup_is_not_undone(rig):
    controller, page, state, clicks, grid = rig
    task = grid.query_selector_all.return_value[5]
    def box():
        state['states'][5] = 'true'
        return dict(x=500, y=0, width=100, height=100)
    task.bounding_box.side_effect = box
    assert controller.click_tile(page, 6, 0, 0)
    assert not clicks


def test_missing_grid_is_explicit(rig):
    controller, page, state, clicks, _ = rig
    page.frames = []
    assert not controller.begin_round(page)
    controller.record_result()
    assert controller.metrics[-1]['failure_phase'] == 'aria_grid_unavailable'


def test_second_round_labels_can_continue_numbering(rig):
    controller, page, state, clicks, _ = rig
    state['labels'] = [f'Challenge Image {i}' for i in range(10, 19)]
    assert controller.begin_round(page)
    controller.prepare(Image.new('RGB', (1, 1)), (0, 0, 1, 1), [2])
    assert controller.click_tile(page, 2, 0, 0)
    assert controller.wait_ready(page, {})
    assert clicks == [2]
    assert controller.metrics[-1]['aria_label_indices'] == list(range(10, 19))


def test_transient_missing_attribute_at_round_start_waits(rig):
    controller, page, state, clicks, _ = rig
    state['states'][0] = None
    original = page.wait_for_timeout.side_effect
    def wait(ms):
        original(ms)
        state['states'][0] = 'false'
    page.wait_for_timeout.side_effect = wait
    assert controller.begin_round(page)
    assert controller._failure is None
    assert not clicks


def test_out_of_order_grid_does_not_reuse_model_positions(rig):
    controller, page, state, clicks, _ = rig
    state['ordered'] = False
    assert not controller.click_tile(page, 2, 0, 0)
    assert not clicks


def test_real_round_calls_aria_before_model_and_before_submit(rig, monkeypatch, tmp_path):
    from captcha import hcaptcha
    controller, page, state, clicks, grid = rig
    monkeypatch.setattr(hcaptcha, 'wait_modal', lambda *a, **kw: [{'x': 0, 'y': 0, 'w': 400, 'h': 600}])
    monkeypatch.setattr(hcaptcha, 'modal_clip', lambda *a: dict(x=0, y=0, width=400, height=600))
    monkeypatch.setattr(hcaptcha, 'find_grid', lambda image: (10, 130, 380, 380))
    monkeypatch.setattr(hcaptcha, 'tiles_ready', lambda *a: True)
    controller.images_ready = lambda *a: True
    def shot(page, path, clip):
        Image.new('RGB', (400, 600), 'gray').save(path)
        return clip
    monkeypatch.setattr(hcaptcha, 'shot', shot)
    def solve(*a, **kw):
        assert controller._signature == 'fixture-challenge'
        return [2, 6, 8]
    monkeypatch.setattr(hcaptcha, 'solve', solve)
    monkeypatch.setattr(hcaptcha, 'vis_modal', lambda *a: [True])
    monkeypatch.setattr(hcaptcha, 'page_state', lambda *a: {'tok': 1, 'done': False})
    tile_click = controller.click
    def click(page, x, y):
        if y == 562:
            assert controller.metrics[-1]['confirmed']
        else:
            tile_click(page, x, y)
    controller.click = click
    done, info = hcaptcha.solve_round(page, str(tmp_path), 0, 'fixture', interactions=controller)
    assert done and info['submitted']
    assert clicks == [2, 6, 8]
