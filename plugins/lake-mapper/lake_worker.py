#!/usr/bin/env python3
"""Lake water extraction and morphology analysis worker."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes
from rasterio.warp import transform_geom
from shapely.geometry import shape

MAX_STDIN_BYTES = 4 * 1024 * 1024
ROLES = {'mask', 'green', 'nir', 'swir1'}


class LakeError(ValueError):
    pass


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise LakeError('projectRoot is required for local files')
    if not isinstance(allowed, list) or not allowed:
        raise LakeError('allowedRoots is required')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise LakeError(f'Project root is not a directory: {root}')
    for item in allowed:
        if isinstance(item, str) and inside(root, Path(item).expanduser().resolve()):
            return root
    raise LakeError(f'Project root is outside allowedRoots: {root}')


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise LakeError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_file(root: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise LakeError('File path must be a non-empty string')
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not inside(path, root):
        raise LakeError(f'File escapes projectRoot: {raw}')
    if not path.is_file():
        raise LakeError(f'File not found: {raw}')
    return path


def sources(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = request.get('inputs')
    if not isinstance(raw, dict) or not raw:
        raise LakeError('inputs must map roles to raster paths or {path,band} objects')
    root = project_root(request)
    result = {}
    for role, value in raw.items():
        if role not in ROLES:
            raise LakeError(f'Unsupported raster role: {role}')
        if isinstance(value, str):
            value = {'path': value}
        if not isinstance(value, dict):
            raise LakeError(f'Input {role} must be a path or object')
        path = local_file(root, value.get('path'))
        band = value.get('band', 1)
        if not isinstance(band, int) or isinstance(band, bool) or band < 1:
            raise LakeError(f'Input {role} band must be a positive integer')
        result[role] = {'path': path, 'band': band}
    return result


def number(request: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < minimum or value > maximum:
        raise LakeError(f'{key} must be a finite number from {minimum} to {maximum}')
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


def source_path(request: dict[str, Any]) -> Path:
    raw = request.get('source')
    if not isinstance(raw, str) or not raw.strip():
        raise LakeError('source is required for vector morphology')
    if raw.startswith('state://'):
        root = state_root(request)
        path = (root / raw[len('state://'):]).resolve()
        if not inside(path, root):
            raise LakeError('Vector source escapes stateRoot')
        return path
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        state = state_root(request)
        if inside(resolved, state):
            return resolved
    return local_file(project_root(request), raw)


def inspect(request: dict[str, Any]) -> dict[str, Any]:
    if request.get('source') is not None:
        path = source_path(request)
        frame = gpd.read_file(path)
        working = request.get('workingCrs', 'EPSG:32645')
        if frame.crs is None and not request.get('sourceCrs'):
            raise LakeError('Vector CRS is missing; provide sourceCrs')
        metric = frame.to_crs(working)
        combined = metric.geometry.union_all()
        return {
            'ok': True, 'action': 'inspect', 'kind': 'vector', 'path': str(path),
            'featureCount': len(frame), 'geometryTypes': dict(frame.geom_type.value_counts()),
            'crs': str(frame.crs) if frame.crs else request.get('sourceCrs'),
            'workingCrs': working, 'bounds': list(frame.total_bounds),
            'areaKm2': combined.area / 1_000_000 if combined.geom_type in {'Polygon', 'MultiPolygon'} else None,
        }
    result = []
    for role, source in sources(request).items():
        with rasterio.open(source['path']) as dataset:
            result.append({
                'role': role, 'path': str(source['path']), 'band': source['band'],
                'width': dataset.width, 'height': dataset.height, 'bandCount': dataset.count,
                'dtype': dataset.dtypes[source['band'] - 1], 'crs': str(dataset.crs) if dataset.crs else None,
                'transform': list(dataset.transform), 'bounds': list(dataset.bounds), 'nodata': dataset.nodata,
            })
    return {'ok': True, 'action': 'inspect', 'kind': 'raster', 'sources': result}


def axis_lengths(geometry: Any) -> tuple[float, float]:
    rectangle = geometry.minimum_rotated_rectangle
    coordinates = list(rectangle.exterior.coords)
    lengths = [math.dist(coordinates[index], coordinates[index + 1]) for index in range(len(coordinates) - 1)]
    return max(lengths) / 1000, min(lengths) / 1000


def metric_values(geometry: Any, working_crs: str) -> dict[str, Any]:
    area_m2 = geometry.area
    perimeter_m = geometry.length
    if area_m2 <= 0 or perimeter_m <= 0:
        raise LakeError('Polygon has zero area or perimeter')
    centroid = shape(transform_geom(working_crs, 'EPSG:4326', geometry.centroid))
    point = shape(transform_geom(working_crs, 'EPSG:4326', geometry.representative_point()))
    envelope = shape(transform_geom(working_crs, 'EPSG:4326', geometry.envelope))
    major, minor = axis_lengths(geometry)
    fd = 2 * math.log(perimeter_m) / math.log(area_m2) if area_m2 > 1 and perimeter_m > 1 else None
    return {
        'area_km2': area_m2 / 1_000_000,
        'perimeter_km': perimeter_m / 1000,
        'centroid': [centroid.x, centroid.y],
        'representativePoint': [point.x, point.y],
        'bounds': list(envelope.bounds),
        'majorAxisKm': major,
        'minorAxisKm': minor,
        'elongation': major / minor if minor > 0 else None,
        'compactness': 4 * math.pi * area_m2 / (perimeter_m * perimeter_m),
        'shapeIndex': perimeter_m / (2 * math.sqrt(math.pi * area_m2)),
        'fractalDimension': fd,
    }


def morphology_features(frame: gpd.GeoDataFrame, request: dict[str, Any]) -> list[dict[str, Any]]:
    working = request.get('workingCrs', 'EPSG:32645')
    metric = frame.to_crs(working)
    min_area = number(request, 'minAreaKm2', 0.001, 0, 1_000_000_000)
    simplify = number(request, 'simplifyMeters', 0, 0, 100000)
    rows = []
    for source_index, geometry in enumerate(metric.geometry):
        if geometry is None or geometry.is_empty or geometry.geom_type not in {'Polygon', 'MultiPolygon'}:
            continue
        if simplify > 0:
            geometry = geometry.simplify(simplify, preserve_topology=True)
        metrics = metric_values(geometry, working)
        if metrics['area_km2'] < min_area:
            continue
        source_properties = frame.iloc[source_index].drop(labels='geometry').to_dict()
        rows.append({'geometry': geometry, 'metrics': metrics, 'sourceProperties': source_properties})
    rows.sort(key=lambda item: item['metrics']['area_km2'], reverse=True)
    if request.get('keepLargest', False):
        rows = rows[:1]
    limit = request.get('limit', 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise LakeError('limit must be an integer from 1 to 1000')
    return rows[:limit]


def output_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index, item in enumerate(rows, start=1):
        metrics = item['metrics']
        source_properties = item['sourceProperties']
        name = next((source_properties[key] for key in ('lake', 'name', 'lake_id', 'Hylak_id') if source_properties.get(key) is not None), f'lake_{index:04d}')
        result.append({'lake_id': name, **metrics, 'sourceProperties': source_properties})
    return result


def write_outputs(request: dict[str, Any], rows: list[dict[str, Any]], input_files: list[Path], extra: dict[str, Any]) -> dict[str, Any]:
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    vector_path = output_root / 'water_polygons.geojson'
    table_path = output_root / 'morphology.csv'
    manifest_path = output_root / 'manifest.json'
    table_rows = output_rows(rows)
    features = []
    for item in table_rows:
        properties = {key: value for key, value in item.items() if key not in {'sourceProperties', 'centroid', 'representativePoint', 'bounds'}}
        properties['centroid_x'] = item['centroid'][0]
        properties['centroid_y'] = item['centroid'][1]
        properties['representative_x'] = item['representativePoint'][0]
        properties['representative_y'] = item['representativePoint'][1]
        properties['bounds'] = item['bounds']
        properties.update(item['sourceProperties'])
        features.append({'type': 'Feature', 'geometry': item['sourceProperties'].pop('_geometry', None), 'properties': properties})
    # Attach geometries separately to keep tabular rows independent of output schema.
    for feature, item in zip(features, rows):
        feature['geometry'] = item['geometry']
    gpd.GeoDataFrame.from_features(features, crs=request.get('workingCrs', 'EPSG:32645')).to_crs('EPSG:4326').to_file(vector_path, driver='GeoJSON')
    fieldnames = ['lake_id', 'area_km2', 'perimeter_km', 'majorAxisKm', 'minorAxisKm', 'elongation', 'compactness', 'shapeIndex', 'fractalDimension']
    with table_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in table_rows:
            writer.writerow({key: row[key] for key in fieldnames})
    manifest = {
        'batchId': batch_id,
        'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'counts': {'polygons': len(rows), 'totalAreaKm2': sum(item['metrics']['area_km2'] for item in rows)},
        'inputs': [hash_file(path, request['maxHashBytes']) for path in input_files],
        'parameters': {key: request.get(key) for key in ('workingCrs', 'minAreaKm2', 'simplifyMeters', 'keepLargest', 'limit')},
        'metrics': table_rows,
        'outputs': {
            'waterPolygons': hash_file(vector_path, request['maxHashBytes']),
            'morphology': hash_file(table_path, request['maxHashBytes']),
        },
        **extra,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default), encoding='utf-8')
    relative = lambda path: f"state://{path.relative_to(state_root(request)).as_posix()}"
    return {
        'ok': True,
        'batchId': batch_id,
        'summary': manifest['counts'],
        'rows': table_rows[:100],
        'paths': {'waterPolygons': str(vector_path), 'morphology': str(table_path), 'manifest': str(manifest_path)},
        'statePaths': {'waterPolygons': relative(vector_path), 'morphology': relative(table_path), 'manifest': relative(manifest_path)},
    }


def morphology(request: dict[str, Any]) -> dict[str, Any]:
    path = source_path(request)
    frame = gpd.read_file(path)
    if frame.crs is None and not request.get('sourceCrs'):
        raise LakeError('Vector CRS is missing; provide sourceCrs')
    if frame.crs is None:
        frame = frame.set_crs(request['sourceCrs'])
    rows = morphology_features(frame, request)
    return write_outputs(request, rows, [path], {'source': str(path), 'action': 'morphology'})


def extract(request: dict[str, Any]) -> dict[str, Any]:
    source_map = sources(request)
    method = request.get('method', 'mndwi')
    if method not in {'mask', 'mndwi', 'ndwi'}:
        raise LakeError('method must be mask, mndwi, or ndwi')
    loaded = {}
    grid = None
    for role, source in source_map.items():
        with rasterio.open(source['path']) as dataset:
            if source['band'] > dataset.count:
                raise LakeError(f'Band {source["band"]} does not exist in {source["path"]}')
            data = dataset.read(source['band'], masked=True).astype('float32')
            metadata = {'transform': dataset.transform, 'crs': dataset.crs, 'width': dataset.width, 'height': dataset.height, 'nodata': dataset.nodata}
            if grid is None:
                grid = metadata
            elif metadata['transform'] != grid['transform'] or metadata['crs'] != grid['crs'] or metadata['width'] != grid['width'] or metadata['height'] != grid['height']:
                raise LakeError('All extraction rasters must share the same grid and CRS')
            loaded[role] = data
    scale = number(request, 'reflectanceScale', 1.0, 0.000000001, 1000000)
    offset = number(request, 'reflectanceOffset', 0.0, -1000000, 1000000)
    for role in ('green', 'nir', 'swir1'):
        if role in loaded:
            loaded[role] = loaded[role] * scale + offset
    if method == 'mask':
        if 'mask' not in loaded:
            raise LakeError('method=mask requires a mask input')
        values = request.get('waterValues', [1])
        if not isinstance(values, list) or not values:
            raise LakeError('waterValues must be a non-empty array')
        candidate = np.isin(np.asarray(loaded['mask']), values)
    else:
        required = ('green', 'swir1') if method == 'mndwi' else ('green', 'nir')
        if any(role not in loaded for role in required):
            raise LakeError(f'{method} requires {required[0]} and {required[1]}')
        with np.errstate(divide='ignore', invalid='ignore'):
            index = (loaded[required[0]] - loaded[required[1]]) / (loaded[required[0]] + loaded[required[1]])
        threshold = number(request, 'threshold', 0.0, -1000000, 1000000)
        operator = request.get('operator', 'gt')
        if operator not in {'gt', 'ge', 'lt', 'le'}:
            raise LakeError('operator must be gt, ge, lt, or le')
        if operator == 'gt':
            candidate = index > threshold
        elif operator == 'ge':
            candidate = index >= threshold
        elif operator == 'lt':
            candidate = index < threshold
        else:
            candidate = index <= threshold
    valid = np.ones(candidate.shape, dtype=bool)
    for data in loaded.values():
        valid &= ~np.ma.getmaskarray(data) & np.isfinite(data)
    mask_nodata = request.get('maskNodata')
    if mask_nodata is not None:
        for data in loaded.values():
            valid &= np.asarray(data) != mask_nodata
    candidate &= valid
    if not candidate.any():
        raise LakeError('No water pixels remain after threshold and validity masks')
    connectivity = int(number(request, 'connectivity', 8, 4, 8))
    if connectivity not in {4, 8}:
        raise LakeError('connectivity must be 4 or 8')
    working = request.get('workingCrs', 'EPSG:32645')
    geometries = []
    for geometry, value in shapes(candidate.astype('uint8'), mask=valid, connectivity=connectivity, transform=grid['transform']):
        if value != 1:
            continue
        if grid['crs'] is None:
            raise LakeError('Raster CRS is required for metric morphology')
        geometries.append(transform_geom(grid['crs'], working, geometry))
    frame = gpd.GeoDataFrame.from_features([{'type': 'Feature', 'geometry': geometry, 'properties': {}} for geometry in geometries], crs=working)
    before_filter = len(frame)
    rows = morphology_features(frame, request)
    extra = {
        'action': 'extract', 'method': method,
        'waterPixels': int(candidate.sum()), 'validPixels': int(valid.sum()),
        'waterFraction': float(candidate.sum() / valid.sum()) if valid.sum() else None,
        'polygonsBeforeFilter': before_filter,
    }
    return write_outputs(request, rows, [source['path'] for source in source_map.values()], extra)


def self_test() -> dict[str, Any]:
    import tempfile
    checks = 0
    with tempfile.TemporaryDirectory(prefix='lake-worker-') as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        state = root / 'state'
        mask = np.zeros((40, 40), dtype='uint8')
        mask[5:20, 5:20] = 1
        mask[25:35, 25:35] = 1
        mask[0, 0] = 1
        path = project / 'mask.tif'
        transform = rasterio.Affine(100, 0, 0, 0, -100, 0)
        with rasterio.open(path, 'w', driver='GTiff', width=40, height=40, count=1, dtype='uint8', crs='EPSG:32645', transform=transform, nodata=0) as target:
            target.write(mask, 1)
        request = {'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'inputs': {'mask': 'mask.tif'}, 'method': 'mask', 'workingCrs': 'EPSG:32645', 'minAreaKm2': 0.02, 'maxHashBytes': 1000000}
        result = extract(request)
        assert result['summary']['polygons'] == 2
        checks += 1
        assert result['summary']['totalAreaKm2'] > 0
        checks += 1
        morph = morphology({**request, 'source': result['paths']['waterPolygons']})
        assert morph['summary']['polygons'] == 2
        checks += 1
        assert all(row['shapeIndex'] >= 1 for row in morph['rows'])
        checks += 1
        try:
            extract({**request, 'inputs': {'mask': '../escape.tif'}})
        except LakeError:
            checks += 1
        else:
            raise AssertionError('Escaping raster accepted')
        assert inspect(request)['kind'] == 'raster'
        checks += 1
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise LakeError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise LakeError('Worker request must be a JSON object')
            max_hash = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash, int) or isinstance(max_hash, bool) or not 1024 <= max_hash <= 2147483648:
                raise LakeError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash
            action = request.get('action')
            if action == 'inspect':
                result = inspect(request)
            elif action == 'extract':
                result = extract(request)
            elif action == 'morphology':
                result = morphology(request)
            else:
                raise LakeError(f'Unsupported action: {action}')
    except LakeError as error:
        result = {'ok': False, 'error': {'code': 'LAKE_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'LAKE_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False, default=json_default).encode('utf-8'))


if __name__ == '__main__':
    main()
