#!/usr/bin/env python3
"""Bounded Google Earth Engine worker for the DSH GEE analysis plugin."""

from __future__ import annotations

import json
import math
import re
import sys
import time
import uuid
import csv
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPERATIONS = {
    "status",
    "collection_summary",
    "region_stats",
    "timeseries",
    "ndvi_timeseries",
    "ndvi_stats",
    "corridor_ndvi_zonal",
}
STATS = {"mean", "median", "min", "max", "sum", "stdDev", "variance", "count"}
COMPOSITES = {"median", "mean", "first", "mosaic"}
COLLECTION_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
BAND_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"


class UserInputError(ValueError):
    pass


def number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise UserInputError(f"{name} must be a finite number")
    if value < minimum or value > maximum:
        raise UserInputError(f"{name} must be between {minimum} and {maximum}")
    return value


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UserInputError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise UserInputError(f"{name} must be between {minimum} and {maximum}")
    return value


def dates(request: dict[str, Any]) -> tuple[str, str]:
    start = request.get("startDate")
    end = request.get("endDate")
    if not isinstance(start, str) or not DATE_PATTERN.fullmatch(start):
        raise UserInputError("startDate must use YYYY-MM-DD")
    if not isinstance(end, str) or not DATE_PATTERN.fullmatch(end):
        raise UserInputError("endDate must use YYYY-MM-DD")
    try:
        start_value = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_value = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise UserInputError(f"Invalid calendar date: {error}") from error
    if end_value <= start_value:
        raise UserInputError("endDate must be later than startDate; endDate is exclusive")
    return start, end


def collection_id(request: dict[str, Any], default: str | None = None) -> str:
    value = request.get("collection", default)
    if not isinstance(value, str) or not COLLECTION_PATTERN.fullmatch(value) or ".." in value:
        raise UserInputError("collection must be a valid Earth Engine ImageCollection id")
    return value


def bands(request: dict[str, Any], default: list[str] | None = None) -> list[str]:
    value = request.get("bands", default)
    if not isinstance(value, list) or not value or len(value) > 8:
        raise UserInputError("bands must contain between 1 and 8 band names")
    if any(not isinstance(band, str) or not BAND_PATTERN.fullmatch(band) for band in value):
        raise UserInputError("band names may contain only letters, numbers, and underscores")
    if len(set(value)) != len(value):
        raise UserInputError("bands must not contain duplicates")
    return value


def geometry_spec(request: dict[str, Any], required: bool = True) -> dict[str, Any] | None:
    geometry = request.get("geometry")
    asset = request.get("geometryAsset")
    longitude = request.get("longitude")
    latitude = request.get("latitude")
    sources = sum((geometry is not None, asset is not None, longitude is not None or latitude is not None))
    if sources > 1:
        raise UserInputError("Use only one of geometry, geometryAsset, or longitude/latitude")
    if asset is not None:
        if not isinstance(asset, str) or not COLLECTION_PATTERN.fullmatch(asset) or ".." in asset:
            raise UserInputError("geometryAsset must be a valid Earth Engine FeatureCollection asset id")
        buffer = request.get("bufferMeters")
        if buffer is not None:
            buffer = number(buffer, "bufferMeters", 1, 1_000_000)
        return {"kind": "asset", "value": asset, "bufferMeters": buffer}
    if geometry is not None:
        if not isinstance(geometry, dict):
            raise UserInputError("geometry must be a GeoJSON geometry or Feature object")
        if geometry.get("type") == "Feature":
            geometry = geometry.get("geometry")
        if not isinstance(geometry, dict) or not isinstance(geometry.get("type"), str):
            raise UserInputError("GeoJSON geometry must contain a valid type")
        allowed = {
            "Point", "MultiPoint", "LineString", "MultiLineString",
            "Polygon", "MultiPolygon", "GeometryCollection",
        }
        if geometry["type"] not in allowed:
            raise UserInputError(f"Unsupported GeoJSON geometry type: {geometry['type']}")
        if geometry["type"] == "GeometryCollection":
            if not isinstance(geometry.get("geometries"), list):
                raise UserInputError("GeometryCollection must contain a geometries array")
        elif "coordinates" not in geometry:
            raise UserInputError("GeoJSON geometry must contain coordinates")
        return {"kind": "geojson", "value": geometry}
    if longitude is not None or latitude is not None:
        if longitude is None or latitude is None:
            raise UserInputError("longitude and latitude must be supplied together")
        lon = number(longitude, "longitude", -180, 180)
        lat = number(latitude, "latitude", -90, 90)
        buffer = number(request.get("bufferMeters", 1000), "bufferMeters", 1, 1_000_000)
        return {"kind": "point", "longitude": lon, "latitude": lat, "bufferMeters": buffer}
    if required:
        raise UserInputError("Supply a GeoJSON geometry, geometryAsset, or longitude and latitude")
    return None


def make_geometry(ee: Any, spec: dict[str, Any] | None) -> Any:
    if spec is None:
        return None
    if spec["kind"] == "geojson":
        return ee.Geometry(spec["value"])
    if spec["kind"] == "asset":
        geometry = ee.FeatureCollection(spec["value"]).geometry()
        return geometry.buffer(spec["bufferMeters"]) if spec["bufferMeters"] is not None else geometry
    return ee.Geometry.Point([spec["longitude"], spec["latitude"]]).buffer(spec["bufferMeters"])


def reduction_options(request: dict[str, Any], geometry: Any, default_scale: float) -> dict[str, Any]:
    scale = number(request.get("scale", default_scale), "scale", 0.000001, 1_000_000)
    max_pixels = number(request.get("maxPixels", 1_000_000_000), "maxPixels", 1, 10_000_000_000_000)
    tile_scale = number(request.get("tileScale", 1), "tileScale", 1, 16)
    best_effort = request.get("bestEffort", False)
    if not isinstance(best_effort, bool):
        raise UserInputError("bestEffort must be a boolean")
    options = {
        "geometry": geometry,
        "scale": scale,
        "maxPixels": max_pixels,
        "bestEffort": best_effort,
        "tileScale": tile_scale,
    }
    crs = request.get("crs")
    if crs is not None:
        if not isinstance(crs, str) or not crs.strip() or len(crs) > 200:
            raise UserInputError("crs must be a short non-empty string")
        options["crs"] = crs.strip()
    return options


def reducer(ee: Any, request: dict[str, Any]) -> tuple[str, Any]:
    name = request.get("stat", "mean")
    if name not in STATS:
        raise UserInputError(f"stat must be one of: {', '.join(sorted(STATS))}")
    factory = getattr(ee.Reducer, name)
    return name, factory()


def optional_limit(request: dict[str, Any], default: int | None, maximum: int) -> int | None:
    value = request.get("limit", default)
    if value is None:
        return None
    return integer(value, "limit", 1, maximum)


def cloud_max(request: dict[str, Any], default: float | None = None) -> float | None:
    value = request.get("cloudMax", default)
    if value is None:
        return None
    return number(value, "cloudMax", 0, 100)


def make_collection(
    ee: Any,
    request: dict[str, Any],
    geometry: Any,
    default_collection: str | None = None,
    default_cloud_max: float | None = None,
) -> tuple[Any, dict[str, Any]]:
    start, end = dates(request)
    collection_name = collection_id(request, default_collection)
    cloud = cloud_max(request, default_cloud_max)
    collection = ee.ImageCollection(collection_name).filterDate(start, end)
    if geometry is not None:
        collection = collection.filterBounds(geometry)
    if cloud is not None:
        collection = collection.filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud))
    return collection, {
        "collection": collection_name,
        "startDate": start,
        "endDateExclusive": end,
        "cloudMax": cloud,
    }


def iso_time(milliseconds: Any) -> str | None:
    if milliseconds is None:
        return None
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def collection_summary(ee: Any, request: dict[str, Any]) -> dict[str, Any]:
    spec = geometry_spec(request, required=False)
    geometry = make_geometry(ee, spec)
    collection, metadata = make_collection(ee, request, geometry)
    sample_limit = optional_limit(request, 10, 50)
    summary = ee.Dictionary({
        "imageCount": collection.size(),
        "firstTime": collection.aggregate_min("system:time_start"),
        "lastTime": collection.aggregate_max("system:time_start"),
        "sampleIds": collection.sort("system:time_start").limit(sample_limit).aggregate_array("system:index"),
    }).getInfo()
    return {
        "ok": True,
        "operation": "collection_summary",
        **metadata,
        "imageCount": summary.get("imageCount", 0),
        "firstTime": iso_time(summary.get("firstTime")),
        "lastTime": iso_time(summary.get("lastTime")),
        "sampleIds": summary.get("sampleIds") or [],
    }


def run_timeseries(
    ee: Any,
    request: dict[str, Any],
    collection: Any,
    geometry: Any,
    selected_bands: list[str],
    default_scale: float,
    include_count: bool = False,
) -> dict[str, Any]:
    stat_name, stat_reducer = reducer(ee, request)
    options = reduction_options(request, geometry, default_scale)
    limit = optional_limit(request, 100, 500)
    drop_null = request.get("dropNull", True)
    if not isinstance(drop_null, bool):
        raise UserInputError("dropNull must be a boolean")
    limited = collection.sort("system:time_start").limit(limit)

    def image_to_row(image: Any) -> Any:
        image = ee.Image(image).select(selected_bands)
        properties = {
            "imageId": image.get("system:index"),
            "time": image.get("system:time_start"),
            "values": image.reduceRegion(reducer=stat_reducer, **options),
        }
        if include_count:
            properties["pixelCount"] = image.reduceRegion(reducer=ee.Reducer.count(), **options)
        return ee.Dictionary(properties)

    rows = limited.toList(limit).map(image_to_row)
    payload = ee.Dictionary({"results": rows, "sourceImageCount": collection.size()}).getInfo()
    rows = []
    dropped_null = 0
    for properties in payload.get("results") or []:
        values = properties.get("values") or {}
        if drop_null and all(values.get(band) is None for band in selected_bands):
            dropped_null += 1
            continue
        row = {
            "imageId": properties.get("imageId"),
            "time": iso_time(properties.get("time")),
            "values": values,
        }
        if include_count:
            row["pixelCount"] = properties.get("pixelCount") or {}
        rows.append(row)
    source_count = payload.get("sourceImageCount", 0)
    return {
        "imageCount": source_count,
        "returnedRows": len(rows),
        "droppedNullRows": dropped_null,
        "limit": limit,
        "sourceImagesLimited": source_count > limit,
        "stat": stat_name,
        "scale": options["scale"],
        "rows": rows,
    }


def timeseries(ee: Any, request: dict[str, Any]) -> dict[str, Any]:
    geometry = make_geometry(ee, geometry_spec(request))
    selected_bands = bands(request)
    collection, metadata = make_collection(ee, request, geometry)
    return {
        "ok": True,
        "operation": "timeseries",
        **metadata,
        "bands": selected_bands,
        **run_timeseries(ee, request, collection, geometry, selected_bands, 30),
    }


def region_stats(ee: Any, request: dict[str, Any]) -> dict[str, Any]:
    geometry = make_geometry(ee, geometry_spec(request))
    selected_bands = bands(request)
    collection, metadata = make_collection(ee, request, geometry)
    stat_name, stat_reducer = reducer(ee, request)
    options = reduction_options(request, geometry, 30)
    composite = request.get("composite", "median")
    if composite not in COMPOSITES:
        raise UserInputError(f"composite must be one of: {', '.join(sorted(COMPOSITES))}")
    limit = optional_limit(request, None, 500)
    source_count = collection.size()
    if limit is not None:
        collection = collection.sort("system:time_start").limit(limit)
    if composite == "median":
        image = collection.median()
    elif composite == "mean":
        image = collection.mean()
    elif composite == "first":
        image = collection.sort("system:time_start").first()
    else:
        image = collection.mosaic()
    stats = image.select(selected_bands).reduceRegion(reducer=stat_reducer, **options)
    payload = ee.Dictionary({"stats": stats, "imageCount": source_count}).getInfo()
    return {
        "ok": True,
        "operation": "region_stats",
        **metadata,
        "bands": selected_bands,
        "composite": composite,
        "stat": stat_name,
        "scale": options["scale"],
        "imageCount": payload.get("imageCount", 0),
        "compositeImageLimit": limit,
        "stats": payload.get("stats") or {},
    }


def mask_sentinel2(image: Any) -> Any:
    scl = image.select("SCL")
    # Keep SCL classes 2, 4, 5, 6, and 7; remove no-data, saturated,
    # cloud shadow, cloud, cirrus, and snow/ice classes.
    valid = scl.eq(2).Or(scl.eq(4)).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7))
    qa60 = image.select("QA60")
    clear = qa60.bitwiseAnd(1 << 10).eq(0).And(qa60.bitwiseAnd(1 << 11).eq(0))
    return image.updateMask(valid.And(clear))


def ndvi_collection(ee: Any, request: dict[str, Any], geometry: Any) -> tuple[Any, dict[str, Any]]:
    collection, metadata = make_collection(ee, request, geometry, S2_COLLECTION, 60)
    mask_clouds = request.get("maskClouds", True)
    if not isinstance(mask_clouds, bool):
        raise UserInputError("maskClouds must be a boolean")
    if mask_clouds:
        collection = collection.map(mask_sentinel2)

    def add_ndvi(image: Any) -> Any:
        return image.addBands(image.normalizedDifference(["B8", "B4"]).rename("NDVI"))

    return collection.map(add_ndvi), {**metadata, "maskClouds": mask_clouds, "index": "NDVI=(B8-B4)/(B8+B4)"}


def ndvi_timeseries(ee: Any, request: dict[str, Any]) -> dict[str, Any]:
    geometry = make_geometry(ee, geometry_spec(request))
    collection, metadata = ndvi_collection(ee, request, geometry)
    return {
        "ok": True,
        "operation": "ndvi_timeseries",
        **metadata,
        **run_timeseries(ee, request, collection, geometry, ["NDVI"], 10, include_count=True),
    }


def ndvi_stats(ee: Any, request: dict[str, Any]) -> dict[str, Any]:
    geometry = make_geometry(ee, geometry_spec(request))
    collection, metadata = ndvi_collection(ee, request, geometry)
    stat_name, stat_reducer = reducer(ee, request)
    options = reduction_options(request, geometry, 10)
    limit = optional_limit(request, None, 500)
    source_count = collection.size()
    if limit is not None:
        collection = collection.sort("system:time_start").limit(limit)
    image = collection.select("NDVI").median()
    stats = image.reduceRegion(reducer=stat_reducer, **options)
    counts = image.reduceRegion(reducer=ee.Reducer.count(), **options)
    payload = ee.Dictionary({
        "stats": stats,
        "pixelCount": counts,
        "imageCount": source_count,
    }).getInfo()
    return {
        "ok": True,
        "operation": "ndvi_stats",
        **metadata,
        "composite": "median",
        "stat": stat_name,
        "scale": options["scale"],
        "imageCount": payload.get("imageCount", 0),
        "compositeImageLimit": limit,
        "stats": payload.get("stats") or {},
        "pixelCount": payload.get("pixelCount") or {},
    }


def corridor_state_root(request: dict[str, Any]) -> Path:
    raw = request.get("stateRoot")
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise UserInputError("stateRoot must be an absolute path supplied by plugin config")
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def corridor_project_root(request: dict[str, Any]) -> Path:
    raw = request.get("projectRoot")
    allowed = request.get("allowedRoots")
    if not isinstance(raw, str) or not raw.strip():
        raise UserInputError("projectRoot is required for local unit files")
    if not isinstance(allowed, list) or not allowed:
        raise UserInputError("allowedRoots is required for local unit files")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise UserInputError(f"Project root is not a directory: {root}")
    for item in allowed:
        if isinstance(item, str):
            parent = Path(item).expanduser().resolve()
            if root == parent or parent in root.parents:
                return root
    raise UserInputError(f"Project root is outside allowedRoots: {root}")


def corridor_table_path(request: dict[str, Any]) -> Path:
    raw = request.get("table")
    if not isinstance(raw, str) or not raw.strip():
        raise UserInputError("table is required for corridor_ndvi_zonal")
    if raw.startswith("state://"):
        root = corridor_state_root(request)
        path = (root / raw[len("state://"):]).resolve()
        if not (path == root or root in path.parents):
            raise UserInputError("Unit table escapes stateRoot")
        return path
    root = corridor_project_root(request)
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not (path == root or root in path.parents):
        raise UserInputError("Unit table escapes projectRoot")
    return path


def file_sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_corridor_units(ee: Any, request: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    path = corridor_table_path(request)
    if not path.is_file():
        raise UserInputError(f"Unit GeoJSON not found: {path}")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise UserInputError("Unit GeoJSON exceeds the 32 MiB limit")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list) or not features:
        raise UserInputError("Unit GeoJSON must contain a non-empty features array")
    unit_ids = request.get("unitIds")
    ring_ids = request.get("ringIds")
    if unit_ids is not None and (not isinstance(unit_ids, list) or any(not isinstance(item, str) for item in unit_ids)):
        raise UserInputError("unitIds must be a string array")
    if ring_ids is not None and (not isinstance(ring_ids, list) or any(not isinstance(item, str) for item in ring_ids)):
        raise UserInputError("ringIds must be a string array")
    unit_filter = set(unit_ids) if unit_ids is not None else None
    ring_filter = set(ring_ids) if ring_ids is not None else None
    selected = []
    for feature in features:
        if not isinstance(feature, dict) or not feature.get("geometry") or not isinstance(feature.get("properties"), dict):
            raise UserInputError("Every unit must have geometry and properties")
        properties = feature["properties"]
        if "segment_id" not in properties or "ring_id" not in properties:
            raise UserInputError("Every unit must contain segment_id and ring_id")
        if unit_filter is not None and properties["segment_id"] not in unit_filter:
            continue
        if ring_filter is not None and properties["ring_id"] not in ring_filter:
            continue
        selected.append(feature)
    limit = integer(request.get("limit", 60), "limit", 1, 500)
    selected = selected[:limit]
    if not selected:
        raise UserInputError("No units remain after unitIds/ringIds filters")
    ee_features = [ee.Feature(ee.Geometry(feature["geometry"]), feature["properties"]) for feature in selected]
    return ee.FeatureCollection(ee_features), {
        "sourceTable": str(path),
        "sourceSha256": file_sha256(path),
        "selectedUnits": len(selected),
        "sourceUnits": len(features),
    }


def corridor_ndvi_zonal(ee: Any, request: dict[str, Any]) -> dict[str, Any]:
    units, source = read_corridor_units(ee, request)
    geometry = units.geometry()
    collection, metadata = ndvi_collection(ee, request, geometry)
    stat_name, stat_reducer = reducer(ee, request)
    if stat_name == "count":
        raise UserInputError("corridor_ndvi_zonal stat must not be count; pixel_count is already returned")
    options = reduction_options(request, geometry, 30)
    options.pop("geometry")
    options.pop("maxPixels")
    options.pop("bestEffort")
    image_limit = request.get("imageLimit")
    if image_limit is not None:
        image_limit = integer(image_limit, "imageLimit", 1, 500)
        collection = collection.sort("system:time_start").limit(image_limit)
    source_count = collection.size()
    image = collection.select("NDVI").median()
    combined = stat_reducer.combine(ee.Reducer.count(), sharedInputs=True)
    reduced = image.reduceRegions(collection=units, reducer=combined, **options).getInfo()
    value_key = stat_name
    rows = []
    dropped = 0
    for feature in reduced.get("features", []):
        properties = feature.get("properties") or {}
        value = properties.get(value_key)
        if value is None:
            dropped += 1
            continue
        rows.append({
            "segment_id": properties.get("segment_id"),
            "ring_id": properties.get("ring_id"),
            "ring_index": properties.get("ring_index"),
            "chainage_start_km": properties.get("chainage_start_km"),
            "chainage_end_km": properties.get("chainage_end_km"),
            "ring_inner_km": properties.get("ring_inner_km"),
            "ring_outer_km": properties.get("ring_outer_km"),
            "ring_area_km2": properties.get("ring_area_km2"),
            "value": value,
            "pixel_count": properties.get("count"),
        })
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = corridor_state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    csv_path = output_root / "ndvi_zonal.csv"
    manifest_path = output_root / "manifest.json"
    fieldnames = [
        "segment_id", "ring_id", "ring_index", "chainage_start_km", "chainage_end_km",
        "ring_inner_km", "ring_outer_km", "ring_area_km2", "value", "pixel_count",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    manifest = {
        "batchId": batch_id,
        "operation": "corridor_ndvi_zonal",
        "createdUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **source,
        **metadata,
        "stat": stat_name,
        "composite": "median",
        "scale": options["scale"],
        "imageCount": source_count.getInfo(),
        "imageLimit": image_limit,
        "droppedNullUnits": dropped,
        "outputRows": len(rows),
        "outputs": {"csv": {"path": str(csv_path), "sha256": file_sha256(csv_path)}},
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    relative = lambda path: f"state://{path.relative_to(corridor_state_root(request)).as_posix()}"
    warnings = []
    if dropped:
        warnings.append(f"{dropped} units had no valid NDVI pixels after masking")
    return {
        "ok": True,
        "operation": "corridor_ndvi_zonal",
        "batchId": batch_id,
        "summary": {
            "units": source["selectedUnits"],
            "outputRows": len(rows),
            "droppedNullUnits": dropped,
            "imageCount": manifest["imageCount"],
            "imageLimit": image_limit,
            "stat": stat_name,
            "scale": options["scale"],
        },
        "warnings": warnings,
        "rows": rows[:100],
        "paths": {"csv": str(csv_path), "manifest": str(manifest_path)},
        "statePaths": {"csv": relative(csv_path), "manifest": relative(manifest_path)},
    }


def self_test() -> dict[str, Any]:
    request = {
        "startDate": "2025-01-01",
        "endDate": "2025-02-01",
        "longitude": 116.4,
        "latitude": 39.9,
        "bufferMeters": 5000,
        "bands": ["B4", "B8"],
        "collection": S2_COLLECTION,
    }
    assert dates(request) == ("2025-01-01", "2025-02-01")
    assert geometry_spec(request)["kind"] == "point"
    assert bands(request) == ["B4", "B8"]
    assert collection_id(request) == S2_COLLECTION
    assert optional_limit({}, 100, 500) == 100
    assert geometry_spec({"geometryAsset": "projects/ee-my-road/assets/dukuregion"})["kind"] == "asset"
    with tempfile.TemporaryDirectory(prefix="gee-worker-") as temporary:
        root = Path(temporary)
        (root / "units.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        assert corridor_table_path({"table": "state://units.geojson", "stateRoot": str(root)}).is_file()
        try:
            corridor_table_path({"table": "state://../escape.geojson", "stateRoot": str(root)})
        except UserInputError:
            pass
        else:
            raise AssertionError("Escaping state table was accepted")
    for invalid in ({"longitude": 200, "latitude": 0}, {"longitude": 0}):
        try:
            geometry_spec(invalid)
        except UserInputError:
            pass
        else:
            raise AssertionError("Invalid point input was accepted")
    try:
        dates({"startDate": "2025-02-01", "endDate": "2025-01-01"})
    except UserInputError:
        pass
    else:
        raise AssertionError("Invalid date order was accepted")
    return {"ok": True, "selfTest": "passed", "checks": 11}


def error_message(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[:6000]


def is_transient_network_error(error: Exception) -> bool:
    text = str(error).lower()
    markers = (
        "ssl", "timeout", "timed out", "remote end closed", "winerror 10060",
        "connection reset", "econnreset", "temporarily unavailable", "deadline exceeded",
    )
    return any(marker in text for marker in markers)


def initialize_with_retry(ee: Any, options: dict[str, Any]) -> None:
    for attempt in range(3):
        try:
            ee.Initialize(**options)
            return
        except Exception as error:
            if attempt == 2 or not is_transient_network_error(error):
                raise
            time.sleep(attempt + 1)


def main() -> None:
    try:
        request = {'operation': '_self_test'} if '--self-test' in sys.argv else json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        if not isinstance(request, dict):
            raise UserInputError("Worker request must be a JSON object")
        operation = request.get("operation")
        if operation == "_self_test":
            result = self_test()
        else:
            if operation not in OPERATIONS:
                raise UserInputError(f"Unsupported operation: {operation}")
            try:
                import ee
            except ImportError as error:
                result = {
                    "ok": False,
                    "error": {
                        "code": "GEE_DEPENDENCY_MISSING",
                        "message": f"Cannot import earthengine-api: {error_message(error)}. Install requirements.txt in the configured Python environment.",
                    },
                }
            else:
                try:
                    options = {}
                    if isinstance(request.get("project"), str) and request["project"].strip():
                        options["project"] = request["project"].strip()
                    if isinstance(request.get("apiUrl"), str) and request["apiUrl"].strip():
                        options["opt_url"] = request["apiUrl"].strip()
                    initialize_with_retry(ee, options)
                except Exception as error:
                    result = {
                        "ok": False,
                        "error": {
                            "code": "GEE_AUTH_OR_PROJECT_FAILED",
                            "message": f"Earth Engine initialization failed: {error_message(error)}. Authenticate this Python environment with earthengine authenticate or configure a service account, and set a GEE-enabled Cloud project in the plugin config.",
                        },
                    }
                else:
                    if operation == "status":
                        probe = ee.Number(1).getInfo()
                        result = {
                            "ok": True,
                            "status": "ready",
                            "apiVersion": getattr(ee, "__version__", "unknown"),
                            "project": options.get("project", "auto-detected"),
                            "probe": probe,
                        }
                    elif operation == "collection_summary":
                        result = collection_summary(ee, request)
                    elif operation == "region_stats":
                        result = region_stats(ee, request)
                    elif operation == "timeseries":
                        result = timeseries(ee, request)
                    elif operation == "ndvi_timeseries":
                        result = ndvi_timeseries(ee, request)
                    elif operation == "ndvi_stats":
                        result = ndvi_stats(ee, request)
                    else:
                        result = corridor_ndvi_zonal(ee, request)
    except UserInputError as error:
        result = {"ok": False, "error": {"code": "GEE_INVALID_ARGUMENT", "message": str(error)}}
    except Exception as error:
        result = {"ok": False, "error": {"code": "GEE_REQUEST_FAILED", "message": error_message(error)}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))


if __name__ == "__main__":
    main()
