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
                     patch.object(subprocess, 'run') as run:
                    runpy.run_path(str(SCRIPT), run_name='__main__')
                    return run
            first = invoke(True)
            args = first.call_args.args[0]
            self.assertIn('wheel', args)
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

    def test_failed_wheel_build_is_not_marked_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            requirements = root / 'ibis/api/requirements.txt'
            requirements.parent.mkdir(parents=True)
            requirements.write_text('gunicorn==26.2.0\n')
            with patch('sys.argv', [str(SCRIPT), 'ibis', '--prepare']), \
                 patch('pathlib.Path', side_effect=lambda value: root / str(value).lstrip('/')), \
                 patch.object(importlib.metadata, 'version', return_value='21.2.0'), \
                 patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
                with self.assertRaises(subprocess.CalledProcessError):
                    runpy.run_path(str(SCRIPT), run_name='__main__')
            self.assertEqual(list(root.rglob('.ready')), [])

    def test_foyr_prepares_declared_test_dependencies_and_reuses_cache_after_partial_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'src/pyproject.toml'
            project.parent.mkdir(parents=True)
            project.write_text('[project]\ndependencies = ["textura.vault_client==1.0"]\n[project.optional-dependencies]\ntest = ["pytest==9.0.3", "pytest-xdist==3.8.0"]\n')
            installed = {'textura.vault_client': '1.0'}
            def version(name):
                return installed.get(name, '0')
            def invoke(prepare):
                with patch('sys.argv',[str(SCRIPT),'foyr',*(['--prepare'] if prepare else [])]), \
                     patch('pathlib.Path',side_effect=lambda value: root/str(value).lstrip('/')), \
                     patch.object(importlib.metadata,'version',side_effect=version), \
                     patch.object(subprocess,'run') as command:
                    runpy.run_path(str(SCRIPT),run_name='__main__')
                    return command
            preparation = invoke(True)
            self.assertEqual(preparation.call_count, 1)
            prepared = preparation.call_args.args[0]
            self.assertNotIn('textura.vault_client==1.0', prepared)
            self.assertIn('pytest==9.0.3', prepared)
            self.assertIn('pytest-xdist==3.8.0', prepared)
            self.assertIn('wheel', prepared)
            installed['pytest'] = '9.0.3'
            # A partial prior installation must not change the cache key.
            invoke(True).assert_not_called()
            command = invoke(False).call_args.args[0]
            self.assertIn('--no-index', command)
            self.assertIn('pytest-xdist==3.8.0', command)
