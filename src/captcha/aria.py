"""Confirm widget toggles from their accessible state; keep mouse input unchanged."""
import re
import time

from playwright.sync_api import Error as PlaywrightError

from .interactions import DynamicInteractions


SNAPSHOT = """grid => {
  if (!grid.isConnected || !grid.getClientRects().length) return null;
  const tasks = [...grid.querySelectorAll('.task')];
  const boxes = tasks.map(el => el.getBoundingClientRect());
  const prompt = document.querySelector('.prompt-text')?.textContent;
  const reference = [...document.querySelectorAll('.challenge-example .image')]
    .map(el => getComputedStyle(el).backgroundImage);
  return {
    signature: JSON.stringify([prompt, reference, tasks.map(el => [
      el.getAttribute('aria-label'),
      el.querySelector('.image') && getComputedStyle(el.querySelector('.image')).backgroundImage
    ])]),
    labels: tasks.map(el => el.getAttribute('aria-label')),
    roles: tasks.map(el => el.getAttribute('role')),
    ordered: boxes.length === 9 && boxes.every((box, i) =>
      box.width > 0 && box.height > 0 &&
      (i % 3 === 0 || (box.x > boxes[i-1].x && Math.abs(box.y-boxes[i-1].y) < 1)) &&
      (i < 3 || (box.y > boxes[i-3].y && Math.abs(box.x-boxes[i-3].x) < 1))),
    states: tasks.map(el => el.getAttribute('aria-pressed'))
  };
}"""


class AriaInteractions(DynamicInteractions):
    def begin_round(self, page):
        self._grid = None
        self._signature = None
        self._failure = None
        self._retried = []
        self._observed = set()
        self.selected = set()
        self._polls = 0
        self._label_indices = []
        try:
            candidates = []
            for frame in page.frames:
                grids = frame.locator('.task-grid:visible')
                if grids.count():
                    candidates.extend(grids.element_handles())
            if len(candidates) != 1:
                self._failure = 'aria_grid_unavailable'
                return False
            self._grid = candidates[0]
            started = time.monotonic()
            while (time.monotonic() - started) * 1000 < self.timeout_ms:
                state = self._read()
                if state is not None:
                    self._signature = state['signature']
                    self._failure = None
                    return True
                if self._failure != 'aria_state_unrecognized':
                    return False
                page.wait_for_timeout(60)
            return False
        except PlaywrightError:
            self._failure = 'aria_read_failed'
            return False

    def _read(self):
        try:
            state = self._grid.evaluate(SNAPSHOT) if self._grid else None
        except PlaywrightError:
            state = None
        if not state:
            self._failure = 'aria_grid_detached'
            return None
        labels = state.get('labels', [])
        self._label_indices = [int(m.group(1)) if isinstance(label, str)
            and (m := re.fullmatch(r'Challenge Image ([0-9]+)', label)) else None for label in labels]
        # Labels may continue numbering in later rounds; model picks are local 1..9.
        if (len(labels) != 9 or any(not isinstance(label, str) or not label for label in labels)
                or len(set(labels)) != 9 or state.get('roles') != ['button'] * 9
                or state.get('ordered') is not True
                or len(state.get('states', [])) != 9
                or any(value not in ('true', 'false') for value in state['states'])):
            self._failure = 'aria_state_unrecognized'
            return None
        if self._signature is not None and state['signature'] != self._signature:
            self._failure = 'aria_challenge_changed'
            return None
        self._observed = {i + 1 for i, value in enumerate(state['states']) if value == 'true'}
        return state

    def click_tile(self, page, index, x, y):
        self._last_click_sent = False
        if self._read() is None:
            return False
        if index in self._observed:
            return True
        try:
            tasks = self._grid.query_selector_all('.task')
            if len(tasks) != 9:
                self._failure = 'aria_state_unrecognized'
                return False
            box = tasks[index - 1].bounding_box()
            # Recheck immediately before input: a delayed toggle must not be undone.
            if self._read() is None:
                return False
            if index in self._observed:
                return True
            if not box or box['width'] <= 0 or box['height'] <= 0:
                self._failure = 'aria_target_unavailable'
                return False
            self.click(page, box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            self._last_click_sent = True
            return True
        except PlaywrightError:
            self._failure = 'aria_click_failed'
            return False

    def _wait(self, page):
        started = time.monotonic()
        stable_since = None
        while (time.monotonic() - started) * 1000 < self.timeout_ms:
            self._polls += 1
            if self._read() is None:
                return False
            if self._observed - self.selected:
                self._failure = 'aria_extra_selection'
                return False
            if self._observed == self.selected:
                if stable_since is None:
                    stable_since = time.monotonic()
                if ((time.monotonic() - stable_since) * 1000 >= self.stable_ms
                        and (time.monotonic() - started) * 1000 >= 180):
                    return True
            else:
                stable_since = None
            page.wait_for_timeout(60)
        return False

    def wait_ready(self, page, clip):
        started = time.monotonic()
        self._polls = 0
        confirmed = self._wait(page)
        if not confirmed and self._failure is None:
            missing = sorted(self.selected - self._observed)
            for index in missing:
                # Each missing cell gets at most one additional click in this round.
                if self._read() is None:
                    break
                if index in self._observed:
                    continue
                if self._observed - self.selected:
                    self._failure = 'aria_extra_selection'
                    break
                if not self.click_tile(page, index, 0, 0):
                    break
                if self._last_click_sent:
                    self._retried.append(index)
            if self._failure is None:
                confirmed = self._wait(page)
        self.record_result(confirmed, round((time.monotonic() - started) * 1000))
        return confirmed

    def record_result(self, confirmed=False, elapsed_ms=0):
        self.metrics.append(dict(backend='aria_pressed', confirmed=confirmed,
            pre_submit_wait_ms=elapsed_ms, polls=getattr(self, '_polls', 0),
            aria_label_indices=list(self._label_indices),
            expected_selection=sorted(getattr(self, 'selected', set())),
            observed_selection=sorted(self._observed), retried_selection=list(self._retried),
            failure_phase=None if confirmed else self._failure or 'aria_selection_missing'))
