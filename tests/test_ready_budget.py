import pytest

from captcha.hcaptcha import ready_budget


def test_later_rounds_wait_longer(monkeypatch):
    monkeypatch.delenv('INSS_TILE_READY_S', raising=False)
    monkeypatch.delenv('INSS_TILE_READY_STEP_S', raising=False)
    assert [ready_budget(round_number) for round_number in (0, 1, 2)] == [8.0, 12.0, 16.0]


def test_budget_is_configurable_and_ignores_bad_values(monkeypatch):
    monkeypatch.setenv('INSS_TILE_READY_S', '15')
    monkeypatch.setenv('INSS_TILE_READY_STEP_S', '5')
    assert ready_budget(1) == 20.0
    monkeypatch.setenv('INSS_TILE_READY_S', 'nonsense')
    assert ready_budget(0) == 8.0
