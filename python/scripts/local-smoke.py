#!/usr/bin/env python3
"""Trusted smoke profiles for an existing Compost stack; called under its lock."""
import argparse
import json
import os
import signal
import shutil
from pathlib import Path
import subprocess
import sys
import uuid

PODMAN = '/usr/bin/podman'
SCRIPTS = Path(__file__).resolve().parent


class Unavailable(RuntimeError):
    pass


def inspect_container(name):
    result = subprocess.run([PODMAN, 'inspect', name], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise Unavailable(f'{name} is not available')
    item = json.loads(result.stdout)[0]
    if not item['State']['Running']:
        raise Unavailable(f'{name} is not running')
    health = (item['State'].get('Health') or item['State'].get('Healthcheck') or {}).get('Status')
    if health and health != 'healthy':
        raise Unavailable(f'{name} is not healthy')
    return item


def require_mount(item, destination, expected):
    actual = next((m['Source'] for m in item['Mounts'] if m['Destination'] == destination), None)
    if actual is None or Path(actual).resolve() != expected.resolve():
        raise Unavailable(f"{item['Name']} does not mount the requested checkout at {destination}")


def check_runtime(workspace, repository, profile):
    if (repository, profile) not in {('cpm', 'projects-query'), ('foyr2', 'projects-page')}:
        raise ValueError('Unsupported smoke profile/repository')
    root = workspace if (workspace / repository).is_dir() else workspace.parent
    ibis = inspect_container('ibis')
    require_mount(ibis, '/ibis', root / 'cpm')
    if profile == 'projects-page':
        foyr = inspect_container('foyr')
        static = inspect_container('static')
        require_mount(foyr, '/src', root / 'foyr2')
        require_mount(static, '/foyr_src', root / 'foyr2')
    return root, ibis['Id']


def run_smoke(workspace, repository, profile):
    root, ibis_id = check_runtime(workspace, repository, profile)
    if profile == 'projects-query':
        user_id = os.environ.get('SYMPHONY_SMOKE_USER_ID', '')
        if not user_id.isascii() or not user_id.isdecimal() or int(user_id) < 1:
            raise Unavailable('Set SYMPHONY_SMOKE_USER_ID to an eligible local Projects fixture user')
        return subprocess.run([
            PODMAN, 'exec', '-i', ibis_id, 'timeout', '--kill-after=5s', '120s',
            '/virtualenv/ibis/bin/python', '-', user_id,
        ], input=(SCRIPTS / 'projects-query-smoke.py').read_text(), text=True, timeout=130).returncode
    state = os.environ.get('SYMPHONY_SMOKE_STORAGE_STATE', '')
    if not state or not Path(state).is_file():
        raise Unavailable('Set SYMPHONY_SMOKE_STORAGE_STATE to a local Playwright login-state file')
    if not shutil.which('node'):
        raise Unavailable('Node is not installed on the host')
    if subprocess.run(['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, timeout=5).returncode:
        raise Unavailable('The host user systemd session is unavailable')
    # Authentication remains in a host-owned file, never in requests or test logs.
    unit = f'symphony-smoke-{uuid.uuid4().hex}.scope'
    try:
        return subprocess.run([
            'systemd-run', '--user', '--scope', '--quiet', '--collect', '--unit', unit,
            '-p', 'MemoryMax=768M', '-p', 'MemorySwapMax=0', '-p', 'CPUQuota=100%',
            'timeout', '--kill-after=5s', '90s', 'node', str(SCRIPTS / 'projects-page-smoke.cjs'),
            str(root / 'foyr2'), str(Path(state).resolve()),
        ], timeout=100).returncode
    finally:
        subprocess.run(['systemctl', '--user', 'stop', unit],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--repository', required=True, choices=['cpm', 'foyr2'])
    parser.add_argument('profile', choices=['projects-query', 'projects-page'])
    args = parser.parse_args(argv)
    try:
        code = run_smoke(args.workspace.resolve(), args.repository, args.profile)
        return 125 if code in (124, 137) else code
    except (Unavailable, OSError, subprocess.TimeoutExpired) as exc:
        print(f'Smoke environment unavailable: {exc}', flush=True)
        return 125


if __name__ == '__main__':
    def interrupted(*_):
        raise Unavailable('Smoke verification interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    sys.exit(main())
