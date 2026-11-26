"""One page-error type for both engines, so callers stop caring which drove the page.

The login drives Chrome through :mod:`captcha.cdp_page` by default, since the
Playwright driver cost 121 MB of a 600 MB run; the Playwright path stays behind
``INSS_PAGE_ENGINE=playwright``. Both raise their own class, so handlers catch
:data:`PageError` (a tuple) and keep working whichever engine is active.
"""
from playwright.sync_api import Error as PlaywrightError

from .cdp_page import Error as CdpPageError

PageError = (PlaywrightError, CdpPageError)

__all__ = ["PageError", "CdpPageError", "PlaywrightError"]
