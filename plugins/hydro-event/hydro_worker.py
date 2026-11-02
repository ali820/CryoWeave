#!/usr/bin/env python3
"""Hypsometry, storage interpolation, water balance and breach scenarios."""
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
from xml.etree import ElementTree

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import calculate_default_transform, reproject
from shapely.geometry import shape

MAX_STDIN_BYTES = 4 * 1024 * 1024


class HydroError(ValueError):
    pass


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise HydroError('projectRoot is required for local files')
    if not isinstance(allowed, list) or not allowed:
        raise HydroError('allowedRoots is required')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise HydroError(f'Project root is not a directory: {root}')
    for item in allowed:
        if isinstance(item, str) and inside(root, Path(item).expanduser().resolve()):
            return root
    raise HydroError(f'Project root is outside allowedRoots: {root}')


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise HydroError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_path(root: Path, raw: Any, state: Path | None = None) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise HydroError('File path must be a non-empty string')
    if raw.startswith('state://'):
        if state is None:
            raise HydroError('stateRoot is required for state:// paths')
        path = (state / raw[len('state://'):]).resolve()
        if not inside(path, state):
            raise HydroError('State file escapes stateRoot')
        return path
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if state is not None and inside(path, state):
        return path
    if not inside(path, root):
        raise HydroError(f'File escapes projectRoot: {raw}')
    if not path.is_file():
        raise HydroError(f'File not found: {raw}')
    return path


def validate_raster_source(path: Path, root: Path, seen: set[Path] | None = None) -> None:
    """Keep local VRT inputs from dereferencing files outside the allowed root."""
    if path.suffix.lower() != '.vrt':
        return
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        raise HydroError('VRT source cycle detected')
    seen = seen | {path}
    try:
        document = ElementTree.parse(path)
    except ElementTree.ParseError as error:
        raise HydroError(f'Invalid VRT XML: {path.name}: {error}') from error
    for element in document.iter('SourceFilename'):
        raw = (element.text or '').strip()
        if not raw:
            continue
        if raw.lower().startswith('/vsi') or '://' in raw:
            raise HydroError(f'VRT remote or virtual source is not allowed: {raw}')
        candidate = Path(raw).expanduser()
        base = path.parent if element.get('relativeToVRT') == '1' else Path.cwd()
        source = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
        if not inside(source, root):
            raise HydroError(f'VRT source escapes projectRoot: {raw}')
        if not source.is_file():
            raise HydroError(f'VRT source not found: {raw}')
        validate_raster_source(source, root, seen)


def number(request: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < minimum or value > maximum:
        raise HydroError(f'{key} must be a finite number from {minimum} to {maximum}')
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


def table_path(request: dict[str, Any], key: str) -> Path:
    return local_path(project_root(request), request.get(key), state_root(request))


def hypsometry(request: dict[str, Any]) -> dict[str, Any]:
    root = project_root(request)
    source = local_path(root, request.get('source'))
    validate_raster_source(source, root)
    working = request.get('workingCrs', 'EPSG:32645')
    with rasterio.open(source) as dataset:
        if dataset.crs is None:
            raise HydroError('DEM CRS is required')
        transform, width, height = calculate_default_transform(dataset.crs, working, dataset.width, dataset.height, *dataset.bounds)
        data = dataset.read(1, masked=True)
        source_crs = dataset.crs
        active_transform = dataset.transform
        if str(source_crs).upper() != working.upper():
            destination = np.full((height, width), np.nan, dtype='float32')
            reproject(
                source=data,
                destination=destination,
                src_transform=dataset.transform,
                src_crs=source_crs,
                src_nodata=dataset.nodata,
                dst_transform=transform,
                dst_crs=working,
                dst_nodata=np.nan,
                resampling=1,
            )
            data = np.ma.masked_invalid(destination)
            active_transform = transform
        mask_path = request.get('mask')
        if mask_path:
            vector_path = local_path(root, mask_path)
            vector = gpd.read_file(vector_path)
            if vector.crs is None:
                raise HydroError('Mask vector CRS is required')
            from rasterio.features import geometry_mask
            from shapely.geometry import mapping
            geometries = [mapping(geom) for geom in vector.to_crs(working).geometry if geom is not None and not geom.is_empty]
            selected = geometry_mask(geometries, out_shape=data.shape, transform=active_transform, invert=True)
            data = np.ma.masked_array(data, mask=np.ma.getmaskarray(data) | ~selected)
        cell_area = abs(active_transform.a * active_transform.e)
    values = np.asarray(data)[~np.ma.getmaskarray(data) & np.isfinite(data)]
    if values.size == 0:
        raise HydroError('DEM has no valid elevation values after masking')
    step = number(request, 'elevationStepM', 1.0, 0.000001, 10000)
    minimum = number(request, 'minElevation', float(values.min()), -100000, 100000)
    maximum = number(request, 'maxElevation', float(values.max()), -100000, 100000)
    if maximum <= minimum:
        raise HydroError('maxElevation must be greater than minElevation')
    levels = list(np.arange(minimum, maximum, step))
    if not levels or levels[-1] < maximum:
        levels.append(maximum)
    rows = [{
        'elev_m': float(minimum),
        'area_below_km2': float((values <= minimum).sum() * cell_area / 1_000_000),
        'volume_km3': 0.0,
        'bin_area_km2': 0.0,
    }]
    previous = minimum
    for level in levels[1:]:
        below = values <= level
        in_bin = (values > previous) & below
        prior = rows[-1]
        rows.append({
            'elev_m': float(level),
            'area_below_km2': float(below.sum() * cell_area / 1_000_000),
            'bin_area_km2': float(in_bin.sum() * cell_area / 1_000_000),
            'volume_km3': prior['volume_km3'] + (prior['area_below_km2'] + float(below.sum() * cell_area / 1_000_000)) / 2 * (level - prior['elev_m']) / 1000,
        })
        previous = level
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    table_path = output_root / 'hypsometry.csv'
    manifest_path = output_root / 'manifest.json'
    with table_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['elev_m', 'area_below_km2', 'volume_km3', 'bin_area_km2'])
        writer.writeheader()
        writer.writerows(rows)
    manifest = {
        'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'source': str(source), 'workingCrs': working, 'cellAreaM2': cell_area,
        'validPixels': int(values.size), 'elevationStepM': step,
        'counts': {'levels': len(rows), 'areaBelowMaxKm2': rows[-1]['area_below_km2'], 'volumeMaxKm3': rows[-1]['volume_km3']},
        'inputs': [hash_file(source, request['maxHashBytes'])],
        'outputs': {'hypsometry': hash_file(table_path, request['maxHashBytes'])},
    }
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {
        'ok': True, 'action': 'hypsometry', 'batchId': batch_id, 'summary': manifest['counts'],
        'rows': rows[:100], 'paths': {'hypsometry': str(table_path), 'manifest': str(manifest_path)},
        'statePaths': {'hypsometry': f'state://{batch_id}/hypsometry.csv', 'manifest': f'state://{batch_id}/manifest.json'},
    }


def read_hypsometry(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    frame = pd.read_csv(path)
    required = {'elev_m', 'area_below_km2', 'volume_km3'}
    if not required.issubset(frame.columns):
        raise HydroError('Hypsometry table requires elev_m, area_below_km2 and volume_km3')
    frame = frame.sort_values('elev_m')
    return frame['elev_m'].to_numpy(), frame['area_below_km2'].to_numpy(), frame['volume_km3'].to_numpy()


def storage(request: dict[str, Any]) -> dict[str, Any]:
    path = table_path(request, 'hypsometry')
    levels, areas, volumes = read_hypsometry(path)
    level = number(request, 'levelM', levels.mean(), -100000, 100000)
    clamped = level < levels[0] or level > levels[-1]
    return {
        'ok': True, 'action': 'storage', 'levelM': level,
        'areaKm2': float(np.interp(level, levels, areas)),
        'volumeKm3': float(np.interp(level, levels, volumes)),
        'clamped': bool(clamped),
        'range': {'minLevelM': float(levels[0]), 'maxLevelM': float(levels[-1])},
    }


def water_balance(request: dict[str, Any]) -> dict[str, Any]:
    path = table_path(request, 'components')
    frame = pd.read_csv(path)
    required = {'term', 'central_km3'}
    if not required.issubset(frame.columns):
        raise HydroError('Water-balance components require term and central_km3')
    for column in ('central_km3', 'lo_km3', 'hi_km3'):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors='coerce')
    central = pd.to_numeric(frame['central_km3'], errors='coerce').fillna(0)
    outcome_term = request.get('outcomeTerm')
    outcome_mask = frame['term'] == outcome_term if outcome_term is not None else pd.Series(False, index=frame.index)
    if outcome_term is not None and not outcome_mask.any():
        raise HydroError(f'outcomeTerm not found in components: {outcome_term}')
    non_source_terms = request.get('nonSourceTerms', [])
    if not isinstance(non_source_terms, list) or any(not isinstance(item, str) for item in non_source_terms):
        raise HydroError('nonSourceTerms must be a string array')
    non_source_mask = frame['term'].apply(lambda value: any(str(value) == term or str(value).startswith(term + '（') or str(value).startswith(term + '(') for term in non_source_terms))
    if non_source_terms and not non_source_mask.any():
        raise HydroError(f'nonSourceTerms not found in components: {non_source_terms}')
    central = central[~outcome_mask]
    effective = central.where(~non_source_mask, -central)
    positive = float(central[central > 0].sum())
    negative = float(central[central < 0].sum())
    net = float(effective.sum())
    outcome = request.get('outcomeKm3')
    if outcome is None and outcome_term is not None:
        outcome = float(pd.to_numeric(frame.loc[outcome_mask, 'central_km3'], errors='coerce').iloc[0])
    if outcome is not None:
        outcome = number(request, 'outcomeKm3', outcome, -1000000, 1000000)
    result = {
        'ok': True, 'action': 'water_balance',
        'summary': {
            'positiveTermsKm3': positive, 'negativeTermsKm3': negative, 'netInputKm3': net,
            'nonSourceTermsKm3': float(central[non_source_mask].sum()),
            'outcomeKm3': outcome,
            'closureRatio': net / outcome if outcome not in {None, 0} else None,
            'residualKm3': outcome - net if outcome is not None else None,
        },
        'components': frame.to_dict('records'),
    }
    for key in ('lo_km3', 'hi_km3'):
        if key in frame:
            values = frame[key].fillna(0)[~outcome_mask]
            effective_values = values.where(~non_source_mask, -values)
            result['summary'][key] = {
                'positiveTermsKm3': float(values[values > 0].sum()),
                'negativeTermsKm3': float(values[values < 0].sum()),
                'netInputKm3': float(effective_values.sum()),
            }
    return result


def breach(request: dict[str, Any]) -> dict[str, Any]:
    path = table_path(request, 'hypsometry')
    levels, areas, volumes = read_hypsometry(path)
    pre = number(request, 'preLevelM', levels[0], -100000, 100000)
    post = number(request, 'postLevelM', levels[-1], -100000, 100000)
    duration = number(request, 'durationDays', 1, 0.000001, 100000)
    pre_volume = float(np.interp(pre, levels, volumes))
    post_volume = float(np.interp(post, levels, volumes))
    release = pre_volume - post_volume
    if release < 0:
        raise HydroError('postLevelM storage exceeds preLevelM storage; release would be negative')
    result = {
        'ok': True, 'action': 'breach', 'scenarioOnly': True,
        'preLevelM': pre, 'postLevelM': post,
        'preStorageKm3': pre_volume, 'postStorageKm3': post_volume,
        'releaseKm3': release, 'durationDays': duration,
        'meanOutflowM3s': release * 1e9 / (duration * 86400),
    }
    width = request.get('widthM')
    coefficient = request.get('dischargeCoefficient')
    head = request.get('headM')
    if width is not None or coefficient is not None or head is not None:
        if width is None or coefficient is None or head is None:
            raise HydroError('widthM, dischargeCoefficient and headM must be supplied together for a weir scenario')
        width = number(request, 'widthM', 0, 0.000001, 1000000)
        coefficient = number(request, 'dischargeCoefficient', 1.0, 0.000001, 100)
        head = number(request, 'headM', 0, 0.000001, 100000)
        result['weirScenario'] = {
            'widthM': width, 'dischargeCoefficient': coefficient, 'headM': head,
            'peakOutflowM3s': coefficient * width * head ** 1.5,
            'formula': 'Q = C_d * width * head^1.5',
        }
    return result


def self_test() -> dict[str, Any]:
    import tempfile
    from rasterio.transform import from_origin
    from shapely.geometry import box
    checks = 0
    with tempfile.TemporaryDirectory(prefix='hydro-worker-') as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        state = root / 'state'
        dem = project / 'dem.tif'
        values = np.arange(100, dtype='float32').reshape(10, 10)
        with rasterio.open(dem, 'w', driver='GTiff', width=10, height=10, count=1, dtype='float32', crs='EPSG:32645', transform=rasterio.Affine(100, 0, 0, 0, -100, 0), nodata=-9999) as target:
            target.write(values, 1)
        result = hypsometry({'source': str(dem), 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'workingCrs': 'EPSG:32645', 'elevationStepM': 10, 'maxHashBytes': 1000000})
        # Independent reference: 100 cells of 100 m x 100 m cover 1 km².
        assert result['summary']['levels'] == 11 and abs(result['summary']['areaBelowMaxKm2'] - 1.0) < 1e-12
        checks += 1
        baseline, first = result['rows'][:2]
        # At 0 m one cell is at/below the baseline; at 10 m there are 11 cells.
        assert baseline == {'elev_m': 0.0, 'area_below_km2': 0.01, 'bin_area_km2': 0.0, 'volume_km3': 0.0}
        assert first['elev_m'] == 10.0 and abs(first['area_below_km2'] - 0.11) < 1e-12
        checks += 1
        # First-layer trapezoid: (0.01 + 0.11) / 2 km² * 10 m / 1000.
        assert abs(first['volume_km3'] - 0.0006) < 1e-12
        checks += 1
        stored = storage({'hypsometry': result['paths']['hypsometry'], 'levelM': 10, 'stateRoot': str(state), 'projectRoot': str(project), 'allowedRoots': [str(project)]})
        assert abs(stored['volumeKm3'] - 0.0006) < 1e-12
        checks += 1
        components = project / 'components.csv'
        components.write_text('term,central_km3,lo_km3,hi_km3\ninflow,1,0.5,1.5\noutflow,-0.2,-0.3,-0.1\n', encoding='utf-8')
        balance = water_balance({'components': str(components), 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'outcomeKm3': 0.8})
        assert abs(balance['summary']['closureRatio'] - 1.0) < 1e-9
        checks += 1
        scenario = breach({'hypsometry': result['paths']['hypsometry'], 'preLevelM': 100, 'postLevelM': 0, 'durationDays': 1, 'stateRoot': str(state), 'projectRoot': str(project), 'allowedRoots': [str(project)], 'widthM': 2, 'dischargeCoefficient': 1.7, 'headM': 1})
        assert scenario['meanOutflowM3s'] > 0 and scenario['weirScenario']['peakOutflowM3s'] > 0
        checks += 1

        geographic_dem = project / 'geographic.tif'
        source_transform = from_origin(87, 35, 0.01, 0.01)
        with rasterio.open(geographic_dem, 'w', driver='GTiff', width=4, height=4, count=1, dtype='float32', crs='EPSG:4326', transform=source_transform, nodata=-9999) as target:
            target.write(np.arange(16, dtype='float32').reshape(4, 4), 1)
        target_transform, target_width, target_height = calculate_default_transform('EPSG:4326', 'EPSG:32645', 4, 4, 87, 34.96, 87.04, 35)
        # Align a mask to exactly four interior cells on the independent target grid.
        polygon = box(
            target_transform.c + target_transform.a,
            target_transform.f + 3 * target_transform.e,
            target_transform.c + 3 * target_transform.a,
            target_transform.f + target_transform.e,
        )
        mask_path = project / 'mask.geojson'
        gpd.GeoDataFrame(geometry=[polygon], crs='EPSG:32645').to_file(mask_path, driver='GeoJSON')
        masked = hypsometry({'source': str(geographic_dem), 'mask': str(mask_path), 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'workingCrs': 'EPSG:32645', 'elevationStepM': 5, 'maxHashBytes': 1000000})
        manifest = json.loads(Path(masked['paths']['manifest']).read_text(encoding='utf-8'))
        expected_area = 4 * abs(target_transform.a * target_transform.e) / 1_000_000
        assert target_width == 4 and target_height == 4 and manifest['validPixels'] == 4
        checks += 1
        assert abs(masked['summary']['areaBelowMaxKm2'] - expected_area) < 1e-12
        checks += 1

        outside = root / 'outside.tif'
        with rasterio.open(outside, 'w', driver='GTiff', width=2, height=2, count=1, dtype='float32', crs='EPSG:32645', transform=rasterio.Affine(100, 0, 0, 0, -100, 0), nodata=-9999) as target:
            target.write(np.arange(4, dtype='float32').reshape(2, 2), 1)
        vrt_template = '''<VRTDataset rasterXSize="2" rasterYSize="2">
  <SRS>EPSG:32645</SRS><GeoTransform>0,100,0,0,0,-100</GeoTransform>
  <VRTRasterBand dataType="Float32" band="1"><NoDataValue>-9999</NoDataValue><SimpleSource>
    <SourceFilename relativeToVRT="1">{source}</SourceFilename><SourceBand>1</SourceBand>
    <SourceProperties RasterXSize="2" RasterYSize="2" DataType="Float32" BlockXSize="2" BlockYSize="2"/>
    <SrcRect xOff="0" yOff="0" xSize="2" ySize="2"/><DstRect xOff="0" yOff="0" xSize="2" ySize="2"/>
  </SimpleSource></VRTRasterBand>
</VRTDataset>'''
        outside_vrt = project / 'outside.vrt'
        outside_vrt.write_text(vrt_template.format(source='../outside.tif'), encoding='utf-8')
        try:
            hypsometry({'source': str(outside_vrt), 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'maxHashBytes': 1000000})
        except HydroError:
            checks += 1
        else:
            raise AssertionError('VRT source outside projectRoot accepted')
        remote_vrt = project / 'remote.vrt'
        remote_vrt.write_text(vrt_template.format(source='/vsicurl/https://example.invalid/dem.tif'), encoding='utf-8')
        try:
            hypsometry({'source': str(remote_vrt), 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'maxHashBytes': 1000000})
        except HydroError:
            checks += 1
        else:
            raise AssertionError('Remote VRT source accepted')
        try:
            hypsometry({'source': '../escape.tif', 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state)})
        except HydroError:
            checks += 1
        else:
            raise AssertionError('Escaping DEM accepted')
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise HydroError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise HydroError('Worker request must be a JSON object')
            max_hash = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash, int) or isinstance(max_hash, bool) or not 1024 <= max_hash <= 2147483648:
                raise HydroError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash
            action = request.get('action')
            if action == 'hypsometry':
                result = hypsometry(request)
            elif action == 'storage':
                result = storage(request)
            elif action == 'water_balance':
                result = water_balance(request)
            elif action == 'breach':
                result = breach(request)
            else:
                raise HydroError(f'Unsupported action: {action}')
    except HydroError as error:
        result = {'ok': False, 'error': {'code': 'HYDRO_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'HYDRO_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(json_safe(result), ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
