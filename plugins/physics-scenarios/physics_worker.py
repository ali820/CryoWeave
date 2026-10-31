#!/usr/bin/env python3
"""Analytic physics scenarios, ensembles and conservation checks."""
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

import numpy as np
import pandas as pd

MAX_STDIN_BYTES = 4 * 1024 * 1024


class PhysicsError(ValueError):
    pass


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise PhysicsError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def number(request: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < minimum or value > maximum:
        raise PhysicsError(f'{key} must be a finite number from {minimum} to {maximum}')
    return float(value)


def positive_list(request: dict[str, Any], key: str, default: list[float]) -> list[float]:
    values = request.get(key, default)
    if not isinstance(values, list) or not values or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < 0 for value in values):
        raise PhysicsError(f'{key} must be a non-empty array of non-negative finite numbers')
    return [float(value) for value in values]


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


def write_artifacts(request: dict[str, Any], filename: str, rows: list[dict[str, Any]], summary: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    table_path = output_root / filename
    manifest_path = output_root / 'manifest.json'
    if rows:
        with table_path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    else:
        table_path.write_text('', encoding='utf-8')
    manifest = {'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'scenarioOnly': True, 'parameters': parameters, 'summary': summary, 'outputs': {'table': hash_file(table_path, request['maxHashBytes'])}}
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {'batchId': batch_id, 'paths': {'table': str(table_path), 'manifest': str(manifest_path)}, 'statePaths': {'table': f'state://{batch_id}/{filename}', 'manifest': f'state://{batch_id}/manifest.json'}}


def thermal_values(conductivity: float, density: float, heat_capacity: float, mean_surface: float, amplitude: float, period_days: float, depth: float, time_days: float, phase_rad: float = 0.0) -> dict[str, float]:
    diffusivity_day = conductivity / (density * heat_capacity) * 86400
    omega = 2 * math.pi / period_days
    damping_depth = math.sqrt(2 * diffusivity_day / omega)
    attenuation = math.exp(-depth / damping_depth)
    phase_lag_days = depth / damping_depth / omega
    temperature = mean_surface + amplitude * attenuation * math.sin(omega * time_days - depth / damping_depth + phase_rad)
    return {'temperatureC': temperature, 'diffusivityM2PerDay': diffusivity_day, 'dampingDepthM': damping_depth, 'attenuation': attenuation, 'phaseLagDays': phase_lag_days}


def thermal(request: dict[str, Any]) -> dict[str, Any]:
    conductivity = number(request, 'conductivityWmK', 2.0, 0.0000001, 1000000)
    density = number(request, 'densityKgM3', 2400, 0.0000001, 100000000)
    heat_capacity = number(request, 'heatCapacityJkgK', 960, 0.0000001, 100000000)
    mean_surface = number(request, 'meanSurfaceC', 14.2, -1000, 1000)
    amplitude = number(request, 'amplitudeC', 12, 0, 1000)
    period_days = number(request, 'periodDays', 1, 0.000001, 1000000)
    phase = number(request, 'phaseRad', 0, -1000000, 1000000)
    depths = positive_list(request, 'depthsM', [0, 0.05, 0.1, 0.2])
    times = positive_list(request, 'timesDays', [0, 0.25, 0.5, 0.75, 1])
    rows = []
    for time in times:
        for depth in depths:
            values = thermal_values(conductivity, density, heat_capacity, mean_surface, amplitude, period_days, depth, time, phase)
            rows.append({'timeDays': time, 'depthM': depth, **values})
    summary = {'diffusivityM2PerDay': rows[0]['diffusivityM2PerDay'], 'dampingDepthM': rows[0]['dampingDepthM'], 'minTemperatureC': min(row['temperatureC'] for row in rows), 'maxTemperatureC': max(row['temperatureC'] for row in rows), 'rowCount': len(rows)}
    artifacts = write_artifacts(request, 'thermal_series.csv', rows, summary, {'model': 'analytic_periodic_conduction', 'conductivityWmK': conductivity, 'densityKgM3': density, 'heatCapacityJkgK': heat_capacity, 'periodDays': period_days, 'depthsM': depths, 'timesDays': times})
    return {'ok': True, 'action': 'thermal', 'scenarioOnly': True, 'summary': summary, 'rows': rows[:100], **artifacts}


def consolidation(request: dict[str, Any]) -> dict[str, Any]:
    cv = number(request, 'coefficientOfConsolidationM2PerYear', 1.0, 0.000000001, 100000000)
    drainage = number(request, 'drainagePathM', 1.0, 0.000001, 1000000)
    cc = number(request, 'compressionIndex', 0.2, 0.000000001, 1000)
    e0 = number(request, 'initialVoidRatio', 1.0, 0.000000001, 1000)
    initial_stress = number(request, 'initialStressKPa', 100, 0.000000001, 100000000)
    final_stress = number(request, 'finalStressKPa', 200, 0.000000001, 100000000)
    if final_stress <= initial_stress:
        raise PhysicsError('finalStressKPa must exceed initialStressKPa for compression scenario')
    thickness = number(request, 'layerThicknessM', 1.0, 0.000001, 1000000)
    times = positive_list(request, 'timesDays', [0, 30, 90, 180, 365])
    ultimate = cc / (1 + e0) * thickness * math.log10(final_stress / initial_stress)
    rows = []
    for time in times:
        tv = cv * (time / 365) / drainage ** 2
        degree = 2 * math.sqrt(tv / math.pi) if tv < 0.2 else 1 - 8 / math.pi ** 2 * math.exp(-math.pi ** 2 * tv / 4)
        degree = min(1.0, max(0.0, degree))
        settlement = degree * ultimate
        remaining = ultimate - settlement
        rows.append({'timeDays': time, 'timeFactor': tv, 'degreeConsolidation': degree, 'ultimateSettlementM': ultimate, 'settlementM': settlement, 'remainingSettlementM': remaining, 'conservationResidualM': settlement + remaining - ultimate})
    summary = {'ultimateSettlementM': ultimate, 'finalSettlementM': rows[-1]['settlementM'], 'finalDegreeConsolidation': rows[-1]['degreeConsolidation'], 'maxConservationResidualM': max(abs(row['conservationResidualM']) for row in rows), 'rangeCheck': all(0 <= row['degreeConsolidation'] <= 1 and row['settlementM'] <= ultimate for row in rows)}
    artifacts = write_artifacts(request, 'consolidation_series.csv', rows, summary, {'model': 'terzaghi_1d_consolidation', 'coefficientOfConsolidationM2PerYear': cv, 'drainagePathM': drainage, 'compressionIndex': cc, 'initialVoidRatio': e0, 'initialStressKPa': initial_stress, 'finalStressKPa': final_stress, 'layerThicknessM': thickness, 'timesDays': times})
    return {'ok': True, 'action': 'consolidation', 'scenarioOnly': True, 'summary': summary, 'rows': rows, **artifacts}


def ensemble(request: dict[str, Any]) -> dict[str, Any]:
    if request.get('scenario', 'thermal') != 'thermal':
        raise PhysicsError('This MVP ensemble currently supports scenario=thermal only')
    base = request.get('baseParameters')
    ranges = request.get('parameterRanges')
    if not isinstance(base, dict) or not isinstance(ranges, dict) or not ranges:
        raise PhysicsError('baseParameters and non-empty parameterRanges are required')
    allowed = {'conductivityWmK', 'densityKgM3', 'heatCapacityJkgK', 'meanSurfaceC', 'amplitudeC', 'periodDays', 'depthM', 'timeDays', 'phaseRad'}
    unknown = set(ranges) - allowed
    if unknown:
        raise PhysicsError(f'Unsupported ensemble parameters: {sorted(unknown)}')
    samples = int(number(request, 'samples', 100, 2, 10000))
    seed = int(number(request, 'randomState', 42, 0, 100000000))
    rng = np.random.default_rng(seed)
    sampled = {key: rng.uniform(float(ranges[key][0]), float(ranges[key][1]), samples) for key in sorted(ranges)}
    if len(sampled) != len(ranges):
        raise PhysicsError('Every parameter range must be [minimum, maximum]')
    rows = []
    for index in range(samples):
        params = {**base, **{key: float(values[index]) for key, values in sampled.items()}}
        for key in allowed - {'depthM', 'timeDays', 'phaseRad', 'meanSurfaceC', 'amplitudeC'}:
            if key in params and params[key] <= 0:
                raise PhysicsError(f'{key} must be positive')
        depth = float(params.get('depthM', 0.2))
        time = float(params.get('timeDays', 1))
        values = thermal_values(float(params.get('conductivityWmK', 2)), float(params.get('densityKgM3', 2400)), float(params.get('heatCapacityJkgK', 960)), float(params.get('meanSurfaceC', 14.2)), float(params.get('amplitudeC', 12)), float(params.get('periodDays', 1)), depth, time, float(params.get('phaseRad', 0)))
        rows.append({'sample': index + 1, 'depthM': depth, 'timeDays': time, **{key: params[key] for key in sorted(sampled)}, **values})
    temperatures = np.array([row['temperatureC'] for row in rows])
    summary = {'samples': samples, 'temperatureQuantilesC': {'p05': float(np.quantile(temperatures, 0.05)), 'p50': float(np.quantile(temperatures, 0.5)), 'p95': float(np.quantile(temperatures, 0.95))}, 'minTemperatureC': float(temperatures.min()), 'maxTemperatureC': float(temperatures.max()), 'rangeCheck': bool(np.isfinite(temperatures).all() and all(0 < row['attenuation'] <= 1 for row in rows))}
    artifacts = write_artifacts(request, 'ensemble.csv', rows, summary, {'model': 'analytic_periodic_conduction', 'baseParameters': base, 'parameterRanges': ranges, 'randomState': seed, 'sampling': 'uniform'})
    return {'ok': True, 'action': 'ensemble', 'scenarioOnly': True, 'summary': summary, 'rows': rows[:100], **artifacts}


def self_test() -> dict[str, Any]:
    import tempfile
    checks = 0
    with tempfile.TemporaryDirectory(prefix='physics-worker-') as temporary:
        base = {'stateRoot': str(Path(temporary) / 'state'), 'maxHashBytes': 1000000}
        result = thermal({**base, 'depthsM': [0, 0.1, 0.2], 'timesDays': [0, 1]})
        assert result['summary']['dampingDepthM'] > 0 and result['rows'][-1]['attenuation'] < 1
        checks += 1
        consolidated = consolidation({**base, 'timesDays': [0, 30, 365]})
        assert consolidated['summary']['maxConservationResidualM'] < 1e-12 and consolidated['summary']['rangeCheck']
        checks += 1
        ensemble_result = ensemble({**base, 'baseParameters': {'depthM': 0.2, 'timeDays': 1}, 'parameterRanges': {'conductivityWmK': [1.5, 2.5], 'amplitudeC': [10, 14]}, 'samples': 20})
        assert ensemble_result['summary']['samples'] == 20 and ensemble_result['summary']['rangeCheck']
        checks += 1
        try:
            thermal({**base, 'conductivityWmK': -1})
        except PhysicsError:
            checks += 1
        else:
            raise AssertionError('Negative conductivity accepted')
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise PhysicsError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise PhysicsError('Worker request must be a JSON object')
            max_hash = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash, int) or isinstance(max_hash, bool) or not 1024 <= max_hash <= 2147483648:
                raise PhysicsError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash
            action = request.get('action')
            if action == 'thermal':
                result = thermal(request)
            elif action == 'consolidation':
                result = consolidation(request)
            elif action == 'ensemble':
                result = ensemble(request)
            else:
                raise PhysicsError(f'Unsupported action: {action}')
    except PhysicsError as error:
        result = {'ok': False, 'error': {'code': 'PHYSICS_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'PHYSICS_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(json_safe(result), ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
