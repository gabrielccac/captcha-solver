"""Run one instrumented login with the captcha probe installed.

Used where the Actions harness is not available: mounts the branch source over
the image's copy and writes crops, decisions and timing to INSS_DIAG_DIR.
"""
import os
import runpy
import sys

sys.path.insert(0, '/diag-entry')

import captcha_probe

probe = captcha_probe.install(os.environ['INSS_DIAG_DIR'])
status = 0
try:
    runpy.run_module('inss.http_login', run_name='__main__')
except SystemExit as stop:
    status = stop.code or 0
finally:
    probe.flush()
raise SystemExit(status)
