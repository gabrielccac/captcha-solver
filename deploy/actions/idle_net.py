"""Measure what a bare Chrome downloads while it sits on about:blank."""
import sys
import tempfile
import time

sys.path.insert(0, '/app/src')
from captcha.browser import ManagedBrowser  # noqa: E402


def rx_kb():
    with open('/sys/class/net/eth0/statistics/rx_bytes') as handle:
        return int(handle.read()) // 1024


seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 35
start = rx_kb()
with tempfile.TemporaryDirectory(prefix='idle-', ignore_cleanup_errors=True) as profile:
    manager = ManagedBrowser(profile, quiet=True)
    manager.start(wait_ready=True)
    time.sleep(seconds)
    manager.stop()
print(f'idle {seconds}s rx_mb={(rx_kb()-start)/1024:.1f}')
