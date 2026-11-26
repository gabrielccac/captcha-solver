"""Report RSS after each import, and what the Playwright driver process costs."""
import os
import subprocess
import sys


def rss(pid=None):
    with open(f'/proc/{pid or os.getpid()}/status') as handle:
        for line in handle:
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) // 1024
    return 0


print('vazio', rss(), 'MB')
import requests  # noqa: E402
print('requests', rss(), 'MB')
from PIL import Image  # noqa: E402
print('PIL', rss(), 'MB')
from playwright.sync_api import sync_playwright  # noqa: E402
print('playwright import', rss(), 'MB')
import inss.http_login  # noqa: E402
print('inss.http_login', rss(), 'MB')
pw = sync_playwright().start()
out = subprocess.run(['ps', '-eo', 'rss=,args='], capture_output=True, text=True).stdout
for line in out.splitlines():
    if 'driver' in line or 'node' in line.lower():
        kb, args = line.strip().split(None, 1)
        print(f'  driver proc {int(kb)//1024} MB: {args[:90]}')
print('python com driver', rss(), 'MB')
img = Image.new('RGB', (1280, 900))
print('imagem 1280x900 RGB', rss(), 'MB')
big = [Image.new('RGBA', (1280, 900)) for _ in range(3)]
print('3 telas RGBA', rss(), 'MB')
pw.stop()
