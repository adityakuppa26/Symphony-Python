"""Bounded verification summaries, coverage, and read-only runtime checks."""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
from pathlib import Path


def excerpt(value: str, limit: int = 500) -> str:
    text = ' '.join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def coverage_summary(plan, request, results: list[dict]) -> list[dict]:
    rows = []
    for requirement in plan.requirements:
        for criterion in requirement.acceptance_criteria:
            selected = [i for i, target in enumerate(request.targets)
                        if criterion.id in target.acceptance_criterion_ids]
            statuses = [results[i]['status'] for i in selected if i < len(results)]
            status = ('uncovered' if not selected else 'pending' if len(statuses) != len(selected)
                      else 'failed' if 'failed' in statuses else 'environment_error' if 'environment_error' in statuses
                      else 'selected_tests_passed')
            rows.append({'requirement_id': requirement.id, 'criterion_id': criterion.id,
                         'statement': criterion.statement, 'status': status,
                         'tests': [f'{request.targets[i].repository}/{request.targets[i].suite}: ' +
                                   ', '.join(request.targets[i].test_args) for i in selected]})
    return rows


def failure_summary(result: dict, output: str = '') -> dict:
    status = result.get('status')
    if status == 'passed':
        return {'summary': 'Selected tests passed.', 'next_step': ''}
    lines = [line.strip() for line in output.splitlines() if any(marker in line.lower()
             for marker in ('assert', 'error', 'failed', 'timeout', 'missing', 'unavailable'))]
    return {'summary': excerpt(' '.join(lines[-5:]) or ('Runtime setup or execution was unavailable.' if status == 'environment_error' else 'Verification did not pass; inspect the saved suite output.')),
            'category': 'environment' if status == 'environment_error' else 'test_or_build',
            'next_step': ('Restore the reported runtime dependency and retry verification.' if status == 'environment_error'
                          else 'Inspect the failing assertion or build output and linked recording before changing code.')}


def runtime_readiness(target, controller: Path) -> dict:
    """Inspect prerequisites without starting services or pulling images."""
    checks, identities = [], {}
    def check(name, ok, detail):
        checks.append({'name': name, 'status': 'ready' if ok else 'unavailable', 'detail': detail})
    check('controller', controller.is_file(), str(controller))
    root = controller.parent
    podman = Path('/usr/bin/podman')
    check('container engine', podman.is_file(), str(podman))
    def inspect(kind, name):
        try:
            result = subprocess.run([str(podman), kind, 'inspect', name], capture_output=True, text=True, timeout=10)
            data = json.loads(result.stdout)[0] if result.returncode == 0 else None
            check(f'{kind} {name}', data is not None, 'Available locally' if data else 'Missing or inaccessible')
            if data:
                identities[f'{kind}:{name}'] = data.get('Id') or data.get('ID')
            return data
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            check(f'{kind} {name}', False, 'Inspection failed or timed out')
            return None
    try:
        pins = []
        if target.repository == 'cpm' or target.suite == 'playwright':
            pins.append(('.state/images.json', None))
            database = inspect('container', 'oracledb19')
            if database:
                checks.append({'name': 'database state', 'status': 'ready' if database.get('State', {}).get('Running') else 'setup_required',
                               'detail': 'Runner checks database health and resets only its owned schema.'})
        if target.suite in {'ibis', 'playwright'}:
            pins.append(('.state/ibis-image.json', 'id'))
        if target.repository == 'foyr2':
            pins.append(('.state/foyr/image.json', 'id'))
        if target.suite == 'playwright':
            pins.append(('.state/playwright/proxy-image.json', 'id'))
            check('fixture seed', (root / 'playwright-seed.py').is_file(),
                  'GC administrator and Home projects; other roles require additional fixtures.')
        for filename, key in pins:
            pin = root / filename
            check('image pin', pin.is_file(), filename)
            if pin.is_file():
                value = json.loads(pin.read_text())
                for name in ([value[key]] if key else list(value.values())):
                    inspect('image', name)
        if target.suite in {'frontend', 'karma', 'playwright'}:
            browsers = list((root / '.state/foyr/browsers').glob('chromium_headless_shell-*/chrome-linux/headless_shell'))
            ready = len(browsers) == 1 and os.access(browsers[0], os.X_OK)
            check('browser', ready, 'Exactly one executable cached Chromium is required.')
            if ready:
                probe = subprocess.run([str(browsers[0]), '--version'], capture_output=True, timeout=15)
                check('browser launch', probe.returncode == 0, 'Chromium --version')
                identities['browser'] = hashlib.sha256(browsers[0].read_bytes()).hexdigest()
        for path in sorted(root.iterdir()):
            if path.is_file() and path.suffix in {'.py', '.sh', '.cjs', '.json', '.yml'}:
                identities[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        check('runtime metadata', False, type(exc).__name__)
    ready = all(item['status'] != 'unavailable' for item in checks)
    return {'repository': target.repository, 'suite': target.suite,
            'status': 'ready_for_setup' if ready else 'unavailable', 'checks': checks,
            'runtime_fingerprint': hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest() if ready else None}


def runner_failure_evidence(output: str, controller: Path) -> dict:
    """Read bounded JUnit failures only from the trusted controller's results tree."""
    import re
    import xml.etree.ElementTree as ET
    from .human_review import HumanReviewContextError, read_frozen_text_artifact
    root = controller.parent
    pattern = re.escape(str(root / 'results')) + r'/([A-Za-z0-9_-]+)'
    directories = list(dict.fromkeys(re.findall(pattern, output)))[:3]
    assertions, paths = [], []
    for directory in directories:
        for filename in ('junit.xml', 'playwright-junit.xml', 'python.xml', 'legacy.xml', 'typescript.xml'):
            relative = f'results/{directory}/{filename}'
            try:
                content = read_frozen_text_artifact(root, relative, label='runner failure evidence', max_bytes=2_000_000)
                if not content:
                    continue
                document = ET.fromstring(content)
            except (HumanReviewContextError, ValueError, OSError, ET.ParseError):
                continue
            paths.append(str(root / relative))
            for case in document.iter('testcase'):
                for tag in ('failure', 'error'):
                    failure = case.find(tag)
                    if failure is not None and len(assertions) < 3:
                        assertions.append(excerpt(f"{case.get('classname', '')}::{case.get('name', '')}: "
                                                  + (failure.get('message') or failure.text or tag), 700))
    return {'assertions': assertions, 'evidence_paths': paths}
