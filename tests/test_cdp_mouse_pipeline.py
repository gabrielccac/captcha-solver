"""The pointer path is pipelined: events keep their order, only the last waits."""
import time

from captcha.cdp_page import Mouse


class StubPage:
    def __init__(self):
        self.params = []
        self.awaited = []

    def _send(self, method, params):
        self.params.append(params)
        self.awaited.append(True)

    def _send_nowait(self, method, params):
        self.params.append(params)
        self.awaited.append(False)


def test_move_dispatches_every_step_and_awaits_only_the_last():
    page = StubPage()
    Mouse(page).move(300, 300, steps=12)
    moves = [params for params in page.params if params["type"] == "mouseMoved"]
    assert len(moves) == 12
    assert sum(page.awaited) == 1 and page.awaited[-1] is True
    assert (moves[-1]["x"], moves[-1]["y"]) == (300.0, 300.0)
    stamps = [step["timestamp"] for step in moves]
    assert stamps == sorted(stamps)
    assert stamps[-1] <= time.time()


def test_click_awaits_press_and_release():
    page = StubPage()
    Mouse(page).click(10, 20)
    assert [params["type"] for params in page.params] == [
        "mouseMoved", "mousePressed", "mouseReleased"]
    assert all(page.awaited)
