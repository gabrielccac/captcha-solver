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


def test_reset_notice_is_ignored_when_the_frame_is_cross_origin():
    """hCaptcha's challenge frame is cross-origin; reading it must never raise."""
    from captcha.hcaptcha import reset_notice

    class Frame:
        def evaluate(self, js):
            raise RuntimeError("Sandbox access violation")

    assert reset_notice(Frame()) is False


def test_reset_words_cover_both_locales():
    from captcha.hcaptcha import RESET_JS

    for phrase in ("tente novamente", "try again", "algo deu errado"):
        assert phrase.split()[0] in RESET_JS.lower()
