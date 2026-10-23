import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('docker_run', Path(__file__).resolve().parents[1] / 'docker_run.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class DockerTests(unittest.TestCase):
    def test_browser_is_owned_by_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').touch()
            with patch.object(launcher, 'ROOT', root), patch('sys.argv', [
                'docker_run.py', '--cpf', '00000000000', '--out', 'runs/test'
            ]), patch.object(launcher.subprocess, 'call', return_value=0) as run:
                self.assertEqual(launcher.main(), 0)
            command = run.call_args.args[0]
            self.assertNotIn('--cdp-url', command)
            self.assertNotIn('--network', command)
            self.assertIn('/tmp/chrome-profile', command)
            self.assertIn(f'type=bind,src={root / "runs"},dst=/app/runs', command)
            self.assertIn(f'seccomp={root / "docker-seccomp.json"}', command)
