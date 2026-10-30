#!/usr/bin/env python3
"""Research run ledger worker using only the Python standard library."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_STDIN_BYTES = 4 * 1024 * 1024
RUN_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')
PIPELINE_PATTERN = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')
SENSITIVE_KEY = re.compile(r'token|secret|password|credential|api[_-]?key|private[_-]?key', re.I)
STATUSES = {'planned', 'running', 'complete', 'failed', 'cancelled'}
TRANSITIONS = {
    'planned': {'planned', 'running', 'cancelled'},
    'running': {'running', 'complete', 'failed', 'cancelled'},
    'failed': {'failed', 'running', 'cancelled'},
    'cancelled': {'cancelled', 'running'},
    'complete': {'complete'},
}


class RunError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not raw.strip() or not Path(raw).is_absolute():
        raise RunError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / 'runs').mkdir(exist_ok=True)
    return root


def resolve_project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise RunError('projectRoot is required for this action')
    if not isinstance(allowed, list) or not allowed or any(not isinstance(item, str) for item in allowed):
        raise RunError('allowedRoots must be a non-empty string array supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise RunError(f'Project root is not a directory: {root}')
    for item in allowed:
        if inside(root, Path(item).expanduser().resolve()):
            return root
    raise RunError(f'Project root is outside allowedRoots: {root}')


def run_dir(state_root: Path, run_id: Any) -> Path:
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise RunError('runId contains invalid characters')
    return (state_root / 'runs' / run_id).resolve()


def load_manifest(state_root: Path, run_id: Any) -> dict[str, Any]:
    path = run_dir(state_root, run_id) / 'manifest.json'
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise RunError(f'Run not found or manifest too large: {run_id}')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        raise RunError(f'Cannot read run manifest {run_id}: {error}') from error


def save_manifest(state_root: Path, manifest: dict[str, Any]) -> None:
    directory = run_dir(state_root, manifest['runId'])
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'manifest.json'
    temporary = directory / f'.manifest-{uuid.uuid4().hex}.tmp'
    try:
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def redact_config(value: Any, path: str = 'config', redacted: list[str] | None = None) -> Any:
    if redacted is None:
        redacted = []
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if SENSITIVE_KEY.search(str(key)):
                result[key] = '[REDACTED]'
                redacted.append(f'{path}.{key}')
            else:
                result[key] = redact_config(item, f'{path}.{key}', redacted)
        return result
    if isinstance(value, list):
        return [redact_config(item, f'{path}[{index}]', redacted) for index, item in enumerate(value)]
    return value


def artifact_path(project_root: Path, state_root: Path, raw: Any, allow_state: bool) -> tuple[Path, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise RunError('Artifact paths must be non-empty strings')
    if raw.startswith('state://'):
        if not allow_state:
            raise RunError('state:// paths are only allowed for outputs')
        relative = raw[len('state://'):]
        path = (state_root / relative).resolve()
        if not inside(path, state_root):
            raise RunError(f'State artifact escapes stateRoot: {raw}')
        return path, raw
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()
    if not inside(path, project_root):
        raise RunError(f'Artifact escapes projectRoot: {raw}')
    return path, path.relative_to(project_root).as_posix()


def artifact_entry(project_root: Path, state_root: Path, raw: Any, role: str, max_hash_bytes: int) -> dict[str, Any]:
    path, stored = artifact_path(project_root, state_root, raw, role == 'output')
    if not path.is_file():
        raise RunError(f'Artifact is not a file: {raw}')
    size = path.stat().st_size
    if size > max_hash_bytes:
        raise RunError(f'Artifact exceeds hash limit: {raw}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'path': stored, 'role': role, 'bytes': size, 'sha256': digest.hexdigest()}


def path_list(value: Any, name: str, allow_empty: bool = True) -> list[str]:
    if value is None and allow_empty:
        return []
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 200:
        raise RunError(f'{name} must be an array with 1-200 entries' if not allow_empty else f'{name} must be an array with at most 200 entries')
    if any(not isinstance(item, str) for item in value):
        raise RunError(f'{name} entries must be strings')
    return value


def create_run(request: dict[str, Any], state_root: Path) -> dict[str, Any]:
    project_root = resolve_project_root(request)
    pipeline = request.get('pipeline')
    if not isinstance(pipeline, str) or not PIPELINE_PATTERN.fullmatch(pipeline):
        raise RunError('pipeline must use 1-100 letters, numbers, dots, underscores, or hyphens')
    config = request.get('config', {})
    if not isinstance(config, dict):
        raise RunError('config must be a JSON object')
    redacted: list[str] = []
    safe_config = redact_config(config, redacted=redacted)
    max_hash_bytes = request['maxHashBytes']
    inputs = [artifact_entry(project_root, state_root, raw, 'input', max_hash_bytes) for raw in path_list(request.get('inputs'), 'inputs')]
    steps_value = request.get('steps', ['run'])
    if not isinstance(steps_value, list) or not steps_value or len(steps_value) > 50:
        raise RunError('steps must contain 1-50 step names')
    if any(not isinstance(step, str) or not step.strip() for step in steps_value):
        raise RunError('step names must be non-empty strings')
    if len(set(steps_value)) != len(steps_value):
        raise RunError('step names must be unique')
    timestamp = utc_now()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    manifest = {
        'runId': run_id,
        'plugin': 'research-run',
        'pluginVersion': '0.1.0',
        'pipeline': pipeline,
        'projectRoot': str(project_root),
        'createdUtc': timestamp,
        'startedUtc': timestamp,
        'updatedUtc': timestamp,
        'status': 'planned',
        'config': safe_config,
        'inputs': inputs,
        'outputs': [],
        'steps': [{'name': step, 'status': 'planned'} for step in steps_value],
        'history': [{'utc': timestamp, 'status': 'planned', 'note': 'Run created'}],
    }
    if redacted:
        manifest['redactedConfigKeys'] = redacted
    save_manifest(state_root, manifest)
    return {'ok': True, 'action': 'create', 'run': manifest}


def list_runs(request: dict[str, Any], state_root: Path) -> dict[str, Any]:
    limit = request.get('limit', 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise RunError('limit must be an integer from 1 to 200')
    runs = []
    for path in (state_root / 'runs').glob('*/manifest.json'):
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if request.get('pipeline') is not None and manifest.get('pipeline') != request['pipeline']:
            continue
        if request.get('status') is not None and manifest.get('status') != request['status']:
            continue
        if request.get('projectRoot') is not None and manifest.get('projectRoot') != request['projectRoot']:
            continue
        runs.append({
            'runId': manifest.get('runId'),
            'pipeline': manifest.get('pipeline'),
            'projectRoot': manifest.get('projectRoot'),
            'status': manifest.get('status'),
            'startedUtc': manifest.get('startedUtc'),
            'updatedUtc': manifest.get('updatedUtc'),
            'inputCount': len(manifest.get('inputs', [])),
            'outputCount': len(manifest.get('outputs', [])),
        })
    runs.sort(key=lambda item: item.get('updatedUtc') or '', reverse=True)
    return {'ok': True, 'action': 'list', 'total': len(runs), 'runs': runs[:limit]}


def apply_status(manifest: dict[str, Any], status: str, note: str, step: Any = None, step_status: Any = None) -> None:
    if status not in STATUSES:
        raise RunError(f'Invalid status: {status}')
    current = manifest['status']
    if status not in TRANSITIONS[current]:
        raise RunError(f'Invalid status transition: {current} -> {status}')
    manifest['status'] = status
    if step is not None:
        if not isinstance(step, str):
            raise RunError('step must be a string')
        target = next((item for item in manifest.get('steps', []) if item.get('name') == step), None)
        if target is None:
            raise RunError(f'Unknown step: {step}')
        new_step_status = step_status if step_status is not None else status
        if new_step_status not in STATUSES:
            raise RunError(f'Invalid step status: {new_step_status}')
        target['status'] = new_step_status
        if new_step_status == 'running' and 'startedUtc' not in target:
            target['startedUtc'] = utc_now()
        if new_step_status in {'complete', 'failed', 'cancelled'}:
            target['endedUtc'] = utc_now()
    manifest['updatedUtc'] = utc_now()
    event = {'utc': manifest['updatedUtc'], 'status': status, 'note': note}
    if step is not None:
        event['step'] = step
        event['stepStatus'] = step_status if step_status is not None else status
    manifest.setdefault('history', []).append(event)


def update_status(request: dict[str, Any], state_root: Path) -> dict[str, Any]:
    manifest = load_manifest(state_root, request.get('runId'))
    note = request.get('note', '')
    if not isinstance(note, str) or len(note) > 4000:
        raise RunError('note must be a string of at most 4000 characters')
    apply_status(manifest, request.get('status'), note, request.get('step'), request.get('stepStatus'))
    save_manifest(state_root, manifest)
    return {'ok': True, 'action': 'update_status', 'run': manifest}


def register_artifacts(request: dict[str, Any], state_root: Path) -> dict[str, Any]:
    manifest = load_manifest(state_root, request.get('runId'))
    project_root = resolve_project_root({'projectRoot': manifest['projectRoot'], 'allowedRoots': request['allowedRoots']})
    raw_paths = path_list(request.get('artifacts'), 'artifacts', allow_empty=False)
    entries = [artifact_entry(project_root, state_root, raw, 'output', request['maxHashBytes']) for raw in raw_paths]
    existing = {item['path'] for item in manifest.get('outputs', [])}
    manifest.setdefault('outputs', []).extend(entry for entry in entries if entry['path'] not in existing)
    if request.get('status') is not None:
        apply_status(manifest, request['status'], request.get('note', 'Artifacts registered'), request.get('step'), request.get('stepStatus'))
    elif request.get('step') is not None:
        apply_status(manifest, manifest['status'], request.get('note', 'Artifacts registered'), request['step'], 'complete')
    else:
        manifest['updatedUtc'] = utc_now()
        manifest.setdefault('history', []).append({'utc': manifest['updatedUtc'], 'status': manifest['status'], 'note': 'Artifacts registered'})
    save_manifest(state_root, manifest)
    return {'ok': True, 'action': 'register_artifacts', 'registered': entries, 'run': manifest}


def verify_entry(project_root: Path, state_root: Path, entry: dict[str, Any], max_hash_bytes: int) -> dict[str, Any] | None:
    try:
        path, stored = artifact_path(project_root, state_root, entry['path'], True)
    except (RunError, KeyError) as error:
        return {'code': 'INVALID_ARTIFACT_PATH', 'severity': 'error', 'message': str(error), 'path': entry.get('path')}
    if not path.is_file():
        return {'code': 'ARTIFACT_MISSING', 'severity': 'error', 'message': 'Artifact is missing', 'path': stored}
    size = path.stat().st_size
    if size > max_hash_bytes:
        return {'code': 'HASH_SKIPPED_LARGE', 'severity': 'warning', 'message': 'Artifact exceeds configured hash limit', 'path': stored}
    actual = artifact_entry(project_root, state_root, entry['path'], 'output', max_hash_bytes)
    if entry.get('bytes') is not None and entry['bytes'] != actual['bytes']:
        return {'code': 'ARTIFACT_BYTES_MISMATCH', 'severity': 'error', 'message': 'Artifact byte count changed', 'path': stored, 'expected': entry['bytes'], 'actual': actual['bytes']}
    if entry.get('sha256') != actual['sha256']:
        return {'code': 'ARTIFACT_SHA256_MISMATCH', 'severity': 'error', 'message': 'Artifact content changed', 'path': stored, 'expected': entry.get('sha256'), 'actual': actual['sha256']}
    return None


def verify_run(request: dict[str, Any], state_root: Path) -> dict[str, Any]:
    manifest = load_manifest(state_root, request.get('runId'))
    project_root = resolve_project_root({'projectRoot': manifest['projectRoot'], 'allowedRoots': request['allowedRoots']})
    findings = []
    for entry in [*manifest.get('inputs', []), *manifest.get('outputs', [])]:
        issue = verify_entry(project_root, state_root, entry, request['maxHashBytes'])
        if issue is not None:
            findings.append(issue)
    severities = Counter(item['severity'] for item in findings)
    return {
        'ok': True,
        'action': 'verify',
        'runId': manifest['runId'],
        'runStatus': manifest['status'],
        'auditStatus': 'stale' if findings else 'current',
        'summary': {'inputs': len(manifest.get('inputs', [])), 'outputs': len(manifest.get('outputs', [])), 'findingsBySeverity': dict(severities)},
        'findings': findings,
    }


def self_test() -> dict[str, Any]:
    checks = 0
    with tempfile.TemporaryDirectory(prefix='run-worker-') as temporary:
        base = Path(temporary)
        project = base / 'project'
        project.mkdir()
        state = base / 'state'
        (project / 'input.csv').write_text('a,b\n1,2\n', encoding='utf-8')
        request = {
            'action': 'create', 'projectRoot': str(project), 'allowedRoots': [str(project)],
            'stateRoot': str(state), 'maxHashBytes': 1024, 'pipeline': 'demo',
            'config': {'year': 2024, 'apiKey': 'do-not-store'}, 'inputs': ['input.csv'], 'steps': ['compute'],
        }
        created = create_run(request, state.resolve())
        run_id = created['run']['runId']
        assert created['run']['config']['apiKey'] == '[REDACTED]'
        checks += 1
        assert list_runs({'limit': 10}, state.resolve())['total'] == 1
        checks += 1
        running = update_status({'runId': run_id, 'status': 'running', 'step': 'compute', 'note': 'start'}, state.resolve())
        assert running['run']['steps'][0]['status'] == 'running'
        checks += 1
        (project / 'result.csv').write_text('value\n42\n', encoding='utf-8')
        registered = register_artifacts({'runId': run_id, 'artifacts': ['result.csv'], 'allowedRoots': [str(project)], 'maxHashBytes': 1024, 'status': 'complete', 'step': 'compute'}, state.resolve())
        assert registered['run']['status'] == 'complete'
        checks += 1
        current = verify_run({'runId': run_id, 'allowedRoots': [str(project)], 'maxHashBytes': 1024}, state.resolve())
        assert current['auditStatus'] == 'current'
        checks += 1
        (project / 'result.csv').write_text('value\n43\n', encoding='utf-8')
        stale = verify_run({'runId': run_id, 'allowedRoots': [str(project)], 'maxHashBytes': 1024}, state.resolve())
        assert stale['auditStatus'] == 'stale' and stale['findings'][0]['code'] == 'ARTIFACT_SHA256_MISMATCH'
        checks += 1
        assert load_manifest(state.resolve(), run_id)['outputs'][0]['path'] == 'result.csv'
        checks += 1
        try:
            update_status({'runId': run_id, 'status': 'planned'}, state.resolve())
        except RunError:
            checks += 1
        else:
            raise AssertionError('Invalid transition accepted')
        try:
            create_run({**request, 'inputs': ['../escape.csv']}, state.resolve())
        except RunError:
            checks += 1
        else:
            raise AssertionError('Escaping input accepted')
        assert len(created['run']['redactedConfigKeys']) == 1
        checks += 1
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise RunError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise RunError('Worker request must be a JSON object')
            max_hash_bytes = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash_bytes, int) or isinstance(max_hash_bytes, bool) or not 1024 <= max_hash_bytes <= 2147483648:
                raise RunError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash_bytes
            state_root = resolve_state_root(request)
            action = request.get('action')
            if action == 'create':
                result = create_run(request, state_root)
            elif action == 'list':
                result = list_runs(request, state_root)
            elif action == 'get':
                result = {'ok': True, 'action': 'get', 'run': load_manifest(state_root, request.get('runId'))}
            elif action == 'update_status':
                result = update_status(request, state_root)
            elif action == 'register_artifacts':
                result = register_artifacts(request, state_root)
            elif action == 'verify':
                result = verify_run(request, state_root)
            else:
                raise RunError(f'Unsupported action: {action}')
    except RunError as error:
        result = {'ok': False, 'error': {'code': 'RUN_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'RUN_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
