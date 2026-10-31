#!/usr/bin/env python3
"""Field multisource inspection, alignment, QA and paired comparison."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pyproj import Geod

MAX_STDIN_BYTES = 4 * 1024 * 1024
GEOD = Geod(ellps='WGS84')


class FieldError(ValueError):
    pass


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise FieldError('projectRoot is required for local files')
    if not isinstance(allowed, list) or not allowed:
        raise FieldError('allowedRoots is required')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise FieldError(f'Project root is not a directory: {root}')
    for item in allowed:
        if isinstance(item, str) and inside(root, Path(item).expanduser().resolve()):
            return root
    raise FieldError(f'Project root is outside allowedRoots: {root}')


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise FieldError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_path(root: Path, raw: Any, state: Path | None = None) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise FieldError('File path must be a non-empty string')
    if raw.startswith('state://'):
        if state is None:
            raise FieldError('stateRoot is required for state:// paths')
        path = (state / raw[len('state://'):]).resolve()
        if not inside(path, state):
            raise FieldError('State file escapes stateRoot')
        return path
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if state is not None and inside(path, state):
        return path
    if not inside(path, root):
        raise FieldError(f'File escapes projectRoot: {raw}')
    if not path.is_file():
        raise FieldError(f'File not found: {raw}')
    return path


def number(request: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < minimum or value > maximum:
        raise FieldError(f'{key} must be a finite number from {minimum} to {maximum}')
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
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return value


def read_table(request: dict[str, Any], key: str) -> tuple[Path, pd.DataFrame]:
    path = local_path(project_root(request), request.get(key), state_root(request))
    suffix = path.suffix.lower()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=Warning)
        if suffix in {'.xls', '.xlsx'}:
            frame = pd.read_excel(path, sheet_name=request.get(f'{key}Sheet', request.get('sheetName', 0)), header=int(request.get('headerRow', 0)))
        elif suffix in {'.geojson', '.json'}:
            frame = pd.read_json(path)
        else:
            frame = pd.read_csv(path)
    frame.columns = [str(column) for column in frame.columns]
    return path, frame


def resolve_column(frame: pd.DataFrame, raw: Any, required: bool = True) -> str | None:
    if raw is None:
        if required:
            raise FieldError('A required column is missing')
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        if not 0 <= raw < len(frame.columns):
            raise FieldError(f'Column index out of range: {raw}')
        return frame.columns[raw]
    column = str(raw)
    if column not in frame.columns:
        raise FieldError(f'Column not found: {column}')
    return column


def parse_times(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if column is None:
        return pd.Series(pd.NaT, index=frame.index)
    return pd.to_datetime(frame[column], errors='coerce')


def inspect_source(request: dict[str, Any]) -> dict[str, Any]:
    path, frame = read_table(request, 'source')
    time_column = resolve_column(frame, request.get('timeColumn'), required=False)
    times = parse_times(frame, time_column)
    value_columns = request.get('valueColumns', [])
    if not isinstance(value_columns, list):
        raise FieldError('valueColumns must be an array')
    stats = {}
    for raw in value_columns:
        column = resolve_column(frame, raw)
        values = pd.to_numeric(frame[column], errors='coerce')
        stats[column] = {'count': int(values.notna().sum()), 'missing': int(values.isna().sum()), 'min': values.min(), 'max': values.max(), 'mean': values.mean()}
    return {
        'ok': True, 'action': 'inspect', 'source': str(path),
        'summary': {'rows': len(frame), 'columns': len(frame.columns), 'timeColumn': time_column, 'validTimes': int(times.notna().sum()), 'firstTime': times.min(), 'lastTime': times.max()},
        'columns': frame.columns.tolist(), 'valueStats': stats,
        'sample': frame.head(5).astype(str).to_dict('records'),
    }


def quality_control(request: dict[str, Any]) -> dict[str, Any]:
    path, frame = read_table(request, 'source')
    value_columns = request.get('valueColumns')
    if value_columns is None:
        value_columns = frame.select_dtypes(include=['number']).columns.tolist()
    if not isinstance(value_columns, list):
        raise FieldError('valueColumns must be an array')
    required = request.get('requiredColumns', [])
    if not isinstance(required, list):
        raise FieldError('requiredColumns must be an array')
    for raw in required:
        resolve_column(frame, raw)
    minimum = request.get('minValue')
    maximum = request.get('maxValue')
    if minimum is not None:
        minimum = number(request, 'minValue', 0, -1000000000, 1000000000)
    if maximum is not None:
        maximum = number(request, 'maxValue', 0, -1000000000, 1000000000)
    missing = {}
    out_of_range = {}
    for raw in value_columns:
        column = resolve_column(frame, raw)
        values = pd.to_numeric(frame[column], errors='coerce')
        missing[column] = int(values.isna().sum())
        mask = pd.Series(False, index=frame.index)
        if minimum is not None:
            mask |= values < minimum
        if maximum is not None:
            mask |= values > maximum
        out_of_range[column] = int(mask.fillna(False).sum())
    duplicate_keys = request.get('duplicateKeyColumns', [])
    if not isinstance(duplicate_keys, list):
        raise FieldError('duplicateKeyColumns must be an array')
    key_columns = [resolve_column(frame, raw) for raw in duplicate_keys]
    duplicate_rows = int(frame.duplicated(subset=key_columns).sum()) if key_columns else int(frame.duplicated().sum())
    time_column = resolve_column(frame, request.get('timeColumn'), required=False)
    times = parse_times(frame, time_column).dropna().sort_values()
    gaps = times.diff().dt.total_seconds().dropna() / 3600
    max_gap = number(request, 'maxGapHours', 48, 0, 100000000)
    gap_count = int((gaps > max_gap).sum())
    return {
        'ok': True, 'action': 'qc', 'source': str(path),
        'summary': {
            'rows': len(frame), 'missingByColumn': missing, 'outOfRangeByColumn': out_of_range,
            'duplicateRows': duplicate_rows, 'timeGaps': gap_count, 'maxObservedGapHours': gaps.max(),
            'qaStatus': 'issues_found' if any(missing.values()) or any(out_of_range.values()) or duplicate_rows or gap_count else 'clean',
        },
    }


def align_sources(request: dict[str, Any]) -> dict[str, Any]:
    root = project_root(request)
    state = state_root(request)
    path_a, frame_a = read_table(request, 'sourceA')
    path_b, frame_b = read_table(request, 'sourceB')
    time_a = resolve_column(frame_a, request.get('timeColumnA'), required=False)
    time_b = resolve_column(frame_b, request.get('timeColumnB'), required=False)
    if (time_a is None) != (time_b is None):
        raise FieldError('Both sources must provide time columns, or neither')
    times_a = parse_times(frame_a, time_a)
    times_b = parse_times(frame_b, time_b)
    lat_a = resolve_column(frame_a, request.get('latColumnA'), required=False)
    lon_a = resolve_column(frame_a, request.get('lonColumnA'), required=False)
    lat_b = resolve_column(frame_b, request.get('latColumnB'), required=False)
    lon_b = resolve_column(frame_b, request.get('lonColumnB'), required=False)
    has_space = all(column is not None for column in (lat_a, lon_a, lat_b, lon_b))
    if time_a is None and not has_space:
        raise FieldError('Alignment requires time columns in both sources or coordinates in both sources')
    value_a = resolve_column(frame_a, request.get('valueColumnA'), required=False)
    value_b = resolve_column(frame_b, request.get('valueColumnB'), required=False)
    tolerance_seconds = number(request, 'maxTimeDiffSeconds', 86400, 0, 1000000000)
    distance_limit = number(request, 'maxDistanceMeters', 1000, 0, 1000000000)
    max_pairs = int(number(request, 'maxPairs', 10000, 1, 1000000))
    rows = []
    if time_a is not None and not has_space:
        left = pd.DataFrame({'indexA': frame_a.index, 'timeA': times_a, 'valueA': frame_a[value_a] if value_a is not None else None})
        right = pd.DataFrame({'indexB': frame_b.index, 'timeB': times_b, 'valueB': frame_b[value_b] if value_b is not None else None})
        left = left.dropna(subset=['timeA']).sort_values('timeA')
        right = right.dropna(subset=['timeB']).sort_values('timeB')
        merged = pd.merge_asof(left, right, left_on='timeA', right_on='timeB', tolerance=pd.Timedelta(seconds=tolerance_seconds), direction='nearest')
        merged = merged.dropna(subset=['indexB']).head(max_pairs)
        merged['timeDiffSeconds'] = (merged['timeA'] - merged['timeB']).abs().dt.total_seconds()
        merged['distanceMeters'] = None
        rows = merged[['indexA', 'indexB', 'timeA', 'timeB', 'timeDiffSeconds', 'distanceMeters', 'valueA', 'valueB']].to_dict('records')
    else:
        used_b = set()
        for index_a, row_a in frame_a.iterrows():
            candidates = []
            for index_b, row_b in frame_b.iterrows():
                if index_b in used_b:
                    continue
                time_diff = None
                if time_a is not None:
                    if pd.isna(times_a.loc[index_a]) or pd.isna(times_b.loc[index_b]):
                        continue
                    time_diff = abs((times_a.loc[index_a] - times_b.loc[index_b]).total_seconds())
                    if time_diff > tolerance_seconds:
                        continue
                distance = None
                if has_space:
                    try:
                        _, _, distance = GEOD.inv(float(row_a[lon_a]), float(row_a[lat_a]), float(row_b[lon_b]), float(row_b[lat_b]))
                    except (TypeError, ValueError):
                        continue
                    if not np.isfinite(distance) or distance > distance_limit:
                        continue
                candidates.append((distance if distance is not None else 0, time_diff if time_diff is not None else 0, index_b, row_b, time_diff, distance))
            if not candidates:
                continue
            _, _, index_b, row_b, time_diff, distance = min(candidates, key=lambda item: (item[0], item[1]))
            used_b.add(index_b)
            rows.append({
                'indexA': index_a, 'indexB': index_b,
                'timeA': times_a.loc[index_a] if time_a is not None else None,
                'timeB': times_b.loc[index_b] if time_b is not None else None,
                'timeDiffSeconds': time_diff, 'distanceMeters': distance,
                'valueA': row_a[value_a] if value_a is not None else None,
                'valueB': row_b[value_b] if value_b is not None else None,
            })
            if len(rows) >= max_pairs:
                break
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    table_path = output_root / 'pairs.csv'
    manifest_path = output_root / 'manifest.json'
    with table_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['indexA', 'indexB', 'timeA', 'timeB', 'timeDiffSeconds', 'distanceMeters', 'valueA', 'valueB'])
        writer.writeheader()
        for row in rows:
            writer.writerow({key: (value.isoformat() if isinstance(value, pd.Timestamp) else value) for key, value in row.items()})
    time_diffs = np.array([row['timeDiffSeconds'] for row in rows if row['timeDiffSeconds'] is not None], dtype=float)
    distances = np.array([row['distanceMeters'] for row in rows if row['distanceMeters'] is not None], dtype=float)
    summary = {
        'matchedPairs': len(rows), 'unmatchedA': len(frame_a) - len(rows),
        'meanTimeDiffSeconds': float(time_diffs.mean()) if time_diffs.size else None,
        'maxTimeDiffSeconds': float(time_diffs.max()) if time_diffs.size else None,
        'medianDistanceMeters': float(np.median(distances)) if distances.size else None,
    }
    manifest = {
        'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'sourceA': str(path_a), 'sourceB': str(path_b), 'parameters': {'maxTimeDiffSeconds': tolerance_seconds, 'maxDistanceMeters': distance_limit},
        'summary': summary, 'outputs': {'pairs': hash_file(table_path, request['maxHashBytes'])},
    }
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {
        'ok': True, 'action': 'align', 'batchId': batch_id, 'summary': summary, 'rows': rows[:100],
        'paths': {'pairs': str(table_path), 'manifest': str(manifest_path)},
        'statePaths': {'pairs': f'state://{batch_id}/pairs.csv', 'manifest': f'state://{batch_id}/manifest.json'},
    }


def compare_values(request: dict[str, Any]) -> dict[str, Any]:
    path, frame = read_table(request, 'table')
    column_a = resolve_column(frame, request.get('columnA'))
    column_b = resolve_column(frame, request.get('columnB'))
    values_a = pd.to_numeric(frame[column_a], errors='coerce')
    values_b = pd.to_numeric(frame[column_b], errors='coerce')
    valid = values_a.notna() & values_b.notna()
    a = values_a[valid].to_numpy(dtype=float)
    b = values_b[valid].to_numpy(dtype=float)
    if a.size < 2:
        raise FieldError('At least two finite paired values are required')
    error = a - b
    sse = float((error ** 2).sum())
    sst = float(((a - a.mean()) ** 2).sum())
    return {
        'ok': True, 'action': 'compare', 'source': str(path),
        'summary': {
            'n': int(a.size), 'mae': float(np.abs(error).mean()), 'rmse': float(np.sqrt((error ** 2).mean())),
            'bias': float(error.mean()), 'maxAbsError': float(np.abs(error).max()),
            'r2': 1 - sse / sst if sst > 0 else None,
            'correlation': float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else None,
            'meanA': float(a.mean()), 'meanB': float(b.mean()),
        },
    }


def self_test() -> dict[str, Any]:
    import tempfile
    checks = 0
    with tempfile.TemporaryDirectory(prefix='field-worker-') as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        state = root / 'state'
        a = project / 'a.csv'
        b = project / 'b.csv'
        a.write_text('date,lat,lon,temp\n2024-01-01,35,100,1\n2024-01-02,35,100,2\n2024-01-03,35,100,3\n', encoding='utf-8')
        b.write_text('date,lat,lon,temp\n2024-01-01,35,100,2\n2024-01-02,35,100,3\n2024-01-03,35,100,4\n', encoding='utf-8')
        base = {'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'maxHashBytes': 1000000}
        aligned = align_sources({**base, 'sourceA': 'a.csv', 'sourceB': 'b.csv', 'timeColumnA': 'date', 'timeColumnB': 'date', 'latColumnA': 'lat', 'lonColumnA': 'lon', 'latColumnB': 'lat', 'lonColumnB': 'lon', 'valueColumnA': 'temp', 'valueColumnB': 'temp'})
        assert aligned['summary']['matchedPairs'] == 3
        checks += 1
        compared = compare_values({**base, 'table': aligned['paths']['pairs'], 'columnA': 'valueA', 'columnB': 'valueB'})
        assert abs(compared['summary']['rmse'] - 1) < 1e-9 and compared['summary']['bias'] == -1
        checks += 1
        inspected = inspect_source({**base, 'source': 'a.csv', 'timeColumn': 'date', 'valueColumns': ['temp']})
        assert inspected['summary']['rows'] == 3
        checks += 1
        quality = quality_control({**base, 'source': 'a.csv', 'valueColumns': ['temp'], 'timeColumn': 'date', 'minValue': -10, 'maxValue': 10, 'maxGapHours': 48})
        assert quality['summary']['qaStatus'] == 'clean'
        checks += 1
        try:
            inspect_source({**base, 'source': '../escape.csv'})
        except FieldError:
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
                raise FieldError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise FieldError('Worker request must be a JSON object')
            max_hash = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash, int) or isinstance(max_hash, bool) or not 1024 <= max_hash <= 2147483648:
                raise FieldError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash
            action = request.get('action')
            if action == 'inspect':
                result = inspect_source(request)
            elif action == 'qc':
                result = quality_control(request)
            elif action == 'align':
                result = align_sources(request)
            elif action == 'compare':
                result = compare_values(request)
            else:
                raise FieldError(f'Unsupported action: {action}')
    except FieldError as error:
        result = {'ok': False, 'error': {'code': 'FIELD_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'FIELD_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(json_safe(result), ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
