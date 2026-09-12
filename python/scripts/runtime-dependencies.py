#!/usr/bin/env python3
"""Prepare pinned runtime wheels over host networking; install offline at startup."""
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys

service = sys.argv[1]
prepare = '--prepare' in sys.argv[2:]
if service == 'ibis':
    names = {'connexion', 'starlette', 'a2wsgi', 'oracledb', 'gunicorn'}
    declarations = Path('/ibis/api/requirements.txt').read_text().splitlines()
else:
    import tomllib
    names = {'textura.vault_client'}
    declarations = tomllib.loads(Path('/src/pyproject.toml').read_text()).get('project', {}).get('dependencies', [])
needed = []
for raw in declarations:
    value = raw.split('#', 1)[0].strip().replace(' ', '')
    match = re.fullmatch(r'([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.+-]+)', value)
    if not match or match[1].lower() not in names:
        continue
    try:
        installed = metadata.version(match[1])
    except metadata.PackageNotFoundError:
        installed = None
    if installed != match[2]:
        needed.append(value)
if not needed:
    sys.exit(0)
# Include the interpreter and full installed environment: resolution can differ
# between images even when their top-level missing declarations are identical.
identity = [sorted(needed), sys.version, sorted((d.metadata['Name'], d.version) for d in metadata.distributions() if d.metadata['Name'])]
key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
cache = Path('/symphony-cache') / 'wheels' / service / key
ready = cache / '.ready'
if prepare:
    if not ready.exists():
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--disable-pip-version-check',
                        '--progress-bar', 'off', '--retries', '3', '--timeout', '120',
                        '--dest', str(cache), *needed], check=True)
        ready.write_text(json.dumps(needed))
    print(f'{service}: runtime wheels cached', flush=True)
else:
    if not ready.exists():
        raise SystemExit(f'{service}: runtime cache missing; run Symphony runtime preparation first')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                    '--no-index', '--find-links', str(cache), *needed], check=True)
