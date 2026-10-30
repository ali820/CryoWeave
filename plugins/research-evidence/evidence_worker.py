#!/usr/bin/env python3
"""Dependency-free, read-only research evidence audit worker."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHECKS = {'authority', 'figures', 'claims', 'conflicts'}
SKIP_DIRS = {
    '.git', '.venv', 'venv', 'node_modules', '__pycache__', '#syncversion',
    '_archive', 'archive', 'tmp', 'temp',
}
MAX_JSON_BYTES = 4 * 1024 * 1024


class EvidenceError(ValueError):
    pass


def finding(code: str, severity: str, message: str, path: str = '', **extra: Any) -> dict[str, Any]:
    result = {'code': code, 'severity': severity, 'path': path, 'message': message}
    result.update({key: value for key, value in extra.items() if value is not None})
    return result


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def display(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


def project_root(request: dict[str, Any]) -> Path:
    raw_root = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise EvidenceError('projectRoot must be a non-empty absolute path')
    if not isinstance(allowed, list) or not allowed or any(not isinstance(item, str) or not item.strip() for item in allowed):
        raise EvidenceError('allowedRoots must be a non-empty string array supplied by plugin config')
    root = Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        raise EvidenceError(f'Project root is not a directory: {root}')
    allowed_resolved = [Path(item).expanduser().resolve() for item in allowed]
    if not any(inside(root, parent) for parent in allowed_resolved):
        raise EvidenceError(f'Project root is outside allowedRoots: {root}')
    return root


def bounded_file(root: Path, raw_path: str | Path) -> Path:
    candidate = Path(raw_path)
    raw_text = str(raw_path)
    # Reject UNC before resolve(), which could otherwise touch a network share.
    if candidate.drive.startswith('\\\\') or raw_text.startswith(('\\\\', '//')):
        raise EvidenceError(f'Path escapes project root: {raw_path}')
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not inside(path, root):
        raise EvidenceError(f'Path escapes project root: {raw_path}')
    if not path.is_file():
        raise EvidenceError(f'File not found: {raw_path}')
    return path


def scan(root: Path, max_files: int) -> tuple[dict[str, list[Path]], int, bool]:
    found: dict[str, list[Path]] = {check: [] for check in CHECKS}
    scanned = 0
    truncated = False
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if not name.lower().startswith('.stfolder') and name.lower() not in SKIP_DIRS
        )
        for name in sorted(filenames):
            scanned += 1
            if scanned > max_files:
                truncated = True
                return found, scanned - 1, truncated
            path = Path(directory) / name
            if name == 'CURRENT_AUTHORITY.md':
                found['authority'].append(path)
            elif name.endswith('.export.json'):
                found['figures'].append(path)
            elif name.startswith('claim_evidence_map') and name.endswith('.csv'):
                found['claims'].append(path)
            elif name == 'numeric_conflict_register.csv':
                found['conflicts'].append(path)
    return found, scanned, truncated


def read_json(root: Path, path: Path) -> Any:
    resolved = bounded_file(root, path)
    if resolved.stat().st_size > MAX_JSON_BYTES:
        raise EvidenceError(f'JSON file exceeds {MAX_JSON_BYTES} bytes: {display(root, resolved)}')
    with resolved.open('rb') as stream:
        payload = stream.read(MAX_JSON_BYTES + 1)
    if len(payload) > MAX_JSON_BYTES:
        raise EvidenceError(f'JSON file exceeds {MAX_JSON_BYTES} bytes: {display(root, resolved)}')
    return json.loads(payload.decode('utf-8-sig'))


def sha256_and_bytes(path: Path, max_hash_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = path.stat().st_size
    if size > max_hash_bytes:
        raise OverflowError(f'File exceeds hash limit of {max_hash_bytes} bytes')
    total = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            total += len(chunk)
            if total > max_hash_bytes:
                raise OverflowError(f'File grew beyond hash limit of {max_hash_bytes} bytes')
            digest.update(chunk)
    return digest.hexdigest(), size


def verify_reference(
    root: Path,
    reference: Any,
    role: str,
    manifest: Path,
    findings: list[dict[str, Any]],
    verified: list[dict[str, Any]],
    max_hash_bytes: int,
) -> None:
    location = display(root, manifest)
    if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
        findings.append(finding('INVALID_REFERENCE', 'error', f'{role} reference has no path', location))
        return
    raw_path = reference['path']
    try:
        path = bounded_file(root, raw_path)
    except EvidenceError as error:
        if Path(raw_path).is_absolute() and 'escapes' in str(error):
            findings.append(finding('EXTERNAL_ABSOLUTE_PATH', 'error', f'Absolute reference is outside the current project root and was not read: {raw_path}', display(root, manifest), expected=raw_path))
        else:
            code = 'PATH_ESCAPE' if 'escapes' in str(error) else 'FILE_MISSING'
            findings.append(finding(code, 'error', str(error), display(root, manifest), expected=raw_path))
        return
    relative = display(root, path)
    try:
        actual_bytes = path.stat().st_size
        expected_bytes = reference.get('bytes')
        if role.startswith('output:') and expected_bytes is None:
            findings.append(finding('BYTES_MISMATCH', 'error', f'Output has no declared byte count: {relative}', relative, actual=actual_bytes, status=role))
        elif expected_bytes is not None and expected_bytes != actual_bytes:
            findings.append(finding('BYTES_MISMATCH', 'error', f'Byte count differs for {relative}', relative, expected=expected_bytes, actual=actual_bytes, status=role))
        actual_hash, _ = sha256_and_bytes(path, max_hash_bytes)
    except OverflowError:
        findings.append(finding('HASH_SKIPPED_LARGE', 'warning', f'Hash skipped above configured limit: {relative}', relative, expected=max_hash_bytes, actual=actual_bytes, status=role))
        return
    except OSError as error:
        findings.append(finding('EVIDENCE_READ_ERROR', 'error', f'Cannot read reference {relative}: {error}', relative, status=role))
        return
    expected_hash = reference.get('sha256')
    if not isinstance(expected_hash, str) or not expected_hash or expected_hash.lower() != actual_hash:
        findings.append(finding('SHA256_MISMATCH', 'error', f'SHA-256 differs or is missing for {relative}', relative, expected=expected_hash, actual=actual_hash, status=role))
    verified.append({'path': relative, 'role': role, 'bytes': actual_bytes, 'sha256': actual_hash})


def audit_authority(root: Path, paths: list[Path], findings: list[dict[str, Any]], max_hash_bytes: int) -> dict[str, Any]:
    if not paths:
        findings.append(finding('AUTHORITY_NOT_FOUND', 'warning', 'CURRENT_AUTHORITY.md was not found in the scanned project'))
        return {'found': False}
    path = paths[0]
    location = display(root, path)
    try:
        resolved = bounded_file(root, path)
        stat = resolved.stat()
        with resolved.open('r', encoding='utf-8-sig', errors='replace') as stream:
            text = stream.read(200000)
    except (EvidenceError, OSError) as error:
        code = 'PATH_ESCAPE' if 'escapes' in str(error) else 'AUTHORITY_READ_ERROR'
        findings.append(finding(code, 'error', f'Cannot read authority file: {error}', location))
        return {'found': False, 'path': location}
    title_match = re.search(r'^\s{0,3}#\s+(.+?)\s*#*\s*$', text, re.MULTILINE)
    date_match = re.search(
        r'(?:更新日期|更新时间|最近更新|last[\s_-]*updated|updated)\s*[：:]\s*'
        r'(\d{4}(?:-|/|年)\d{1,2}(?:-|/|月)\d{1,2}日?)',
        text,
        re.IGNORECASE,
    )
    size = stat.st_size
    digest = None
    try:
        digest, _ = sha256_and_bytes(resolved, max_hash_bytes)
    except OverflowError:
        findings.append(finding('HASH_SKIPPED_LARGE', 'warning', f'Authority hash skipped above configured limit: {location}', location, expected=max_hash_bytes, actual=size, status='authority'))
    except OSError as error:
        findings.append(finding('AUTHORITY_READ_ERROR', 'error', f'Cannot hash authority file: {error}', location))
    mtime_utc = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace('+00:00', 'Z')
    return {
        'found': True,
        'path': display(root, resolved),
        'title': title_match.group(1).strip() if title_match else None,
        'updated': date_match.group(1) if date_match else None,
        'bytes': size,
        'size': size,
        'mtime': stat.st_mtime,
        'mtimeUtc': mtime_utc,
        'sha256': digest,
        'additionalCandidates': [display(root, item) for item in paths[1:]],
    }


def audit_figures(root: Path, paths: list[Path], findings: list[dict[str, Any]], max_hash_bytes: int) -> dict[str, Any]:
    verified: list[dict[str, Any]] = []
    parsed = 0
    for manifest in paths:
        location = display(root, manifest)
        try:
            data = read_json(root, manifest)
        except (EvidenceError, json.JSONDecodeError, UnicodeError, OSError) as error:
            findings.append(finding('INVALID_EXPORT_JSON', 'error', str(error), location))
            continue
        if not isinstance(data, dict):
            findings.append(finding('INVALID_EXPORT_JSON', 'error', 'Export manifest must be a JSON object', location))
            continue
        parsed += 1
        references = []
        for role in ('source_data', 'cache_assets'):
            values = data.get(role, [])
            if isinstance(values, list):
                references.extend((role, value) for value in values)
            else:
                findings.append(finding('INVALID_EXPORT_JSON', 'error', f'{role} must be an array', location))
        outputs = data.get('outputs', {})
        if isinstance(outputs, dict):
            references.extend(
                (f'output:{key}', outputs[key])
                for key in ('svg', 'pdf', 'png', 'tiff')
                if key in outputs
            )
        else:
            findings.append(finding('INVALID_EXPORT_JSON', 'error', 'outputs must be an object', location))
        if not references:
            findings.append(finding('EMPTY_EXPORT', 'warning', 'Export manifest contains no file references', location))
        for role, reference in references:
            verify_reference(root, reference, role, manifest, findings, verified, max_hash_bytes)
    return {'exportManifests': len(paths), 'parsedManifests': parsed, 'verifiedReferences': len(verified), 'verifiedFiles': verified[:50]}


def read_csv(root: Path, path: Path) -> tuple[list[str], list[dict[str, str]]]:
    resolved = bounded_file(root, path)
    with resolved.open('r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def audit_claims(root: Path, paths: list[Path], findings: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    gates: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    files = []
    for path in paths:
        location = display(root, path)
        try:
            headers, rows = read_csv(root, path)
        except (EvidenceError, csv.Error, UnicodeError, OSError) as error:
            findings.append(finding('INVALID_CLAIM_CSV', 'error', str(error), location))
            continue
        for required in ('claim_id', 'proposed_claim'):
            if required not in headers:
                findings.append(finding('MISSING_CLAIM_COLUMN', 'error', f'Missing required column: {required}', location))
        if 'evidence_source_ids' not in headers:
            findings.append(finding('MISSING_CLAIM_COLUMN', 'warning', 'Missing required column: evidence_source_ids', location))
        seen: set[str] = set()
        actionable = 0
        for index, row in enumerate(rows, start=2):
            total += 1
            claim_id = (row.get('claim_id') or '').strip()
            claim = (row.get('proposed_claim') or '').strip()
            gate = (row.get('gate') or '').strip() or 'UNKNOWN'
            status = (row.get('evidence_status') or '').strip() or 'UNKNOWN'
            gates[gate] += 1
            statuses[status] += 1
            if not claim_id or not claim:
                findings.append(finding('INCOMPLETE_CLAIM', 'error', 'claim_id or proposed_claim is empty', location, actual=f'row {index}'))
            if claim_id and claim_id in seen:
                findings.append(finding('DUPLICATE_CLAIM_ID', 'error', f'Duplicate claim_id: {claim_id}', location))
            seen.add(claim_id)
            if not (row.get('evidence_source_ids') or '').strip():
                findings.append(finding('CLAIM_WITHOUT_SOURCE', 'warning', f'Claim has no evidence_source_ids: {claim_id or index}', location))
            blocked = False
            if gate.upper() in ('LOCKED', 'NO-GO', 'HOLD'):
                blocked = True
                findings.append(finding('CLAIM_GATE_BLOCKED', 'warning', f'Claim {claim_id or index} has gate {gate}', location, status=gate))
            if status.upper() in ('LOCKED', 'NO-GO', 'HOLD'):
                blocked = True
                findings.append(finding('CLAIM_STATUS_BLOCKED', 'warning', f'Claim {claim_id or index} has evidence_status {status}', location, status=status))
            if blocked:
                actionable += 1
        files.append({'path': location, 'rows': len(rows), 'actionableRows': actionable})
    return {'claimMaps': len(paths), 'claims': total, 'byGate': dict(gates), 'byEvidenceStatus': dict(statuses), 'files': files}


def audit_conflicts(root: Path, paths: list[Path], findings: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    unresolved = 0
    statuses: Counter[str] = Counter()
    files = []
    for path in paths:
        location = display(root, path)
        try:
            headers, rows = read_csv(root, path)
        except (EvidenceError, csv.Error, UnicodeError, OSError) as error:
            findings.append(finding('INVALID_CONFLICT_CSV', 'error', str(error), location))
            continue
        if 'status' not in headers:
            findings.append(finding('MISSING_CONFLICT_COLUMN', 'error', 'Missing required column: status', location))
        file_unresolved = 0
        for index, row in enumerate(rows, start=2):
            total += 1
            status = (row.get('status') or '').strip() or 'UNKNOWN'
            statuses[status] += 1
            if status.lower() not in ('resolved', 'resolved_by_definition'):
                unresolved += 1
                file_unresolved += 1
                metric = (row.get('metric') or row.get('conflict_id') or index)
                findings.append(finding('UNRESOLVED_NUMERIC_CONFLICT', 'warning', f'Unresolved numeric conflict: {metric}', location, status=status))
        files.append({'path': location, 'rows': len(rows), 'unresolvedRows': file_unresolved})
    return {'conflictRegisters': len(paths), 'conflicts': total, 'unresolved': unresolved, 'byStatus': dict(statuses), 'files': files}


def run_audit(request: dict[str, Any]) -> dict[str, Any]:
    root = project_root(request)
    checks = request.get('checks', sorted(CHECKS))
    if not isinstance(checks, list) or any(not isinstance(check, str) or check not in CHECKS for check in checks):
        raise EvidenceError('checks must be a subset of authority, figures, claims, conflicts')
    max_files = request.get('maxFiles', 20000)
    max_findings = request.get('maxFindings', 100)
    max_hash_bytes = request.get('maxHashBytes', 268435456)
    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 0:
        raise EvidenceError('maxFiles must be a non-negative integer')
    if not isinstance(max_findings, int) or isinstance(max_findings, bool) or max_findings < 0:
        raise EvidenceError('maxFindings must be a non-negative integer')
    if not isinstance(max_hash_bytes, int) or isinstance(max_hash_bytes, bool) or max_hash_bytes < 0:
        raise EvidenceError('maxHashBytes must be a non-negative integer')

    found, scanned, scan_truncated = scan(root, max_files)
    findings: list[dict[str, Any]] = []
    if scan_truncated:
        findings.append(finding('SCAN_TRUNCATED', 'warning', f'Directory scan stopped at maxFiles={max_files}; results may be incomplete'))
    details: dict[str, Any] = {}
    if 'authority' in checks:
        details['authority'] = audit_authority(root, found['authority'], findings, max_hash_bytes)
    if 'figures' in checks:
        details['figures'] = audit_figures(root, found['figures'], findings, max_hash_bytes)
    if 'claims' in checks:
        details['claims'] = audit_claims(root, found['claims'], findings)
    if 'conflicts' in checks:
        details['conflicts'] = audit_conflicts(root, found['conflicts'], findings)

    severities = Counter(item['severity'] for item in findings)
    audit_status = 'issues_found' if findings else 'clean'
    return {
        'ok': True,
        'auditStatus': audit_status,
        'projectRoot': str(root),
        'summary': {
            'checks': checks,
            'filesScanned': scanned,
            'scanTruncated': scan_truncated,
            'findings': len(findings),
            'findingsBySeverity': dict(severities),
            'returnedFindings': min(len(findings), max_findings),
            'omittedFindings': max(0, len(findings) - max_findings),
        },
        'findings': findings[:max_findings],
        'details': details,
    }


def self_test() -> dict[str, Any]:
    checks = 0
    with tempfile.TemporaryDirectory(prefix='evidence-worker-') as temporary:
        root = Path(temporary).resolve()
        (root / 'figures').mkdir()
        (root / 'data.csv').write_text('a,b\n1,2\n', encoding='utf-8')
        (root / 'figures' / 'figure.png').write_bytes(b'png')
        (root / 'CURRENT_AUTHORITY.md').write_text('# Demo\n更新日期：2026-01-01\n', encoding='utf-8')
        def sha(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {
            'source_data': [{'path': 'data.csv', 'sha256': sha(root / 'data.csv')}],
            'cache_assets': [],
            'outputs': {'png': {'path': 'figures/figure.png', 'bytes': 3, 'sha256': sha(root / 'figures' / 'figure.png')}},
        }
        (root / 'figure.export.json').write_text(json.dumps(manifest), encoding='utf-8')
        (root / 'claim_evidence_map.csv').write_text('claim_id,proposed_claim,evidence_source_ids,evidence_status,gate\nC1,Claim,S1,PASS,OPEN\n', encoding='utf-8')
        (root / 'numeric_conflict_register.csv').write_text('conflict_id,metric,canonical_value,other_value,status\nN1,m,1,2,resolved\n', encoding='utf-8')
        request = {'projectRoot': str(root), 'allowedRoots': [str(root)], 'maxHashBytes': 1024}
        result = run_audit(request)
        assert result['auditStatus'] == 'clean', result
        checks += 1
        assert result['details']['authority']['title'] == 'Demo'
        checks += 1
        assert result['details']['figures']['verifiedReferences'] == 2
        checks += 1
        (root / 'data.csv').write_text('a,b\n9,9\n', encoding='utf-8')
        changed = run_audit({**request, 'checks': ['figures']})
        assert any(item['code'] == 'SHA256_MISMATCH' for item in changed['findings'])
        checks += 1
        manifest['source_data'][0]['path'] = '../escape.csv'
        (root / 'figure.export.json').write_text(json.dumps(manifest), encoding='utf-8')
        escaped = run_audit({**request, 'checks': ['figures']})
        assert any(item['code'] == 'PATH_ESCAPE' for item in escaped['findings'])
        checks += 1
        try:
            run_audit({'projectRoot': str(root.parent), 'allowedRoots': [str(root)]})
        except EvidenceError:
            checks += 1
        else:
            raise AssertionError('Outside project root was accepted')
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_JSON_BYTES + 1)
            if len(payload) > MAX_JSON_BYTES:
                raise EvidenceError('stdin JSON exceeds the 4 MiB limit')
            try:
                request = json.loads(payload.decode('utf-8-sig'))
            except (json.JSONDecodeError, UnicodeError) as error:
                raise EvidenceError(f'Cannot parse stdin JSON: {error}') from error
            if not isinstance(request, dict):
                raise EvidenceError('Worker request must be a JSON object')
            result = run_audit(request)
    except EvidenceError as error:
        result = {'ok': False, 'error': {'code': 'EVIDENCE_INVALID_REQUEST', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'EVIDENCE_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
