# Corridor Analysis Units

走廊与距离梯度分析插件。把道路、铁路或其他线状研究对象转换为带里程和独立缓冲环的研究单元，并对单元表格做分组统计。

## `corridor_analysis`

### `inspect`

检查本地矢量或 GEE FeatureCollection：要素数、几何类型、坐标系、范围、长度/面积、空几何和无效几何。

```json
{
  "action": "inspect",
  "geometryAsset": "projects/ee-my-road/assets/G217duku_all_line",
  "workingCrs": "EPSG:32645"
}
```

### `prepare_units`

按里程切分线要素，并生成独立缓冲环。以下示例把独库北段 0–120 km 切成 10 km 一段，再生成 2/4/6/8/10 km 五环：

```json
{
  "action": "prepare_units",
  "geometryAsset": "projects/ee-my-road/assets/G217duku_all_line",
  "workingCrs": "EPSG:32645",
  "direction": "north_to_south",
  "startKm": 0,
  "endKm": 120,
  "segmentKm": 10,
  "rings": [2, 4, 6, 8, 10]
}
```

输出：

- `units.geojson`：所有 `segment × ring` 单元；
- `units.csv`：单元属性表，可直接用于分组统计或 GEE/本地表连接；
- `segments.geojson`：仅里程段；
- `manifest.json`：来源、参数、坐标系、数量、几何和输出 SHA-256。

每个单元包含：

- `segment_id`、`ring_id`；
- 起止里程与段长；
- `ring_inner_km`、`ring_outer_km`；
- 独立环面积 `ring_area_km2`；
- 工作坐标系。

独立环是逐环差集：`0–2`、`2–4`、`4–6` 等，不会把内环面积重复计入外环。

### `summarize`

对包含 `segment_id`、`ring_id` 的 CSV 按环、按段或两者联合汇总：

```json
{
  "action": "summarize",
  "table": "state://<batchId>/units.csv",
  "valueColumn": "ring_area_km2",
  "groupBy": "ring"
}
```

输出每组有效数、缺失数、均值、标准差、最小/最大、总和，可选加权均值、权重和与像元总数。

## 本地文件示例

```json
{
  "action": "prepare_units",
  "projectRoot": "E:/文章同步/16-dk2/5-独库植被过渡带",
  "source": "data/reference/dk_merge.shp",
  "workingCrs": "EPSG:32645",
  "segmentKm": 10,
  "rings": [2, 4, 6, 8, 10]
}
```

支持 GeoJSON、Shapefile、GeoPackage 等 GeoPandas 可读取格式。本地源文件必须位于配置的 `allowedRoots` 内。

## 当前边界

- 不直接执行遥感区域统计；先生成标准研究单元，再与现有 GEE 或本地流程连接；
- 不把多段不连通线路自动当成一条连续路线，需先修复拓扑；
- 缓冲使用明确的工作 CRS，输出回 WGS84 GeoJSON；
- 所有新生成文件只写入独立 `stateRoot`，不覆盖项目源文件；
- `state://<batchId>/...` 表示该批次目录下的文件。
