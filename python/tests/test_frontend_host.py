import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('frontend_host', Path(__file__).resolve().parents[1] / 'scripts/frontend-host.py')
frontend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frontend)


class FrontendHostTests(unittest.TestCase):
    def test_busy_port_never_starts_job(self):
        with patch.object(frontend.sys, 'argv', ['runner', '/chrome', '--', 'job']), \
             patch.object(frontend.socket, 'socket', side_effect=OSError('busy')), \
             patch.object(frontend.subprocess, 'Popen') as start:
            self.assertEqual(frontend.main(), 125)
            start.assert_not_called()

    def test_test_failure_is_preserved_and_browser_is_stopped(self):
        runner = Mock(returncode=1)
        runner.poll.side_effect = [None, 1, 1]
        browser = Mock()
        browser.poll.return_value = None
        browser.wait.return_value = 0
        response = Mock(status=200)
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(frontend.sys, 'argv', ['runner', '/private/chrome', '--', 'job']), \
             patch.object(frontend.socket, 'socket') as sock, \
             patch.object(frontend.signal, 'signal'), \
             patch.object(frontend.subprocess, 'run'), \
             patch('os.killpg') as kill_group, \
             patch.object(frontend.time, 'sleep'), \
             patch.object(frontend.urllib.request, 'build_opener', return_value=opener), \
             patch.object(frontend.subprocess, 'Popen', side_effect=[runner, browser]) as start:
            self.assertEqual(frontend.main(), 1)
            self.assertIn('http://127.0.0.1:9876/', start.call_args.args[0])
            self.assertTrue(any(arg.startswith('--user-data-dir=') for arg in start.call_args.args[0]))
            self.assertEqual(start.call_args.args[0][0], "systemd-run")
            self.assertIn("MemoryMax=768M", start.call_args.args[0])
            self.assertIn("CPUQuota=100%", start.call_args.args[0])
            self.assertTrue(start.call_args.kwargs.get("start_new_session"))
            kill_group.assert_any_call(browser.pid, frontend.signal.SIGTERM)
