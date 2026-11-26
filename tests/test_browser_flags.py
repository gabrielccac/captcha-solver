"""Chrome must not spend bandwidth or memory on background downloads."""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'src/captcha/browser.py'


def launch_args():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            values = [element.value for element in node.elts
                      if isinstance(element, ast.Constant) and isinstance(element.value, str)]
            if any('--remote-debugging-port=' in value or '--disable-dev-shm-usage' == value
                   for value in values):
                return values
    raise AssertionError('launch argument list not found')


def test_idle_chrome_cannot_download_models_or_dictionaries():
    args = launch_args()
    features = [value for value in args if value.startswith('--disable-features=')]
    assert features, 'expected a --disable-features entry'
    disabled = set(features[0].split('=', 1)[1].split(','))
    assert {'OptimizationHints', 'OptimizationGuideModelDownloading'} <= disabled
    for flag in ('--disable-component-update', '--disable-spell-checking',
                 '--disable-background-networking', '--disable-sync'):
        assert flag in args


def test_launch_keeps_the_headed_profile_the_login_needs():
    args = launch_args()
    assert '--renderer-process-limit=4' in args
    assert '--disable-dev-shm-usage' in args
    source = SOURCE.read_text(encoding='utf-8')
    assert 'headless=False' in source, 'the login must stay headed by default'
    assert 'command.append("--ozone-platform=x11")' in source.replace("'", '"')
