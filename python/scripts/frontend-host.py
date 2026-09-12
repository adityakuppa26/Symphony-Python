#!/usr/bin/env python3
"""Run Karma in the existing image and capture it with a private host browser."""
import signal
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def stop(process, *, group=False):
    if process is not None and (group or process.poll() is None):
        try:
            if group:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=5)
        except ProcessLookupError:
            process.wait()
        except subprocess.TimeoutExpired:
            if group:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
        finally:
            if group:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def main():
    browser_path = sys.argv[1]
    command = sys.argv[3:]
    runner = browser = None
    browser_unit = None
    # Refuse to capture somebody else's already-running Karma server.
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 9876))
    except OSError:
        print('Frontend runtime: host port 9876 is already in use', flush=True)
        return 125
    def interrupted(*_):
        raise InterruptedError('Frontend verification interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix='symphony-chrome-') as profile:
        try:
            runner = subprocess.Popen(command)
            while runner.poll() is None:
                if browser is None:
                    try:
                        with opener.open('http://127.0.0.1:9876/', timeout=1) as response:
                            ready = response.status == 200
                    except (OSError, urllib.error.URLError):
                        ready = False
                    if ready:
                        browser_unit = f'symphony-browser-{uuid.uuid4().hex}.scope'
                        browser = subprocess.Popen([
                            'systemd-run', '--user', '--scope', '--quiet', '--collect',
                            '--unit', browser_unit, '-p', 'MemoryMax=768M',
                            '-p', 'MemorySwapMax=0', '-p', 'CPUQuota=100%',
                            browser_path, '--headless', '--no-sandbox', '--disable-gpu',
                            '--disable-dev-shm-usage', '--no-first-run', '--no-default-browser-check',
                            f'--user-data-dir={profile}', 'http://127.0.0.1:9876/',
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                        print('Host Chrome connected to Karma', flush=True)
                elif browser.poll() is not None:
                    print('Frontend runtime: host browser exited before tests finished', flush=True)
                    return 125
                time.sleep(0.5)
            return runner.returncode if runner.returncode else (0 if browser else 125)
        except (OSError, InterruptedError) as exc:
            print(f'Frontend runtime unavailable: {exc}', flush=True)
            return 125
        finally:
            stop(browser, group=True)
            if browser_unit:
                subprocess.run(['systemctl', '--user', 'stop', browser_unit],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False, timeout=10)
            stop(runner)


if __name__ == '__main__':
    sys.exit(main())
