#!/usr/bin/env python3
"""Corridor vector preparation and grouped statistics worker."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely import reverse
from shapely.geometry import mapping
from shapely.ops import linemerge, substring

MAX_STDIN_BYTES = 4 * 1024 * 1024
ASSET_PATTERN = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_./-]*$')


class CorridorError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise CorridorError('projectRoot is required for local files')
    if not isinstance(allowed, list) or not allowed:
        raise CorridorError('allowedRoots is required')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise CorridorError(f'Project root is not a directory: {root}')
    for item in allowed:
        if isinstance(item, str) and inside(root, Path(item).expanduser().resolve()):
            return root
    raise CorridorError(f'Project root is outside allowedRoots: {root}')


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise CorridorError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_file(root: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise CorridorError('source is required when geometryAsset is not supplied')
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not inside(path, root):
        raise CorridorError(f'Local source escapes projectRoot: {raw}')
    if not path.is_file():
        raise CorridorError(f'Local source is not a file: {raw}')
    return path


def initialize_ee(ee: Any, project: str | None) -> None:
    for attempt in range(3):
        try:
            ee.Initialize(project=project) if project else ee.Initialize()
            return
        except Exception as error:
            text = str(error).lower()
            transient = any(marker in text for marker in ('ssl', 'timeout', 'timed out', 'remote end closed', 'connection reset', 'winerror 10060'))
            if attempt == 2 or not transient:
                raise CorridorError(f'Earth Engine initialization failed: {str(error)[:2000]}') from error
            time.sleep(attempt + 1)


def load_vector(request: dict[str, Any]) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    asset = request.get('geometryAsset')
    if asset is not None:
        if not isinstance(asset, str) or not ASSET_PATTERN.fullmatch(asset) or '..' in asset:
            raise CorridorError('geometryAsset must be a valid Earth Engine FeatureCollection asset id')
        try:
            import ee
            initialize_ee(ee, request.get('geeProject'))
            payload = ee.FeatureCollection(asset).getInfo()
        except Exception as error:
            if isinstance(error, CorridorError):
                raise
            raise CorridorError(f'Cannot load Earth Engine asset {asset}: {str(error)[:2000]}') from error
        features = payload.get('features') or []
        if not features:
            raise CorridorError(f'Earth Engine asset has no features: {asset}')
        frame = gpd.GeoDataFrame.from_features(features, crs='EPSG:4326')
        return frame, {'kind': 'earth-engine-asset', 'id': asset}
    root = project_root(request)
    path = local_file(root, request.get('source'))
    try:
        frame = gpd.read_file(path)
    except Exception as error:
        raise CorridorError(f'Cannot read vector source {path}: {str(error)[:2000]}') from error
    return frame, {'kind': 'local-file', 'path': path.relative_to(root).as_posix()}


def normalize_crs(frame: gpd.GeoDataFrame, request: dict[str, Any]) -> tuple[gpd.GeoDataFrame, str, str]:
    source_crs = str(frame.crs) if frame.crs is not None else request.get('sourceCrs')
    if not source_crs:
        raise CorridorError('source CRS is missing; provide sourceCrs explicitly')
    working_crs = request.get('workingCrs', 'EPSG:32645')
    try:
        return frame.to_crs(working_crs), source_crs, working_crs
    except Exception as error:
        raise CorridorError(f'Cannot transform CRS {source_crs} -> {working_crs}: {error}') from error


def single_line(frame: gpd.GeoDataFrame) -> Any:
    geometries = [geometry for geometry in frame.geometry if geometry is not None and not geometry.is_empty]
    if not geometries:
        raise CorridorError('No non-empty geometries found')
    combined = gpd.GeoSeries(geometries).union_all()
    if combined.geom_type == 'MultiLineString':
        combined = linemerge(combined)
    if combined.geom_type != 'LineString':
        raise CorridorError(f'Expected one connected LineString, got {combined.geom_type}; repair source topology first')
    if combined.length <= 0:
        raise CorridorError('Line geometry has zero length')
    return combined


def numeric(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum or value > maximum:
        raise CorridorError(f'{name} must be a finite number from {minimum} to {maximum}')
    return value


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_vector(request: dict[str, Any]) -> dict[str, Any]:
    frame, source = load_vector(request)
    metric, source_crs, working_crs = normalize_crs(frame, request)
    combined = metric.geometry.union_all() if hasattr(metric.geometry, 'union_all') else metric.geometry.unary_union
    types = Counter(frame.geom_type.fillna('UNKNOWN'))
    result = {
        'ok': True,
        'action': 'inspect',
        'source': source,
        'featureCount': len(frame),
        'geometryTypes': dict(types),
        'sourceCrs': source_crs,
        'workingCrs': working_crs,
        'bounds': list(frame.total_bounds),
        'geometrySha256': hashlib.sha256(combined.wkb).hexdigest(),
        'emptyGeometries': int(frame.geometry.is_empty.sum()),
        'invalidGeometries': int((~frame.geometry.is_valid).sum()),
    }
    if combined.geom_type in {'LineString', 'MultiLineString'}:
        result['lengthKm'] = combined.length / 1000
    if combined.geom_type in {'Polygon', 'MultiPolygon'}:
        result['areaKm2'] = combined.area / 1_000_000
    return result


def prepare_units(request: dict[str, Any]) -> dict[str, Any]:
    frame, source = load_vector(request)
    metric, source_crs, working_crs = normalize_crs(frame, request)
    line = single_line(metric)
    direction = request.get('direction', 'original')
    if direction not in {'original', 'north_to_south', 'south_to_north'}:
        raise CorridorError('direction must be original, north_to_south, or south_to_north')
    coordinates = list(line.coords)
    if direction != 'original':
        starts_north = coordinates[0][1] >= coordinates[-1][1]
        if (direction == 'north_to_south' and not starts_north) or (direction == 'south_to_north' and starts_north):
            line = reverse(line)
    total_km = line.length / 1000
    start_km = numeric(request.get('startKm', 0), 'startKm', 0, total_km)
    end_km = numeric(request.get('endKm', total_km), 'endKm', 0.000001, total_km)
    if end_km <= start_km:
        raise CorridorError('endKm must be greater than startKm')
    if start_km > 0 or end_km < total_km:
        line = substring(line, start_km * 1000, end_km * 1000)
    segment_km = numeric(request.get('segmentKm', 10), 'segmentKm', 0.001, total_km)
    rings = request.get('rings')
    if not isinstance(rings, list) or not rings or len(rings) > 20:
        raise CorridorError('rings must contain 1-20 positive ascending distances in km')
    rings = [numeric(value, f'rings[{index}]', 0.000001, 1000) for index, value in enumerate(rings)]
    if any(later <= earlier for earlier, later in zip(rings, rings[1:])):
        raise CorridorError('rings must be strictly increasing')

    segments = []
    units = []
    cursor = start_km
    index = 0
    while cursor < end_km - 1e-9:
        index += 1
        segment_end = min(cursor + segment_km, end_km)
        geometry = substring(line, (cursor - start_km) * 1000, (segment_end - start_km) * 1000)
        segment_id = f'seg_{index:04d}'
        segments.append({
            'type': 'Feature',
            'geometry': mapping(geometry),
            'properties': {
                'segment_id': segment_id,
                'chainage_start_km': cursor,
                'chainage_end_km': segment_end,
                'segment_length_km': geometry.length / 1000,
            },
        })
        previous = 0.0
        for ring_index, outer_km in enumerate(rings, start=1):
            outer = geometry.buffer(outer_km * 1000)
            ring = outer if previous == 0 else outer.difference(geometry.buffer(previous * 1000))
            if ring.is_empty or ring.area <= 0:
                raise CorridorError(f'Empty independent ring generated for {segment_id} ring {ring_index}')
            units.append({
                'type': 'Feature',
                'geometry': mapping(ring),
                'properties': {
                    'segment_id': segment_id,
                    'ring_id': f'ring_{ring_index:02d}',
                    'ring_index': ring_index,
                    'chainage_start_km': cursor,
                    'chainage_end_km': segment_end,
                    'segment_length_km': geometry.length / 1000,
                    'ring_inner_km': previous,
                    'ring_outer_km': outer_km,
                    'ring_area_km2': ring.area / 1_000_000,
                    'working_crs': working_crs,
                },
            })
            previous = outer_km
        cursor = segment_end

    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    units_path = output_root / 'units.geojson'
    units_table_path = output_root / 'units.csv'
    segments_path = output_root / 'segments.geojson'
    manifest_path = output_root / 'manifest.json'
    units_frame = gpd.GeoDataFrame.from_features(units, crs=working_crs)
    units_frame.to_crs('EPSG:4326').to_file(units_path, driver='GeoJSON')
    units_frame.drop(columns='geometry').to_csv(units_table_path, index=False)
    gpd.GeoDataFrame.from_features(segments, crs=working_crs).to_crs('EPSG:4326').to_file(segments_path, driver='GeoJSON')
    manifest = {
        'batchId': batch_id,
        'createdUtc': utc_now(),
        'source': source,
        'sourceCrs': source_crs,
        'workingCrs': working_crs,
        'parameters': {
            'direction': direction, 'startKm': start_km, 'endKm': end_km,
            'segmentKm': segment_km, 'ringsKm': rings,
        },
        'lineSha256': hashlib.sha256(line.wkb).hexdigest(),
        'totalLineLengthKm': total_km,
        'counts': {'segments': len(segments), 'ringsPerSegment': len(rings), 'units': len(units)},
        'outputs': {
            'units': {'path': str(units_path), 'sha256': hash_file(units_path)},
            'unitsTable': {'path': str(units_table_path), 'sha256': hash_file(units_table_path)},
            'segments': {'path': str(segments_path), 'sha256': hash_file(segments_path)},
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    relative = lambda path: f"state://{path.relative_to(state_root(request)).as_posix()}"
    return {
        'ok': True,
        'action': 'prepare_units',
        'batchId': batch_id,
        'summary': manifest['counts'],
        'startKm': start_km,
        'endKm': end_km,
        'paths': {'units': str(units_path), 'unitsTable': str(units_table_path), 'segments': str(segments_path), 'manifest': str(manifest_path)},
        'statePaths': {'units': relative(units_path), 'unitsTable': relative(units_table_path), 'segments': relative(segments_path), 'manifest': relative(manifest_path)},
    }


def table_path(request: dict[str, Any]) -> Path:
    raw = request.get('table')
    if not isinstance(raw, str) or not raw.strip():
        raise CorridorError('table is required for summarize')
    if raw.startswith('state://'):
        root = state_root(request)
        path = (root / raw[len('state://'):]).resolve()
        if not inside(path, root):
            raise CorridorError('state table escapes stateRoot')
        return path
    return local_file(project_root(request), raw)


def summarize(request: dict[str, Any]) -> dict[str, Any]:
    path = table_path(request)
    if not path.is_file():
        raise CorridorError(f'Table not found: {path}')
    frame = pd.read_csv(path)
    if len(frame) > 200000:
        raise CorridorError('summarize supports at most 200000 input rows')
    group_by = request.get('groupBy', 'ring')
    column_map = {'ring': ['ring_id'], 'segment': ['segment_id'], 'both': ['segment_id', 'ring_id']}
    if group_by not in column_map:
        raise CorridorError('groupBy must be ring, segment, or both')
    group_columns = column_map[group_by]
    for column in group_columns:
        if column not in frame.columns:
            raise CorridorError(f'Missing grouping column: {column}')
    value_column = request.get('valueColumn', 'value')
    if value_column not in frame.columns:
        raise CorridorError(f'Missing value column: {value_column}')
    values = pd.to_numeric(frame[value_column], errors='coerce')
    weight_column = request.get('weightColumn')
    weights = None
    if weight_column is not None:
        if weight_column not in frame.columns:
            raise CorridorError(f'Missing weight column: {weight_column}')
        weights = pd.to_numeric(frame[weight_column], errors='coerce').fillna(0)
        if (weights < 0).any():
            raise CorridorError('weights must be non-negative')
    pixel_column = request.get('pixelCountColumn')
    pixels = None
    if pixel_column is not None:
        if pixel_column not in frame.columns:
            raise CorridorError(f'Missing pixel count column: {pixel_column}')
        pixels = pd.to_numeric(frame[pixel_column], errors='coerce').fillna(0)
    work = frame[group_columns].copy()
    work['_value'] = values
    work['_weight'] = weights if weights is not None else 1.0
    work['_pixels'] = pixels if pixels is not None else 0
    rows = []
    for keys, group in work.groupby(group_columns, dropna=False, sort=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        valid = group['_value'].dropna()
        weight_sum = float(group['_weight'].sum())
        weighted = float((valid * group.loc[valid.index, '_weight']).sum() / group.loc[valid.index, '_weight'].sum()) if len(valid) and group.loc[valid.index, '_weight'].sum() > 0 else None
        row = {column: key for column, key in zip(group_columns, keys)}
        row.update({
            'count': int(len(valid)),
            'missing': int(group['_value'].isna().sum()),
            'mean': float(valid.mean()) if len(valid) else None,
            'std': float(valid.std()) if len(valid) > 1 else None,
            'min': float(valid.min()) if len(valid) else None,
            'max': float(valid.max()) if len(valid) else None,
            'sum': float(valid.sum()) if len(valid) else 0.0,
            'weightedMean': weighted,
            'weightSum': weight_sum,
            'pixelCountSum': float(group['_pixels'].sum()),
        })
        rows.append(row)
    limit = request.get('limit', 1000)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
        raise CorridorError('limit must be an integer from 1 to 5000')
    warnings = []
    if int(work['_value'].isna().sum()) > 0:
        warnings.append('Some values were missing or non-numeric and were excluded from value statistics')
    return {
        'ok': True,
        'action': 'summarize',
        'summary': {'inputRows': len(frame), 'groups': len(rows), 'returnedGroups': min(len(rows), limit), 'omittedGroups': max(0, len(rows) - limit)},
        'warnings': warnings,
        'rows': rows[:limit],
    }


def self_test() -> dict[str, Any]:
    import tempfile
    checks = 0
    with tempfile.TemporaryDirectory(prefix='corridor-worker-') as temporary:
        base = Path(temporary)
        project = base / 'project'
        project.mkdir()
        state = base / 'state'
        source = project / 'line.geojson'
        source.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': [[84.0, 43.0], [84.1, 43.1]]}, 'properties': {}}]}), encoding='utf-8')
        common = {'allowedRoots': [str(project)], 'projectRoot': str(project), 'source': 'line.geojson', 'workingCrs': 'EPSG:32645', 'stateRoot': str(state)}
        inspected = inspect_vector(common)
        assert inspected['featureCount'] == 1 and inspected['lengthKm'] > 0
        checks += 1
        prepared = prepare_units({**common, 'segmentKm': 5, 'rings': [1, 2], 'direction': 'north_to_south'})
        assert prepared['summary']['ringsPerSegment'] == 2 and prepared['summary']['units'] == prepared['summary']['segments'] * 2
        checks += 1
        units = gpd.read_file(prepared['paths']['units'])
        assert (units['ring_area_km2'] > 0).all() and (units['ring_outer_km'] > units['ring_inner_km']).all()
        checks += 1
        table = project / 'values.csv'
        table.write_text('segment_id,ring_id,value,weight,pixels\nseg_0001,ring_01,1,1,10\nseg_0001,ring_01,3,1,20\nseg_0001,ring_02,5,2,30\n', encoding='utf-8')
        summarized = summarize({'table': str(table), 'allowedRoots': [str(project)], 'projectRoot': str(project), 'stateRoot': str(state), 'groupBy': 'ring', 'weightColumn': 'weight', 'pixelCountColumn': 'pixels'})
        assert summarized['summary']['groups'] == 2 and summarized['rows'][0]['mean'] == 2
        checks += 1
        assert summarized['rows'][0]['pixelCountSum'] == 30
        checks += 1
        try:
            prepare_units({**common, 'source': '../escape.geojson', 'segmentKm': 5, 'rings': [1]})
        except CorridorError:
            checks += 1
        else:
            raise AssertionError('Escaping source accepted')
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise CorridorError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise CorridorError('Worker request must be a JSON object')
            action = request.get('action')
            if action == 'inspect':
                result = inspect_vector(request)
            elif action == 'prepare_units':
                result = prepare_units(request)
            elif action == 'summarize':
                result = summarize(request)
            else:
                raise CorridorError(f'Unsupported action: {action}')
    except CorridorError as error:
        result = {'ok': False, 'error': {'code': 'CORRIDOR_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'CORRIDOR_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
