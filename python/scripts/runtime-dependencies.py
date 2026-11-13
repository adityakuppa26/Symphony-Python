#!/usr/bin/env python3
"""Prepare pinned runtime wheels over host networking; install offline at startup."""
import hashlib
import importlib.metadata as metadata
import json
import platform
from pathlib import Path
import re
import subprocess
import sys

service = sys.argv[1]
prepare = '--prepare' in sys.argv[2:]
if service == 'ibis':
    names = {'connexion', 'starlette', 'a2wsgi', 'oracledb', 'gunicorn'}
    declarations = Path('/ibis/api/requirements.txt').read_text().splitlines()
    groups = {'compatibility': declarations}
else:
    import tomllib
    project = tomllib.loads(Path('/src/pyproject.toml').read_text()).get('project', {})
    tests = project.get('optional-dependencies', {}).get('test', [])
    if not tests:
        raise SystemExit('Foyr has no declared test extras; cannot prepare its pytest environment')
    names = None
    groups = {
        'compatibility': [value for value in project.get('dependencies', [])
                          if value.startswith('textura.vault_client==')],
        'test': tests,
    }
for group, declarations in groups.items():
    needed = []
    requested = []
    for raw in declarations:
        value = raw.split('#', 1)[0].strip().replace(' ', '')
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+-]+)', value)
        if not match:
            if service == 'foyr':
                raise SystemExit('Runtime preparation requires explicit name==version pins; this dependency declaration is unsupported')
            continue
        if names is not None and match[1].lower() not in names:
            continue
        requested.append(value)
        try:
            installed = metadata.version(match[1])
        except metadata.PackageNotFoundError:
            installed = None
        if installed != match[2]:
            needed.append(value)
    if not needed:
        continue
    # Key the complete declared environment, not the subset currently missing.
    # Retrying a partially installed container must find the same prepared wheels.
    requested = sorted(set(requested))
    identity = ['built-wheels-v1', group, requested, sys.version, sys.platform, platform.machine()]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    cache = Path('/symphony-cache') / 'wheels' / service / key
    ready = cache / '.ready'
    if prepare:
        if not ready.exists():
            cache.mkdir(parents=True, exist_ok=True)
            subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--disable-pip-version-check',
                            '--progress-bar', 'off', '--retries', '3', '--timeout', '120',
                            '--wheel-dir', str(cache), *requested], check=True)
            ready.write_text(json.dumps(requested))
        print(f'{service}: runtime wheels cached', flush=True)
    else:
        if not ready.exists():
            raise SystemExit(f'{service}: runtime cache missing; run Symphony runtime preparation first')
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                        '--no-index', '--only-binary=:all:', '--find-links', str(cache), *requested], check=True)
