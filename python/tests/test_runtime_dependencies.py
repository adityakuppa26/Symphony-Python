import importlib.metadata
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'runtime-dependencies.py'


class RuntimeDependencyTests(unittest.TestCase):
    def test_cache_reuse_offline_install_and_pin_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / 'ibis/api/requirements.txt'
            requirements.parent.mkdir(parents=True)
            requirements.write_text('gunicorn==26.2.0\nunrelated-package==1.0\n')
            def invoke(prepare):
                with patch('sys.argv', [str(SCRIPT), 'ibis', *(['--prepare'] if prepare else [])]), \
                     patch('pathlib.Path', side_effect=lambda value: root / str(value).lstrip('/')), \
                     patch.object(importlib.metadata, 'version', return_value='21.2.0'), \
                     patch.object(importlib.metadata, 'distributions', return_value=[]), \
                     patch.object(subprocess, 'run') as run:
                    runpy.run_path(str(SCRIPT), run_name='__main__')
                    return run
            first = invoke(True)
            args = first.call_args.args[0]
            self.assertIn('download', args)
            self.assertIn('gunicorn==26.2.0', args)
            self.assertNotIn('unrelated-package==1.0', args)
            invoke(True).assert_not_called()
            install = invoke(False).call_args.args[0]
            self.assertIn('--no-index', install)
            self.assertIn('--find-links', install)
            requirements.write_text('gunicorn==26.3.0\n')
            with self.assertRaisesRegex(SystemExit, 'runtime cache missing'):
                invoke(False)
            self.assertIn('gunicorn==26.3.0', invoke(True).call_args.args[0])

    def test_failed_download_is_not_marked_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / 'ibis/api/requirements.txt'
            requirements.parent.mkdir(parents=True)
            requirements.write_text('gunicorn==26.2.0\n')
            with patch('sys.argv', [str(SCRIPT), 'ibis', '--prepare']), \
                 patch('pathlib.Path', side_effect=lambda value: root / str(value).lstrip('/')), \
                 patch.object(importlib.metadata, 'version', return_value='21.2.0'), \
                 patch.object(importlib.metadata, 'distributions', return_value=[]), \
                 patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
                with self.assertRaises(subprocess.CalledProcessError):
                    runpy.run_path(str(SCRIPT), run_name='__main__')
            self.assertEqual(list(root.rglob('.ready')), [])
