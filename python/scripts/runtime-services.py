#!/usr/bin/env python3
"""Track test-started Compose containers. Call while holding the runtime lock."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.runtime-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(path):
    return json.loads(path.read_text()) if path.exists() else {'containers': {}, 'dependencies': {}}


def podman(args, command):
    result = subprocess.run([args.podman, *command], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f'Podman {command[0]} failed: {result.stderr.strip()[:300]}')
    return result.stdout


def containers(args, filters, *, by_service=True):
    ids = podman(args, ['ps', '-a', *sum((['--filter', item] for item in filters), []), '--format', '{{.ID}}']).split()
    if not ids:
        return {}
    result = {}
    for item in json.loads(podman(args, ['inspect', *ids])):
        labels = item.get('Config', {}).get('Labels') or {}
        name = (labels.get('com.docker.compose.service') if by_service else None) or item['Name'].lstrip('/')
        result[name] = {'id': item['Id'], 'running': bool(item.get('State', {}).get('Running')),
                        'setup': labels.get('symphony.runtime.setup')}
    return result


def scope_for(config, services):
    dependencies = {name: list(service.get('depends_on') or []) for name, service in config['services'].items()}
    scope = set()
    def visit(name):
        if name not in scope:
            scope.add(name)
            for dependency in dependencies.get(name, []):
                visit(dependency)
    for name in services:
        visit(name)
    return sorted(scope), dependencies


def register(state, snapshot, current):
    """Reference-count Symphony-owned services; never acquire running user services."""
    records = state['containers']
    for name in snapshot['scope']:
        after = current.get(name)
        if after is None:
            continue
        before = snapshot['before'].get(name)
        record = records.get(name)
        if record and (before is None or record['id'] != before['id']):
            # Someone replaced the tracked container outside a Symphony setup.
            records.pop(name)
            record = None
        if record:
            if record['id'] != after['id']:
                record['remove'] = True
            record['id'] = after['id']
            record['owners'] = sorted(set(record['owners']) | {snapshot['workspace']})
        elif (before is None or not before['running']) and (
            after['running'] or before is None or before['id'] != after['id']
        ):
            records[name] = {'id': after['id'], 'owners': [snapshot['workspace']],
                             'remove': before is None or before['id'] != after['id']}
    state['dependencies'].update(snapshot['dependencies'])


def releasable(state, current, workspace):
    records = state['containers']
    for name in list(records):
        record = records[name]
        if name not in current or current[name]['id'] != record['id']:
            records.pop(name)
        else:
            record['owners'] = [owner for owner in record['owners'] if owner != workspace]
    candidates = {name for name, record in records.items() if not record['owners']}
    # Preserve dependencies still needed by another running service or workspace.
    protected = {name for name, item in current.items() if item['running'] and name not in candidates}
    pending = list(protected)
    while pending:
        for dependency in state['dependencies'].get(pending.pop(), []):
            if dependency not in protected:
                protected.add(dependency)
                pending.append(dependency)
    candidates -= protected
    ordered = []
    # Dependents first; dependencies last (including independent service chains).
    while candidates:
        roots = [name for name in candidates if not any(name in state['dependencies'].get(other, []) for other in candidates)]
        if not roots:
            roots = sorted(candidates)
        for name in sorted(roots):
            ordered.append(name)
            candidates.remove(name)
    return ordered


# Runs inside the target container. Only processes carrying this execution's
# random token are signalled; the application server has no such token.
STOP_TEST = r'''import os, signal, sys, time
from pathlib import Path
marker = ('SYMPHONY_TEST_TOKEN=' + sys.argv[1]).encode()
def owned():
    result=[]
    for p in Path('/proc').glob('[0-9]*'):
        try:
            if marker in (p/'environ').read_bytes().split(b'\0'):
                result.append(int(p.name))
        except OSError:
            pass
    return result
for sig in (signal.SIGTERM, signal.SIGKILL):
    for pid in owned():
        if pid != os.getpid():
            try: os.kill(pid,sig)
            except ProcessLookupError: pass
    if sig == signal.SIGTERM and owned(): time.sleep(1)
'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['begin', 'finish', 'exec-begin', 'down'])
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--podman', default='/usr/bin/podman')
    parser.add_argument('--services', nargs='*', default=[])
    args = parser.parse_args()
    workspace = str(Path(args.workspace).resolve())
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = read(args.state)
    state.setdefault('execs', [])
    current = containers(args, ['label=com.docker.compose.project=compost'])
    if args.action == 'begin':
        scope, dependencies = scope_for(json.load(sys.stdin), args.services)
        fd, name = tempfile.mkstemp(prefix='setup-', suffix='.json', dir=args.state.parent)
        os.close(fd)
        snapshot = Path(name)
        token = hashlib.sha256(workspace.encode()).hexdigest()
        save(snapshot, {'workspace': workspace, 'scope': scope, 'dependencies': dependencies,
                        'before': current, 'token': token})
        # Stable labels avoid metadata-only recreation on repeated tests. Do not
        # add ownership labels to services that were already running for the user.
        labelled = [name for name in scope if name not in current or not current[name]['running']
                    or state['containers'].get(name, {}).get('id') == current[name]['id']]
        save(Path(str(snapshot) + '.override'), {'services': {name: {'labels': {'symphony.runtime.setup': token}} for name in labelled}})
        print(snapshot)
    elif args.action == 'finish':
        snapshot = json.loads(args.snapshot.read_text())
        if snapshot['workspace'] != workspace:
            raise ValueError('Runtime setup snapshot belongs to another workspace')
        register(state, snapshot, current)
        save(args.state, state)
        args.snapshot.unlink()
        Path(str(args.snapshot) + '.override').unlink(missing_ok=True)
    elif args.action == 'exec-begin':
        service = args.services[0]
        if service not in current or not current[service]['running']:
            raise RuntimeError(f'Test service {service} is not running')
        token = uuid.uuid4().hex
        state['execs'].append({'workspace': workspace, 'container': current[service]['id'], 'token': token})
        save(args.state, state)
        print(token, current[service]['id'])
    else:
        # Recover ownership after an interrupted setup before attempting cleanup.
        for snapshot_path in args.state.parent.glob('setup-*.json'):
            snapshot = json.loads(snapshot_path.read_text())
            if snapshot['workspace'] == workspace:
                recovered = {name: item for name, item in current.items() if item.get('setup') == snapshot['token']}
                register(state, snapshot, recovered)
                save(args.state, state)
                snapshot_path.unlink()
                Path(str(snapshot_path) + '.override').unlink(missing_ok=True)
        failures = []
        active_ids = {item['id'] for item in current.values() if item['running']}
        remaining_execs = []
        for execution in state['execs']:
            if execution['workspace'] != workspace:
                remaining_execs.append(execution)
            elif execution['container'] in active_ids:
                try:
                    podman(args, ['exec', execution['container'], 'python3', '-c', STOP_TEST, execution['token']])
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    remaining_execs.append(execution)
                    failures.append(str(exc))
        state['execs'] = remaining_execs
        for name in releasable(state, current, workspace):
            record = state['containers'][name]
            command = ['rm', '-f', '--time', '30', record['id']] if record['remove'] else ['stop', '--time', '30', record['id']]
            try:
                podman(args, command)
                print(f'Released test service: {name}')
                state['containers'].pop(name)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
                break
        save(args.state, state)
        for name, record in state['containers'].items():
            if not record['owners']:
                print(f'Retained shared or retry-pending service: {name}')
        for name, item in containers(args, ['label=symphony.runtime=job', f'label=symphony.workspace={workspace}'], by_service=False).items():
            try:
                podman(args, ['rm', '-f', '--time', '0', item['id']])
                print(f'Removed test runner: {name}')
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError('; '.join(failures))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'Runtime ownership/cleanup failed: {exc}', file=sys.stderr)
        sys.exit(125)
