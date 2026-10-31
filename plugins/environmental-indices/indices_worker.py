#!/usr/bin/env python3
"""Environmental index and TVDI computation from aligned local rasters."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import rasterio

MAX_STDIN_BYTES = 4 * 1024 * 1024
ROLES = {'red', 'nir', 'green', 'swir1', 'ndvi', 'lst', 'qa'}
INDICES = {'ndvi', 'fvc', 'lst', 'ndmi', 'ndsi', 'tvdi'}


class IndicesError(ValueError):
    pass


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise IndicesError('projectRoot is required for local rasters')
    if not isinstance(allowed, list) or not allowed:
        raise IndicesError('allowedRoots is required')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise IndicesError(f'Project root is not a directory: {root}')
    for item in allowed:
        if isinstance(item, str) and inside(root, Path(item).expanduser().resolve()):
            return root
    raise IndicesError(f'Project root is outside allowedRoots: {root}')


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise IndicesError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_path(root: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise IndicesError('Raster path must be a non-empty string')
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not inside(path, root):
        raise IndicesError(f'Raster path escapes projectRoot: {raw}')
    if not path.is_file():
        raise IndicesError(f'Raster not found: {raw}')
    return path


def sources(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = request.get('inputs')
    if not isinstance(raw, dict) or not raw:
        raise IndicesError('inputs must map roles to raster paths or {path,band} objects')
    root = project_root(request)
    result = {}
    for role, value in raw.items():
        if role not in ROLES:
            raise IndicesError(f'Unsupported input role: {role}')
        if isinstance(value, str):
            value = {'path': value}
        if not isinstance(value, dict):
            raise IndicesError(f'Input {role} must be a path or object')
        path = local_path(root, value.get('path'))
        band = value.get('band', 1)
        if not isinstance(band, int) or isinstance(band, bool) or band < 1:
            raise IndicesError(f'Input {role} band must be a positive integer')
        result[role] = {'path': path, 'band': band, 'role': role}
    return result


def number(request: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < minimum or value > maximum:
        raise IndicesError(f'{key} must be a finite number from {minimum} to {maximum}')
    return float(value)


def inspect(request: dict[str, Any]) -> dict[str, Any]:
    result = []
    seen = set()
    for role, source in sources(request).items():
        key = (str(source['path']), source['band'])
        if key in seen:
            continue
        seen.add(key)
        with rasterio.open(source['path']) as dataset:
            result.append({
                'role': role,
                'path': str(source['path']),
                'band': source['band'],
                'width': dataset.width,
                'height': dataset.height,
                'bandCount': dataset.count,
                'dtype': dataset.dtypes[source['band'] - 1],
                'crs': str(dataset.crs) if dataset.crs else None,
                'transform': list(dataset.transform),
                'bounds': list(dataset.bounds),
                'nodata': dataset.nodata,
                'descriptions': dataset.descriptions,
            })
    return {'ok': True, 'action': 'inspect', 'sources': result}


def read_raster(source: dict[str, Any], target_grid: dict[str, Any] | None = None) -> tuple[np.ma.MaskedArray, dict[str, Any]]:
    with rasterio.open(source['path']) as dataset:
        if source['band'] > dataset.count:
            raise IndicesError(f'Band {source["band"]} does not exist in {source["path"]}')
        metadata = {
            'width': dataset.width,
            'height': dataset.height,
            'transform': dataset.transform,
            'crs': dataset.crs,
        }
        if target_grid is None or grid_compatible(metadata, target_grid):
            return dataset.read(source['band'], masked=True).astype('float32'), target_grid or metadata
        if dataset.crs is None or target_grid['crs'] is None:
            raise IndicesError('Cannot align rasters when CRS metadata is missing')
        from rasterio.warp import Resampling, reproject
        source_values = dataset.read(source['band'], masked=False)
        destination = np.full((target_grid['height'], target_grid['width']), np.nan, dtype='float32')
        reproject(
            source=source_values,
            destination=destination,
            src_transform=metadata['transform'],
            src_crs=metadata['crs'],
            src_nodata=dataset.nodata,
            dst_transform=target_grid['transform'],
            dst_crs=target_grid['crs'],
            dst_nodata=np.nan,
            resampling=Resampling.nearest if source.get('role') == 'qa' else Resampling.bilinear,
        )
        return np.ma.masked_invalid(destination), target_grid


def finite_mask(data: np.ma.MaskedArray) -> np.ndarray:
    return ~np.ma.getmaskarray(data) & np.isfinite(data)


def normalized_difference(first: np.ma.MaskedArray, second: np.ma.MaskedArray) -> np.ma.MaskedArray:
    denominator = first + second
    with np.errstate(divide='ignore', invalid='ignore'):
        return (first - second) / denominator


def valid_values(data: np.ma.MaskedArray, valid: np.ndarray) -> np.ndarray:
    values = np.asarray(data)[valid & np.isfinite(data)]
    return values.astype('float64', copy=False)


def stats_for(name: str, data: np.ma.MaskedArray, valid: np.ndarray) -> dict[str, Any]:
    values = valid_values(data, valid)
    return {
        'index': name,
        'validCount': int(values.size),
        'mean': float(values.mean()) if values.size else None,
        'std': float(values.std()) if values.size else None,
        'min': float(values.min()) if values.size else None,
        'max': float(values.max()) if values.size else None,
    }


def hash_file(path: Path, max_bytes: int) -> dict[str, Any]:
    size = path.stat().st_size
    if size > max_bytes:
        return {'path': str(path), 'bytes': size, 'skipped': True}
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'path': str(path), 'bytes': size, 'sha256': digest.hexdigest()}


def fit_tvdi(ndvi: np.ma.MaskedArray, lst: np.ma.MaskedArray, valid: np.ndarray, request: dict[str, Any]) -> tuple[np.ma.MaskedArray, dict[str, Any]]:
    ndvi_min = number(request, 'tvdiNdviMin', 0.0, -1, 1)
    ndvi_max = number(request, 'tvdiNdviMax', 1.0, -1, 1)
    if ndvi_max <= ndvi_min:
        raise IndicesError('tvdiNdviMax must be greater than tvdiNdviMin')
    bin_count = int(number(request, 'ndviBins', 20, 2, 200))
    min_points = int(number(request, 'minBinPoints', 30, 1, 1000000))
    dry_percentile = number(request, 'dryPercentile', 95, 0, 100)
    wet_percentile = number(request, 'wetPercentile', 5, 0, 100)
    if dry_percentile <= wet_percentile:
        raise IndicesError('dryPercentile must be greater than wetPercentile')
    x = valid_values(ndvi, valid)
    y = valid_values(lst, valid)
    in_range = (x >= ndvi_min) & (x <= ndvi_max)
    x = x[in_range]
    y = y[in_range]
    if x.size < max(100, min_points * 2):
        raise IndicesError(f'Not enough valid NDVI-LST points for TVDI: {x.size}')
    edges = np.linspace(ndvi_min, ndvi_max, bin_count + 1)
    rows = []
    for index in range(bin_count):
        left, right = edges[index], edges[index + 1]
        selected = (x >= left) & (x < right if index < bin_count - 1 else x <= right)
        if selected.sum() < min_points:
            continue
        rows.append({
            'ndvi': float(x[selected].mean()),
            'dryLst': float(np.percentile(y[selected], dry_percentile)),
            'wetLst': float(np.percentile(y[selected], wet_percentile)),
            'count': int(selected.sum()),
        })
    if len(rows) < 2:
        raise IndicesError('TVDI requires at least two populated NDVI bins')
    x_fit = np.array([row['ndvi'] for row in rows])
    dry_fit = np.polyfit(x_fit, np.array([row['dryLst'] for row in rows]), 1)
    wet_fit = np.polyfit(x_fit, np.array([row['wetLst'] for row in rows]), 1)
    dry = dry_fit[0] * ndvi + dry_fit[1]
    wet = wet_fit[0] * ndvi + wet_fit[1]
    with np.errstate(divide='ignore', invalid='ignore'):
        tvdi = (lst - wet) / (dry - wet)
    tvdi = np.ma.clip(tvdi, 0, 1).astype('float32')
    return tvdi, {
        'ndviBins': bin_count,
        'minBinPoints': min_points,
        'pointsUsed': int(x.size),
        'dryPercentile': dry_percentile,
        'wetPercentile': wet_percentile,
        'dryEdge': {'slope': float(dry_fit[0]), 'intercept': float(dry_fit[1])},
        'wetEdge': {'slope': float(wet_fit[0]), 'intercept': float(wet_fit[1])},
        'bins': rows,
    }


def grid_compatible(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if first['width'] != second['width'] or first['height'] != second['height'] or first['crs'] != second['crs']:
        return False
    return bool(np.allclose(tuple(first['transform']), tuple(second['transform']), rtol=0, atol=1e-9))


def calculate(request: dict[str, Any]) -> dict[str, Any]:
    source_map = sources(request)
    requested = request.get('indices', ['ndvi', 'fvc', 'lst', 'ndmi', 'ndsi', 'tvdi'])
    if not isinstance(requested, list) or not requested or any(index not in INDICES for index in requested):
        raise IndicesError('indices must be a non-empty subset of ndvi/fvc/lst/ndmi/ndsi/tvdi')
    if len(set(requested)) != len(requested):
        raise IndicesError('indices must not contain duplicates')
    loaded = {}
    profile_metadata = None
    for role, source in source_map.items():
        data, metadata = read_raster(source, profile_metadata)
        if profile_metadata is None:
            profile_metadata = metadata
        elif not grid_compatible(metadata, profile_metadata):
            raise IndicesError('All rasters must share the same shape, transform, and CRS')
        if metadata['width'] * metadata['height'] > int(number(request, 'maxPixels', 20000000, 1000, 200000000)):
            raise IndicesError('Raster exceeds configured maxPixels')
        loaded[role] = data
    reflectance_scale = number(request, 'reflectanceScale', 1.0, 0.000000001, 1000000)
    reflectance_offset = number(request, 'reflectanceOffset', 0.0, -1000000, 1000000)
    ndvi_scale = number(request, 'ndviScale', 1.0, 0.000000001, 1000000)
    ndvi_offset = number(request, 'ndviOffset', 0.0, -1000000, 1000000)
    lst_scale = number(request, 'lstScale', 1.0, 0.000000001, 1000000)
    lst_offset = number(request, 'lstOffset', 0.0, -1000000, 1000000)
    reflectance_min = number(request, 'reflectanceMin', -1.0, -1000000, 1000000)
    reflectance_max = number(request, 'reflectanceMax', 2.0, -1000000, 1000000)
    ndvi_min = number(request, 'ndviValidMin', -1.0, -1000000, 1000000)
    ndvi_max = number(request, 'ndviValidMax', 1.0, -1000000, 1000000)
    lst_min = number(request, 'lstMinC', -100.0, -1000, 1000)
    lst_max = number(request, 'lstMaxC', 100.0, -1000, 1000)
    mask_nodata = request.get('maskNodata')
    if mask_nodata is not None and (isinstance(mask_nodata, bool) or not isinstance(mask_nodata, (int, float))):
        raise IndicesError('maskNodata must be numeric')
    for role in ('red', 'nir', 'green', 'swir1'):
        if role in loaded:
            loaded[role] = loaded[role] * reflectance_scale + reflectance_offset
    if 'ndvi' in loaded:
        loaded['ndvi'] = loaded['ndvi'] * ndvi_scale + ndvi_offset
    if 'lst' in loaded:
        loaded['lst'] = loaded['lst'] * lst_scale + lst_offset
        unit = request.get('lstUnit', 'C')
        if unit == 'K':
            loaded['lst'] = loaded['lst'] - 273.15
        elif unit != 'C':
            raise IndicesError('lstUnit must be C or K')
    valid = np.ones(profile_metadata['height'], dtype=bool)[:, None] * np.ones(profile_metadata['width'], dtype=bool)[None, :]
    for role, data in loaded.items():
        valid &= finite_mask(data)
        if mask_nodata is not None:
            valid &= np.asarray(data) != mask_nodata
        if role in {'red', 'nir', 'green', 'swir1'}:
            valid &= (np.asarray(data) >= reflectance_min) & (np.asarray(data) <= reflectance_max)
        elif role == 'ndvi':
            valid &= (np.asarray(data) >= ndvi_min) & (np.asarray(data) <= ndvi_max)
        elif role == 'lst':
            valid &= (np.asarray(data) >= lst_min) & (np.asarray(data) <= lst_max)
    if request.get('qaMode') is not None:
        if request['qaMode'] != 'landsat':
            raise IndicesError('qaMode currently supports only landsat')
        if 'qa' not in loaded:
            raise IndicesError('qaMode=landsat requires a QA input')
        qa = np.asarray(loaded['qa']).astype('uint16')
        bits = [0, 1, 2, 3, 4]
        if not request.get('keepSnow', False):
            bits.append(5)
        for bit in bits:
            valid &= (qa & (1 << bit)) == 0
    if not valid.any():
        raise IndicesError('No valid pixels remain after masks')
    if 'ndvi' in loaded:
        ndvi = loaded['ndvi']
    elif 'red' in loaded and 'nir' in loaded:
        ndvi = normalized_difference(loaded['nir'], loaded['red'])
    else:
        raise IndicesError('NDVI calculation requires ndvi or red+nir inputs')
    outputs = {}
    if 'ndvi' in requested:
        outputs['ndvi'] = ndvi
    if 'fvc' in requested:
        minimum = number(request, 'fvcNdviMin', 0.05, -1, 1)
        maximum = number(request, 'fvcNdviMax', 0.8, -1, 1)
        if maximum <= minimum:
            raise IndicesError('fvcNdviMax must be greater than fvcNdviMin')
        outputs['fvc'] = np.ma.clip((ndvi - minimum) / (maximum - minimum), 0, 1).astype('float32')
    if 'lst' in requested:
        if 'lst' not in loaded:
            raise IndicesError('lst index requires an LST input')
        outputs['lst'] = loaded['lst']
    if 'ndmi' in requested:
        if 'nir' not in loaded or 'swir1' not in loaded:
            raise IndicesError('NDMI requires nir and swir1 inputs')
        outputs['ndmi'] = normalized_difference(loaded['nir'], loaded['swir1'])
    if 'ndsi' in requested:
        if 'green' not in loaded or 'swir1' not in loaded:
            raise IndicesError('NDSI requires green and swir1 inputs')
        outputs['ndsi'] = normalized_difference(loaded['green'], loaded['swir1'])
    tvdi_fit = None
    if 'tvdi' in requested:
        if 'lst' not in loaded:
            raise IndicesError('TVDI requires an LST input')
        outputs['tvdi'], tvdi_fit = fit_tvdi(ndvi, loaded['lst'], valid, request)
    stats = [stats_for(name, outputs[name], valid) for name in requested]
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    raster_path = output_root / 'indices.tif'
    stats_path = output_root / 'stats.csv'
    fit_path = output_root / 'tvdi_fit.json'
    manifest_path = output_root / 'manifest.json'
    first_path = next(iter(source_map.values()))['path']
    with rasterio.open(first_path) as dataset:
        profile = dataset.profile.copy()
    profile.update(driver='GTiff', count=len(requested), dtype='float32', nodata=np.nan, compress='DEFLATE', predictor=3, tiled=False)
    with rasterio.open(raster_path, 'w', **profile) as target:
        for index, name in enumerate(requested, start=1):
            values = np.ma.filled(outputs[name], np.nan).astype('float32')
            values[~valid] = np.nan
            target.write(values, index)
            target.set_band_description(index, name)
    with stats_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['index', 'validCount', 'mean', 'std', 'min', 'max'])
        writer.writeheader()
        writer.writerows(stats)
    if tvdi_fit is not None:
        fit_path.write_text(json.dumps(tvdi_fit, ensure_ascii=False, indent=2), encoding='utf-8')
    unique_paths = sorted({str(source['path']) for source in source_map.values()})
    manifest = {
        'batchId': batch_id,
        'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'indices': requested,
        'validPixels': int(valid.sum()),
        'validFraction': float(valid.mean()),
        'inputs': [hash_file(Path(path), request['maxHashBytes']) for path in unique_paths],
        'parameters': {
            'reflectanceScale': reflectance_scale, 'reflectanceOffset': reflectance_offset,
            'ndviScale': ndvi_scale, 'ndviOffset': ndvi_offset,
            'lstScale': lst_scale, 'lstOffset': lst_offset, 'lstUnit': request.get('lstUnit', 'C'),
            'qaMode': request.get('qaMode'), 'keepSnow': request.get('keepSnow', False),
            'alignment': 'reproject-to-first-source; bilinear for continuous rasters and nearest for QA',
        },
        'stats': stats,
        'tvdiFit': tvdi_fit,
        'outputs': {'raster': hash_file(raster_path, request['maxHashBytes']), 'stats': hash_file(stats_path, request['maxHashBytes'])},
    }
    if tvdi_fit is not None:
        manifest['outputs']['tvdiFit'] = hash_file(fit_path, request['maxHashBytes'])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    relative = lambda path: f"state://{path.relative_to(state_root(request)).as_posix()}"
    return {
        'ok': True,
        'action': 'calculate',
        'batchId': batch_id,
        'indices': requested,
        'validPixels': manifest['validPixels'],
        'validFraction': manifest['validFraction'],
        'stats': stats,
        'tvdiFit': tvdi_fit,
        'paths': {'raster': str(raster_path), 'stats': str(stats_path), 'fit': str(fit_path) if tvdi_fit else None, 'manifest': str(manifest_path)},
        'statePaths': {'raster': relative(raster_path), 'stats': relative(stats_path), 'fit': relative(fit_path) if tvdi_fit else None, 'manifest': relative(manifest_path)},
    }


def self_test() -> dict[str, Any]:
    import tempfile
    checks = 0
    with tempfile.TemporaryDirectory(prefix='indices-worker-') as temporary:
        root = Path(temporary)
        state = root / 'state'
        project = root / 'project'
        project.mkdir()
        shape = (40, 40)
        x = np.linspace(0.1, 0.8, shape[1], dtype='float32')[None, :].repeat(shape[0], axis=0)
        arrays = {
            'ndvi': x,
            'lst': 15 + 20 * (1 - x) + np.linspace(0, 3, shape[0], dtype='float32')[:, None],
            'red': np.full(shape, 0.2, dtype='float32'),
            'nir': np.full(shape, 0.6, dtype='float32'),
            'green': np.full(shape, 0.15, dtype='float32'),
            'swir1': np.full(shape, 0.1, dtype='float32'),
        }
        transform = rasterio.Affine(1, 0, 0, 0, -1, 0)
        for role, values in arrays.items():
            path = project / f'{role}.tif'
            with rasterio.open(path, 'w', driver='GTiff', width=shape[1], height=shape[0], count=1, dtype='float32', crs='EPSG:4326', transform=transform, nodata=-9999) as target:
                target.write(values, 1)
        inputs = {role: f'{role}.tif' for role in arrays}
        request = {'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'inputs': inputs, 'indices': ['ndvi', 'fvc', 'lst', 'ndmi', 'ndsi', 'tvdi'], 'maxPixels': 1000000, 'maxHashBytes': 1000000, 'ndviBins': 5, 'minBinPoints': 5}
        result = calculate(request)
        assert result['validPixels'] == shape[0] * shape[1]
        checks += 1
        assert len(result['indices']) == 6 and Path(result['paths']['raster']).is_file()
        checks += 1
        assert result['tvdiFit']['dryEdge']['slope'] < 0
        checks += 1
        with rasterio.open(result['paths']['raster']) as output:
            assert output.count == 6 and output.descriptions[0] == 'ndvi'
        checks += 1
        try:
            calculate({**request, 'inputs': {'ndvi': '../escape.tif'}, 'indices': ['ndvi']})
        except IndicesError:
            checks += 1
        else:
            raise AssertionError('Escaping raster accepted')
        assert inspect(request)['sources']
        checks += 1
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise IndicesError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise IndicesError('Worker request must be a JSON object')
            max_hash = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash, int) or isinstance(max_hash, bool) or not 1024 <= max_hash <= 2147483648:
                raise IndicesError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash
            action = request.get('action')
            if action == 'inspect':
                result = inspect(request)
            elif action == 'calculate':
                result = calculate(request)
            else:
                raise IndicesError(f'Unsupported action: {action}')
    except IndicesError as error:
        result = {'ok': False, 'error': {'code': 'INDICES_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'INDICES_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
