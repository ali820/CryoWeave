#!/usr/bin/env python3
"""Sentinel-1 archive, pair planning, product QA and reference verification."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import rasterio
from rasterio.features import shapes
from scipy.ndimage import find_objects, label as connected_components
from rasterio.warp import transform_geom
from shapely.geometry import shape

MAX_STDIN_BYTES = 4 * 1024 * 1024


class InsarError(ValueError):
    pass


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise InsarError('projectRoot is required for local files')
    if not isinstance(allowed, list) or not allowed:
        raise InsarError('allowedRoots is required')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise InsarError(f'Project root is not a directory: {root}')
    for item in allowed:
        if isinstance(item, str) and inside(root, Path(item).expanduser().resolve()):
            return root
    raise InsarError(f'Project root is outside allowedRoots: {root}')


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise InsarError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_path(root: Path, raw: Any, state: Path | None = None, allow_dir: bool = False) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise InsarError('File path must be a non-empty string')
    if raw.startswith('state://'):
        if state is None:
            raise InsarError('stateRoot is required for state:// paths')
        path = (state / raw[len('state://'):]).resolve()
        if not inside(path, state):
            raise InsarError('State file escapes stateRoot')
        return path
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if state is not None and inside(path, state):
        return path
    if not inside(path, root):
        raise InsarError(f'File escapes projectRoot: {raw}')
    if not allow_dir and not path.is_file():
        raise InsarError(f'File not found: {raw}')
    if allow_dir and not path.exists():
        raise InsarError(f'Path not found: {raw}')
    return path


def number(request: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < minimum or value > maximum:
        raise InsarError(f'{key} must be a finite number from {minimum} to {maximum}')
    return float(value)


def hash_file(path: Path, max_bytes: int) -> dict[str, Any]:
    size = path.stat().st_size
    if size > max_bytes:
        return {'path': str(path), 'bytes': size, 'skipped': True}
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'path': str(path), 'bytes': size, 'sha256': digest.hexdigest()}


def json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def parse_scene(path: Path) -> dict[str, Any]:
    date_match = re.search(r'(20\d{6})', path.name)
    track_match = re.search(r'[Tt](\d{2,3})', path.name)
    date = datetime.strptime(date_match.group(1), '%Y%m%d').date() if date_match else None
    return {'path': path, 'name': path.name, 'date': date, 'track': track_match.group(1) if track_match else 'unknown', 'bytes': path.stat().st_size}


def archive_scenes(request: dict[str, Any]) -> list[dict[str, Any]]:
    root = local_path(project_root(request), request.get('archiveRoot'), allow_dir=True)
    if not root.is_dir():
        raise InsarError(f'Archive is not a directory: {root}')
    pattern = request.get('pattern', '*.zip')
    max_files = int(number(request, 'maxFiles', 2000, 1, 100000))
    scenes = []
    for path in sorted(root.rglob(pattern)):
        if not path.is_file():
            continue
        scenes.append(parse_scene(path))
        if len(scenes) >= max_files:
            break
    if not scenes:
        raise InsarError(f'No archive files matched {pattern} under {root}')
    return scenes


def inspect_archive(request: dict[str, Any]) -> dict[str, Any]:
    scenes = archive_scenes(request)
    tracks = Counter(scene['track'] for scene in scenes)
    dates = sorted(scene['date'] for scene in scenes if scene['date'] is not None)
    names = Counter(scene['name'] for scene in scenes)
    duplicates = [{'name': name, 'count': count} for name, count in names.items() if count > 1]
    return {
        'ok': True, 'action': 'inspect',
        'summary': {
            'files': len(scenes), 'tracks': dict(tracks),
            'firstDate': dates[0].isoformat() if dates else None,
            'lastDate': dates[-1].isoformat() if dates else None,
            'totalBytes': sum(scene['bytes'] for scene in scenes),
            'duplicateNames': duplicates,
        },
        'files': [{'name': scene['name'], 'date': scene['date'].isoformat() if scene['date'] else None, 'track': scene['track'], 'bytes': scene['bytes']} for scene in scenes[:100]],
    }


def plan_pairs(request: dict[str, Any]) -> dict[str, Any]:
    scenes = archive_scenes(request)
    max_days = int(number(request, 'maxTemporalBaselineDays', 48, 1, 10000))
    max_connections = int(number(request, 'maxConnections', 4, 1, 20))
    max_pairs = int(number(request, 'maxPairs', 5000, 1, 100000))
    same_track = request.get('sameTrack', True)
    dated = sorted((scene for scene in scenes if scene['date'] is not None), key=lambda scene: scene['date'])
    pairs = []
    for index, master in enumerate(dated):
        for offset in range(1, max_connections + 1):
            if index + offset >= len(dated):
                break
            slave = dated[index + offset]
            days = (slave['date'] - master['date']).days
            if days > max_days:
                break
            if same_track and master['track'] != slave['track']:
                continue
            pairs.append({
                'master': master['name'], 'slave': slave['name'],
                'masterDate': master['date'].isoformat(), 'slaveDate': slave['date'].isoformat(),
                'days': days, 'track': master['track'],
            })
            if len(pairs) >= max_pairs:
                break
        if len(pairs) >= max_pairs:
            break
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    table_path = output_root / 'interferogram_plan.csv'
    manifest_path = output_root / 'manifest.json'
    with table_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['master', 'slave', 'masterDate', 'slaveDate', 'days', 'track'])
        writer.writeheader()
        writer.writerows(pairs)
    manifest = {
        'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'parameters': {'maxTemporalBaselineDays': max_days, 'maxConnections': max_connections, 'sameTrack': same_track},
        'sceneCount': len(dated), 'pairCount': len(pairs),
        'outputs': {'plan': hash_file(table_path, request['maxHashBytes'])},
    }
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {
        'ok': True, 'action': 'plan', 'batchId': batch_id, 'summary': manifest,
        'rows': pairs[:100], 'paths': {'plan': str(table_path), 'manifest': str(manifest_path)},
        'statePaths': {'plan': f'state://{batch_id}/interferogram_plan.csv', 'manifest': f'state://{batch_id}/manifest.json'},
    }


def analyze_products(request: dict[str, Any]) -> dict[str, Any]:
    root = project_root(request)
    velocity_path = local_path(root, request.get('velocity'))
    coherence_path = local_path(root, request.get('coherence'))
    with rasterio.open(velocity_path) as velocity_ds, rasterio.open(coherence_path) as coherence_ds:
        if velocity_ds.width != coherence_ds.width or velocity_ds.height != coherence_ds.height or velocity_ds.transform != coherence_ds.transform or velocity_ds.crs != coherence_ds.crs:
            raise InsarError('Velocity and coherence rasters must share the same grid and CRS')
        velocity = velocity_ds.read(1, masked=True).astype('float32')
        coherence = coherence_ds.read(1, masked=True).astype('float32')
        transform = velocity_ds.transform
        crs = velocity_ds.crs
    velocity_scale = number(request, 'velocityScale', 1.0, 0.000000001, 1000000)
    coherence_scale = number(request, 'coherenceScale', 1.0, 0.000000001, 1000000)
    velocity = velocity * velocity_scale
    coherence = coherence * coherence_scale
    velocity_threshold = number(request, 'velocityThreshold', 10.0, 0, 1000000)
    coherence_threshold = number(request, 'coherenceThreshold', 0.7, 0, 1)
    min_pixels = int(number(request, 'minAreaPixels', 20, 1, 1000000))
    max_clusters = int(number(request, 'maxClusters', 1000, 1, 100000))
    valid = (~np.ma.getmaskarray(velocity) & ~np.ma.getmaskarray(coherence) & np.isfinite(velocity) & np.isfinite(coherence) & (coherence >= coherence_threshold) & (np.abs(velocity) >= velocity_threshold))
    if not valid.any():
        raise InsarError('No anomalous pixels remain after thresholds')
    working = request.get('workingCrs', 'EPSG:32645')
    clusters = []
    labels, _ = connected_components(valid, structure=np.ones((3, 3), dtype=int))
    for label_id, slices in enumerate(find_objects(labels), start=1):
        region = labels[slices] == label_id
        count = int(region.sum())
        if count < min_pixels:
            continue
        region_transform = transform * rasterio.Affine.translation(slices[1].start, slices[0].start)
        for geometry, value in shapes(region.astype('uint8'), mask=region, connectivity=8, transform=region_transform):
            if value != 1:
                continue
        polygon = shape(geometry)
        if crs is not None and str(crs).upper() != working.upper():
            polygon = shape(transform_geom(crs, working, polygon))
        centroid = shape(transform_geom(working, 'EPSG:4326', polygon.centroid))
        values = np.asarray(velocity)[slices][region]
        coh_values = np.asarray(coherence)[slices][region]
        clusters.append({
            'pixelCount': count,
            'areaKm2': polygon.area / 1_000_000,
            'meanVelocity': float(values.mean()),
            'medianVelocity': float(np.median(values)),
            'minVelocity': float(values.min()),
            'maxVelocity': float(values.max()),
            'stdVelocity': float(values.std()),
            'meanCoherence': float(coh_values.mean()),
            'centroid': [centroid.x, centroid.y],
        })
    clusters.sort(key=lambda item: abs(item['meanVelocity']), reverse=True)
    clusters = clusters[:max_clusters]
    valid_values = np.asarray(velocity)[valid]
    valid_coherence = np.asarray(coherence)[valid]
    summary = {
        'validPixels': int(valid.sum()),
        'validFraction': float(valid.mean()),
        'clusterCount': len(clusters),
        'totalAnomalyAreaKm2': float(sum(item['areaKm2'] for item in clusters)),
        'meanVelocity': float(valid_values.mean()),
        'medianVelocity': float(np.median(valid_values)),
        'minVelocity': float(valid_values.min()),
        'maxVelocity': float(valid_values.max()),
        'meanCoherence': float(valid_coherence.mean()),
    }
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    table_path = output_root / 'clusters.csv'
    summary_path = output_root / 'summary.json'
    manifest_path = output_root / 'manifest.json'
    with table_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['pixelCount', 'areaKm2', 'meanVelocity', 'medianVelocity', 'minVelocity', 'maxVelocity', 'stdVelocity', 'meanCoherence', 'centroid'])
        writer.writeheader()
        for row in clusters:
            writer.writerow({**row, 'centroid': f'{row["centroid"][0]},{row["centroid"][1]}'})
    summary_path.write_text(json.dumps(json_safe(summary), ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = {
        'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'velocity': str(velocity_path), 'coherence': str(coherence_path),
        'parameters': {'velocityThreshold': velocity_threshold, 'coherenceThreshold': coherence_threshold, 'minAreaPixels': min_pixels, 'maxClusters': max_clusters, 'workingCrs': working},
        'summary': summary,
        'outputs': {'clusters': hash_file(table_path, request['maxHashBytes']), 'summary': hash_file(summary_path, request['maxHashBytes'])},
    }
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {
        'ok': True, 'action': 'analyze', 'batchId': batch_id, 'summary': summary, 'rows': clusters[:100],
        'paths': {'clusters': str(table_path), 'summary': str(summary_path), 'manifest': str(manifest_path)},
        'statePaths': {'clusters': f'state://{batch_id}/clusters.csv', 'summary': f'state://{batch_id}/summary.json', 'manifest': f'state://{batch_id}/manifest.json'},
    }


def verify_manifest(request: dict[str, Any]) -> dict[str, Any]:
    path = local_path(project_root(request), request.get('referenceManifest'))
    try:
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        raise InsarError(f'Cannot read reference manifest: {error}') from error
    expected = request.get('expectedReferenceVersion')
    actual = manifest.get('reference_version')
    findings = []
    if expected is not None and actual != expected:
        findings.append({'code': 'STALE_REFERENCE', 'severity': 'error', 'message': f'Reference version mismatch: expected {expected}, actual {actual}', 'expected': expected, 'actual': actual})
    products = manifest.get('products', [])
    if not isinstance(products, list):
        raise InsarError('reference manifest products must be an array')
    for product in products:
        if not isinstance(product, dict) or not isinstance(product.get('path'), str):
            findings.append({'code': 'INVALID_PRODUCT', 'severity': 'error', 'message': 'Product entry has no path'})
            continue
        product_path = local_path(project_root(request), product['path'], state_root(request))
        if not product_path.is_file():
            findings.append({'code': 'FILE_MISSING', 'severity': 'error', 'message': f'Missing product: {product["path"]}', 'path': product['path']})
            continue
        actual_hash = hash_file(product_path, request['maxHashBytes'])
        if actual_hash.get('skipped'):
            findings.append({'code': 'HASH_SKIPPED_LARGE', 'severity': 'warning', 'message': f'Hash skipped above limit: {product["path"]}', 'path': product['path']})
        elif product.get('sha256') and actual_hash['sha256'] != product['sha256'].lower():
            findings.append({'code': 'SHA256_MISMATCH', 'severity': 'error', 'message': f'SHA-256 mismatch: {product["path"]}', 'path': product['path'], 'expected': product['sha256'].lower(), 'actual': actual_hash['sha256']})
    return {
        'ok': True, 'action': 'verify', 'auditStatus': 'issues_found' if findings else 'clean',
        'staleReference': any(item['code'] == 'STALE_REFERENCE' for item in findings),
        'referenceVersion': actual, 'expectedReferenceVersion': expected,
        'summary': {'products': len(products), 'findings': len(findings)},
        'findings': findings,
    }


def self_test() -> dict[str, Any]:
    import tempfile
    checks = 0
    with tempfile.TemporaryDirectory(prefix='insar-worker-') as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        state = root / 'state'
        for name in ['S1A_IW_SLC_20230101_T84.zip', 'S1A_IW_SLC_20230105_T84.zip', 'S1A_IW_SLC_20230120_T84.zip', 'S1A_IW_SLC_20230201_T85.zip']:
            (project / name).write_bytes(b'x')
        inspected = inspect_archive({'archiveRoot': '.', 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state)})
        assert inspected['summary']['files'] == 4
        checks += 1
        planned = plan_pairs({'archiveRoot': '.', 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'maxTemporalBaselineDays': 20, 'maxConnections': 2, 'maxHashBytes': 1000000})
        assert planned['summary']['pairCount'] == 3
        checks += 1
        velocity = np.zeros((20, 20), dtype='float32')
        coherence = np.full((20, 20), 0.9, dtype='float32')
        velocity[2:6, 2:6] = 20
        velocity[12:16, 12:16] = -15
        coherence[0, 0] = 0.1
        for name, values in [('velocity', velocity), ('coherence', coherence)]:
            with rasterio.open(project / f'{name}.tif', 'w', driver='GTiff', width=20, height=20, count=1, dtype='float32', crs='EPSG:32645', transform=rasterio.Affine(100, 0, 0, 0, -100, 0), nodata=-9999) as target:
                target.write(values, 1)
        analyzed = analyze_products({'velocity': 'velocity.tif', 'coherence': 'coherence.tif', 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'velocityThreshold': 10, 'coherenceThreshold': 0.7, 'minAreaPixels': 4, 'maxHashBytes': 1000000})
        assert analyzed['summary']['clusterCount'] == 2
        checks += 1
        manifest = project / 'reference.json'
        manifest.write_text(json.dumps({'reference_version': 'old', 'products': [{'path': 'velocity.tif', 'sha256': hash_file(project / 'velocity.tif', 1000000)['sha256']}, {'path': 'coherence.tif', 'sha256': hash_file(project / 'coherence.tif', 1000000)['sha256']}]}), encoding='utf-8')
        verified = verify_manifest({'referenceManifest': 'reference.json', 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'expectedReferenceVersion': 'new', 'maxHashBytes': 1000000})
        assert verified['staleReference'] is True
        checks += 1
        try:
            analyze_products({'velocity': '../escape.tif', 'coherence': 'coherence.tif', 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state)})
        except InsarError:
            checks += 1
        else:
            raise AssertionError('Escaping product accepted')
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise InsarError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise InsarError('Worker request must be a JSON object')
            max_hash = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash, int) or isinstance(max_hash, bool) or not 1024 <= max_hash <= 2147483648:
                raise InsarError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash
            action = request.get('action')
            if action == 'inspect':
                result = inspect_archive(request)
            elif action == 'plan':
                result = plan_pairs(request)
            elif action == 'analyze':
                result = analyze_products(request)
            elif action == 'verify':
                result = verify_manifest(request)
            else:
                raise InsarError(f'Unsupported action: {action}')
    except InsarError as error:
        result = {'ok': False, 'error': {'code': 'INSAR_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'INSAR_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(json_safe(result), ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
