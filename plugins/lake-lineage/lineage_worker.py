#!/usr/bin/env python3
"""Annual lake lineage and present-day transport co-location worker."""
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

import geopandas as gpd
import numpy as np
from shapely import make_valid, transform as shapely_transform
from shapely.geometry import mapping
from shapely.strtree import STRtree
from pyproj import Transformer

MAX_STDIN_BYTES = 4 * 1024 * 1024


class LineageError(ValueError):
    pass


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise LineageError('projectRoot is required for local files')
    if not isinstance(allowed, list) or not allowed:
        raise LineageError('allowedRoots is required')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise LineageError(f'Project root is not a directory: {root}')
    for item in allowed:
        if isinstance(item, str) and inside(root, Path(item).expanduser().resolve()):
            return root
    raise LineageError(f'Project root is outside allowedRoots: {root}')


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise LineageError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_path(root: Path, raw: Any, state: Path | None = None) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise LineageError('File path must be a non-empty string')
    if raw.startswith('state://'):
        if state is None:
            raise LineageError('stateRoot is required for state:// paths')
        path = (state / raw[len('state://'):]).resolve()
        if not inside(path, state):
            raise LineageError('State file escapes stateRoot')
        return path
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if state is not None and inside(path, state):
        return path
    if not inside(path, root):
        raise LineageError(f'File escapes projectRoot: {raw}')
    if not path.is_file():
        raise LineageError(f'File not found: {raw}')
    return path


def number(request: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < minimum or value > maximum:
        raise LineageError(f'{key} must be a finite number from {minimum} to {maximum}')
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


def json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


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


def year_files(request: dict[str, Any]) -> list[dict[str, Any]]:
    raw = request.get('yearFiles')
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        raise LineageError('yearFiles must contain 1-100 entries')
    root = project_root(request)
    state = state_root(request)
    result = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {'path': entry}
        if not isinstance(entry, dict):
            raise LineageError('yearFiles entries must be paths or {year,path} objects')
        path = local_path(root, entry.get('path'), state)
        year = entry.get('year')
        if year is None:
            match = re.search(r'(19|20)\d{2}', path.name)
            if not match:
                raise LineageError(f'Cannot infer year from {path.name}; provide year explicitly')
            year = int(match.group(0))
        if not isinstance(year, int) or isinstance(year, bool) or not 1800 <= year <= 2200:
            raise LineageError(f'Invalid year: {year}')
        result.append({'year': year, 'path': path})
    result.sort(key=lambda item: item['year'])
    if len({item['year'] for item in result}) != len(result):
        raise LineageError('yearFiles must not contain duplicate years')
    return result


def load_nodes(request: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    working = request.get('workingCrs', 'EPSG:32645')
    simplify = number(request, 'simplifyMeters', 0, 0, 100000)
    max_per_year = int(number(request, 'maxPolygonsPerYear', 10000, 1, 20000))
    source_id_column = request.get('sourceIdColumn')
    source_ids = request.get('sourceIds')
    if source_ids is not None and (not isinstance(source_ids, list) or any(not isinstance(item, (str, int)) for item in source_ids)):
        raise LineageError('sourceIds must be a string or integer array')
    selected_ids = {str(item) for item in source_ids} if source_ids is not None else None
    nodes = []
    file_summaries = []
    for item in year_files(request):
        frame = gpd.read_file(item['path'])
        if frame.crs is None:
            raise LineageError(f'Year file has no CRS: {item["path"]}')
        frame = frame.to_crs(working)
        year = item['year']
        count = 0
        for source_index, geometry in enumerate(frame.geometry):
            if geometry is None or geometry.is_empty or geometry.geom_type not in {'Polygon', 'MultiPolygon'}:
                continue
            properties = frame.iloc[source_index].drop(labels='geometry').to_dict()
            if source_id_column and selected_ids is not None and str(properties.get(source_id_column)) not in selected_ids:
                continue
            if not geometry.is_valid:
                geometry = make_valid(geometry)
            if simplify > 0:
                geometry = geometry.simplify(simplify, preserve_topology=True)
            if geometry.area <= 0:
                continue
            count += 1
            node_id = str(properties[source_id_column]) if source_id_column else f'y{year}_{count:05d}'
            nodes.append({'id': node_id, 'key': f'y{year}_{count:05d}', 'year': year, 'index': source_index, 'geometry': geometry, 'area': geometry.area, 'properties': properties})
            if count >= max_per_year:
                break
        file_summaries.append({'year': year, 'path': str(item['path']), 'nodes': count, 'sourceFeatures': len(frame)})
    if not nodes:
        raise LineageError('No valid lake polygons were loaded')
    return nodes, file_summaries


def build_lineage(request: dict[str, Any]) -> dict[str, Any]:
    nodes, file_summaries = load_nodes(request)
    threshold = number(request, 'dominantOverlap', 0.95, 0.0001, 1.0)
    minor_overlap = number(request, 'minorOverlap', 0.001, 0, 1.0)
    if minor_overlap > threshold:
        raise LineageError('minorOverlap must not exceed dominantOverlap')
    parent = {node['key']: node['key'] for node in nodes}
    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value
    def union(first: str, second: str) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[second_root] = first_root
    events = []
    by_year = {}
    for node in nodes:
        by_year.setdefault(node['year'], []).append(node)
    years = sorted(by_year)
    for previous_year, current_year in zip(years, years[1:]):
        previous = by_year[previous_year]
        current = by_year[current_year]
        tree = STRtree([node['geometry'] for node in current])
        links = []
        for prev in previous:
            candidates = []
            minors = []
            for index in tree.query(prev['geometry']):
                curr = current[index]
                intersection = prev['geometry'].intersection(curr['geometry'])
                area = intersection.area
                if area <= 0:
                    continue
                ratio_a = area / prev['area']
                ratio_b = area / curr['area']
                candidate = (area, prev, curr, ratio_a, ratio_b)
                if ratio_a >= threshold and ratio_b >= threshold:
                    candidates.append(candidate)
                elif ratio_a >= minor_overlap or ratio_b >= minor_overlap:
                    minors.append(candidate)
            candidates.sort(key=lambda item: item[0], reverse=True)
            if candidates:
                best = candidates[0]
                if len(candidates) > 1 and best[0] < candidates[1][0] * 1.2:
                    events.append({'year': current_year, 'type': 'ambiguous', 'from': prev['id'], 'to': [item[2]['id'] for item in candidates], 'areaKm2': best[0] / 1_000_000})
                else:
                    links.append(best)
            for minor in minors:
                if any(link[1]['key'] == minor[1]['key'] for link in links):
                    continue
                events.append({'year': current_year, 'type': 'minor_exchange', 'from': prev['id'], 'to': minor[2]['id'], 'areaKm2': minor[0] / 1_000_000, 'ratioPrevious': minor[3], 'ratioCurrent': minor[4]})
            if not candidates and not minors:
                events.append({'year': current_year, 'type': 'disappear', 'from': prev['id'], 'areaKm2': prev['area'] / 1_000_000})
            elif not candidates and len(minors) > 1:
                events.append({'year': current_year, 'type': 'split', 'from': prev['id'], 'to': [item[2]['id'] for item in minors], 'areaKm2': prev['area'] / 1_000_000})
        linked_current = {link[2]['key'] for link in links}
        for curr in current:
            if curr['key'] not in linked_current:
                events.append({'year': current_year, 'type': 'new', 'to': curr['id'], 'areaKm2': curr['area'] / 1_000_000})
        for _, prev, curr, _, _ in links:
            previous_roots = {find(link[1]['key']) for link in links if link[2]['key'] == curr['key']}
            current_targets = {link[2]['key'] for link in links if link[1]['key'] == prev['key']}
            if len(previous_roots) > 1:
                events.append({'year': current_year, 'type': 'merge', 'from': prev['id'], 'to': curr['id']})
            if len(current_targets) > 1:
                events.append({'year': current_year, 'type': 'split', 'from': prev['id'], 'to': curr['id']})
            union(prev['key'], curr['key'])
    groups = {}
    for node in nodes:
        groups.setdefault(find(node['key']), []).append(node)
    cohort = []
    for members in groups.values():
        years_in_group = sorted({node['year'] for node in members})
        first_year, last_year = years_in_group[0], years_in_group[-1]
        complete = years_in_group == list(range(first_year, last_year + 1))
        first_area = sum(node['area'] for node in members if node['year'] == first_year) / 1_000_000
        last_area = sum(node['area'] for node in members if node['year'] == last_year) / 1_000_000
        cohort.append({
            'firstYear': first_year, 'lastYear': last_year, 'yearsObserved': len(years_in_group),
            'completeSeries': complete, 'firstAreaKm2': first_area, 'lastAreaKm2': last_area,
            'deltaKm2': last_area - first_area,
            'deltaPercent': (last_area - first_area) / first_area * 100 if first_area > 0 else None,
            'members': members,
        })
    cohort.sort(key=lambda item: (item['firstYear'], item['lastYear'], -item['lastAreaKm2']))
    start_year = request.get('startYear')
    end_year = request.get('endYear')
    if start_year is not None and (not isinstance(start_year, int) or isinstance(start_year, bool)):
        raise LineageError('startYear must be an integer')
    if end_year is not None and (not isinstance(end_year, int) or isinstance(end_year, bool)):
        raise LineageError('endYear must be an integer')
    for index, item in enumerate(cohort, start=1):
        item['lineageId'] = f'lineage_{index:05d}'
        item['eligible'] = (
            (start_year is None or item['firstYear'] <= start_year)
            and (end_year is None or item['lastYear'] >= end_year)
            and (not request.get('requireComplete', True) or item['completeSeries'])
        )
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    lineage_path = output_root / 'lineage.csv'
    events_path = output_root / 'events.csv'
    cohort_path = output_root / 'cohort.geojson'
    manifest_path = output_root / 'manifest.json'
    fields = ['lineageId', 'firstYear', 'lastYear', 'yearsObserved', 'completeSeries', 'eligible', 'firstAreaKm2', 'lastAreaKm2', 'deltaKm2', 'deltaPercent', 'memberCount']
    with lineage_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in cohort:
            writer.writerow({**{key: item[key] for key in fields if key != 'memberCount'}, 'memberCount': len(item['members'])})
    with events_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['year', 'type', 'from', 'to', 'areaKm2', 'ratioPrevious', 'ratioCurrent'])
        writer.writeheader()
        for event in events:
            writer.writerow({
                'year': event.get('year'), 'type': event.get('type'), 'from': event.get('from'),
                'to': ';'.join(event['to']) if isinstance(event.get('to'), list) else event.get('to', ''),
                'areaKm2': event.get('areaKm2'), 'ratioPrevious': event.get('ratioPrevious'), 'ratioCurrent': event.get('ratioCurrent'),
            })
    features = []
    for item in cohort:
        if not item['eligible']:
            continue
        last_nodes = [node for node in item['members'] if node['year'] == item['lastYear']]
        geometry = gpd.GeoSeries([node['geometry'] for node in last_nodes]).union_all()
        properties = {key: item[key] for key in fields if key != 'memberCount'}
        properties['memberCount'] = len(item['members'])
        features.append({'type': 'Feature', 'geometry': mapping(geometry), 'properties': properties})
    gpd.GeoDataFrame.from_features(features, crs=request.get('workingCrs', 'EPSG:32645')).to_crs('EPSG:4326').to_file(cohort_path, driver='GeoJSON')
    event_types = Counter(event['type'] for event in events)
    manifest = {
        'batchId': batch_id,
        'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'parameters': {
            'workingCrs': request.get('workingCrs', 'EPSG:32645'), 'dominantOverlap': threshold,
            'minorOverlap': minor_overlap, 'startYear': start_year, 'endYear': end_year,
            'requireComplete': request.get('requireComplete', True),
        },
        'years': file_summaries,
        'counts': {'lineages': len(cohort), 'eligibleLineages': sum(item['eligible'] for item in cohort), 'events': len(events), 'eventTypes': dict(event_types)},
        'inputs': [hash_file(item['path'], request['maxHashBytes']) for item in year_files(request)],
        'outputs': {
            'lineage': hash_file(lineage_path, request['maxHashBytes']),
            'events': hash_file(events_path, request['maxHashBytes']),
            'cohort': hash_file(cohort_path, request['maxHashBytes']),
        },
    }
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    relative = lambda path: f"state://{path.relative_to(state_root(request)).as_posix()}"
    public_cohort = [{key: value for key, value in item.items() if key not in {'members'}} for item in cohort[:100]]
    return {
        'ok': True, 'action': 'build', 'batchId': batch_id,
        'summary': manifest['counts'], 'rows': public_cohort, 'eventTypes': dict(event_types),
        'paths': {'lineage': str(lineage_path), 'events': str(events_path), 'cohort': str(cohort_path), 'manifest': str(manifest_path)},
        'statePaths': {'lineage': relative(lineage_path), 'events': relative(events_path), 'cohort': relative(cohort_path), 'manifest': relative(manifest_path)},
    }


def inspect(request: dict[str, Any]) -> dict[str, Any]:
    _, summaries = load_nodes(request)
    return {'ok': True, 'action': 'inspect', 'years': summaries}


def transform_frame(frame: gpd.GeoDataFrame, working: str) -> gpd.GeoDataFrame:
    if frame.crs is not None and str(frame.crs).upper() == working.upper():
        return frame
    transformed = frame.copy()
    transformer = Transformer.from_crs(str(frame.crs), working, always_xy=True)
    transformed['geometry'] = frame.geometry.map(lambda geometry: shapely_transform(geometry, lambda coords: np.asarray(transformer.transform(coords[:, 0], coords[:, 1])).T))
    transformed.set_crs(working, allow_override=True, inplace=True)
    return transformed


def colocate(request: dict[str, Any]) -> dict[str, Any]:
    root = project_root(request)
    state = state_root(request)
    cohort_path = local_path(root, request.get('cohort'), state)
    transport_path = local_path(root, request.get('transport'), state)
    working = request.get('workingCrs', 'EPSG:32645')
    cohort = gpd.read_file(cohort_path)
    if cohort.crs is None:
        cohort = cohort.set_crs(request.get('sourceCrs', 'EPSG:4326'))
    transport = gpd.read_file(transport_path)
    if transport.crs is None:
        transport = transport.set_crs(request.get('transportCrs', request.get('sourceCrs', 'EPSG:4326')))
    cohort = transform_frame(cohort, working)
    transport = transform_frame(transport, working)
    transport = transport[transport.geometry.notna() & ~transport.geometry.is_empty & transport.geometry.is_valid]
    if transport.empty:
        raise LineageError('Transport has no valid geometries after filtering')
    thresholds = request.get('distanceThresholdsKm', [1, 3, 5, 10])
    if not isinstance(thresholds, list) or not thresholds or any(not isinstance(value, (int, float)) or value <= 0 for value in thresholds):
        raise LineageError('distanceThresholdsKm must be positive numbers')
    limit = request.get('limit', 100)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise LineageError('limit must be an integer from 1 to 1000')
    tree = STRtree(list(transport.geometry))
    rows = []
    warnings = []
    for _, feature in cohort.iterrows():
        geometry = feature.geometry
        if geometry is None or geometry.is_empty:
            continue
        index = tree.nearest(geometry)
        nearest = transport.iloc[index]
        distance = geometry.distance(nearest.geometry) / 1000
        if not math.isfinite(distance):
            warnings.append({'code': 'DISTANCE_UNAVAILABLE', 'lineageId': feature.get('lineageId')})
            continue
        properties = {key: nearest[key] for key in ('osm_id', 'name', 'ref', 'fclass', 'highway', 'railway', 'type') if key in nearest and nearest[key] is not None}
        row = {
            'lineageId': feature.get('lineageId'), 'firstYear': feature.get('firstYear'), 'lastYear': feature.get('lastYear'),
            'firstAreaKm2': feature.get('firstAreaKm2'), 'lastAreaKm2': feature.get('lastAreaKm2'),
            'nearestDistanceKm': distance, 'nearestProperties': properties,
        }
        for threshold in thresholds:
            row[f'within{threshold:g}Km'] = distance <= threshold
        rows.append(row)
        if len(rows) >= limit:
            break
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    table_path = output_root / 'colocation.csv'
    manifest_path = output_root / 'manifest.json'
    with table_path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['lineageId', 'firstYear', 'lastYear', 'firstAreaKm2', 'lastAreaKm2', 'nearestDistanceKm', 'nearestProperties', *{f'within{value:g}Km' for value in thresholds}])
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, 'nearestProperties': json.dumps(json_safe(row['nearestProperties']), ensure_ascii=False)})
    manifest = {
        'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'cohort': str(cohort_path), 'transport': str(transport_path), 'workingCrs': working,
        'distanceThresholdsKm': thresholds, 'count': len(rows), 'warnings': warnings,
        'outputs': {'colocation': hash_file(table_path, request['maxHashBytes'])},
    }
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {
        'ok': True, 'action': 'colocate', 'batchId': batch_id, 'summary': {'count': len(rows), 'thresholds': thresholds, 'warnings': warnings},
        'rows': rows, 'paths': {'colocation': str(table_path), 'manifest': str(manifest_path)},
        'statePaths': {'colocation': f'state://{batch_id}/colocation.csv', 'manifest': f'state://{batch_id}/manifest.json'},
    }


def self_test() -> dict[str, Any]:
    import tempfile
    checks = 0
    with tempfile.TemporaryDirectory(prefix='lineage-worker-') as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        state = root / 'state'

        def polygon(x: float, size: float = 1000) -> dict[str, Any]:
            return {'type': 'Polygon', 'coordinates': [[[x, 4700000], [x + size, 4700000], [x + size, 4700000 + size], [x, 4700000 + size], [x, 4700000]]]}

        def write_lakes(path: Path, rows: list[tuple[str | None, dict[str, Any]]]) -> None:
            features = [
                {'type': 'Feature', 'geometry': geometry, 'properties': {} if lake_id is None else {'lake_id': lake_id}}
                for lake_id, geometry in rows
            ]
            gpd.GeoDataFrame.from_features(features, crs='EPSG:32645').to_file(path, driver='GeoJSON')

        def request_for(paths: list[str], **overrides: Any) -> dict[str, Any]:
            request = {
                'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state),
                'yearFiles': paths, 'workingCrs': 'EPSG:32645', 'dominantOverlap': 0.95,
                'minorOverlap': 0.001, 'maxHashBytes': 1000000,
            }
            request.update(overrides)
            return request

        def read_events(result: dict[str, Any]) -> list[dict[str, str]]:
            with open(result['paths']['events'], encoding='utf-8-sig', newline='') as stream:
                return list(csv.DictReader(stream))

        for year, geometries in {2020: [polygon(500000), polygon(501000)], 2021: [polygon(500000), polygon(501000, 500), polygon(501500, 400), polygon(504000)], 2022: [polygon(500000), polygon(501000), polygon(504000)]}.items():
            write_lakes(project / f'lakes_{year}.geojson', [(None, geometry) for geometry in geometries])
        request = request_for([f'lakes_{year}.geojson' for year in (2020, 2021, 2022)], dominantOverlap=0.9)
        result = build_lineage(request)
        assert result['summary']['lineages'] >= 2 and result['summary']['events'] > 0
        checks += 1
        assert any(item['completeSeries'] for item in result['rows'])
        checks += 1
        split_events = [row for row in read_events(result) if row['type'] == 'split' and row['from'] == 'y2020_00002']
        assert len(split_events) == 1
        assert set(split_events[0]['to'].split(';')) == {'y2021_00002', 'y2021_00003'}
        checks += 1
        transport = project / 'roads.geojson'
        gpd.GeoDataFrame.from_features([{'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': [[500000, 4700000], [501000, 4700000]]}, 'properties': {'name': 'demo road'}}], crs='EPSG:32645').to_file(transport, driver='GeoJSON')
        nearest = colocate({**request, 'cohort': result['paths']['cohort'], 'transport': 'roads.geojson'})
        assert nearest['summary']['count'] >= 1 and nearest['rows'][0]['nearestDistanceKm'] < 1000
        checks += 1

        # A source column without a whitelist must load every ID, and repeated
        # external IDs must not alias unrelated annual nodes in the union-find.
        identified_paths = ['identified_2020.geojson', 'identified_2021.geojson']
        write_lakes(project / identified_paths[0], [('stable', polygon(510000)), ('reused', polygon(520000))])
        write_lakes(project / identified_paths[1], [('stable', polygon(510000)), ('reused', polygon(530000))])
        identified_request = request_for(identified_paths, sourceIdColumn='lake_id')
        identified_summary = inspect(identified_request)
        assert [item['nodes'] for item in identified_summary['years']] == [2, 2]
        checks += 1
        filtered_summary = inspect({**identified_request, 'sourceIds': ['stable']})
        assert [item['nodes'] for item in filtered_summary['years']] == [1, 1]
        checks += 1
        identified_result = build_lineage(identified_request)
        assert identified_result['summary']['lineages'] == 3
        assert sorted(item['yearsObserved'] for item in identified_result['rows']) == [1, 1, 2]
        checks += 1

        # Two similarly dominant successors must be reported as current nodes,
        # not as repeated references to the previous node.
        ambiguous_paths = ['ambiguous_2020.geojson', 'ambiguous_2021.geojson']
        write_lakes(project / ambiguous_paths[0], [(None, polygon(540000))])
        write_lakes(project / ambiguous_paths[1], [(None, polygon(540000)), (None, polygon(540010))])
        ambiguous_result = build_lineage(request_for(ambiguous_paths))
        ambiguous_events = [row for row in read_events(ambiguous_result) if row['type'] == 'ambiguous']
        assert len(ambiguous_events) == 1
        assert ambiguous_events[0]['from'] == 'y2020_00001'
        assert set(ambiguous_events[0]['to'].split(';')) == {'y2021_00001', 'y2021_00002'}
        checks += 1

        # The asymmetric overlap makes the previous/current ratios distinguishable.
        minor_paths = ['minor_2020.geojson', 'minor_2021.geojson']
        write_lakes(project / minor_paths[0], [(None, polygon(550000, 1000))])
        write_lakes(project / minor_paths[1], [(None, polygon(550000, 8000))])
        minor_result = build_lineage(request_for(minor_paths))
        minor_events = [row for row in read_events(minor_result) if row['type'] == 'minor_exchange']
        assert len(minor_events) == 1
        assert minor_events[0]['from'] == 'y2020_00001' and minor_events[0]['to'] == 'y2021_00001'
        assert math.isclose(float(minor_events[0]['ratioPrevious']), 1.0, abs_tol=1e-12)
        assert math.isclose(float(minor_events[0]['ratioCurrent']), 0.015625, abs_tol=1e-12)
        checks += 1

        try:
            build_lineage({**request, 'yearFiles': ['../escape.geojson']})
        except LineageError:
            checks += 1
        else:
            raise AssertionError('Escaping year file accepted')
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise LineageError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise LineageError('Worker request must be a JSON object')
            max_hash = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash, int) or isinstance(max_hash, bool) or not 1024 <= max_hash <= 2147483648:
                raise LineageError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash
            action = request.get('action')
            if action == 'inspect':
                result = inspect(request)
            elif action == 'build':
                result = build_lineage(request)
            elif action == 'colocate':
                result = colocate(request)
            else:
                raise LineageError(f'Unsupported action: {action}')
    except LineageError as error:
        result = {'ok': False, 'error': {'code': 'LINEAGE_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'LINEAGE_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(json_safe(result), ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
