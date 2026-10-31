# Lake Mapping and Morphology

从水体掩膜或 MNDWI/NDWI 提取湖泊多边形，并计算形态指标。也可直接分析已有 GeoJSON、Shapefile、GeoPackage 等矢量。

## `lake_mapper`

### `inspect`

检查栅格或矢量的网格、CRS、几何类型和范围。

### `extract`

从以下输入提取水体：

- `method: mask`：已有二值/分类掩膜，默认 `waterValues: [1]`；
- `method: mndwi`：需要 `green + swir1`；
- `method: ndwi`：需要 `green + nir`。

可配置阈值、比较方式、反射率缩放、NoData、4/8 连通性、最小面积、几何简化、是否只保留最大水体和数量上限。

```json
{
  "action": "extract",
  "projectRoot": "<project-root>",
  "inputs": {
    "green": "green.tif",
    "swir1": "swir1.tif"
  },
  "method": "mndwi",
  "threshold": 0,
  "operator": "gt",
  "minAreaKm2": 0.001,
  "connectivity": 8
}
```

### `morphology`

直接分析已有湖泊矢量：

```json
{
  "action": "morphology",
  "projectRoot": "E:/文章同步/25-湖泊连续性分析",
  "source": "02_水体提取/data/lake_polys.geojson",
  "workingCrs": "EPSG:32645",
  "minAreaKm2": 0.001,
  "limit": 10
}
```

## 指标

| 字段 | 含义 |
| --- | --- |
| `area_km2` | 多边形面积 |
| `perimeter_km` | 多边形周长，含内环 |
| `centroid` | 重心的 WGS84 经纬度，可能位于多边形外 |
| `representativePoint` | 保证位于多边形内的代表点 |
| `majorAxisKm / minorAxisKm` | 最小旋转矩形的长短轴 |
| `elongation` | 长短轴比 |
| `compactness` | `4πA/P²`，越接近圆形越接近 1 |
| `shapeIndex` | `P/(2√πA)`，圆形为 1，越复杂越大 |
| `fractalDimension` | 周长—面积关系的近似分形维数，仅用于同口径比较 |

面积、周长和轴长使用 `workingCrs` 计算；重心、代表点和边界转换为 WGS84。

## 输出

- `water_polygons.geojson`：湖泊多边形和形态属性；
- `morphology.csv`：核心形态指标表；
- `manifest.json`：输入输出哈希、过滤参数、数量和总面积。

所有新文件只写入独立 `stateRoot`，不修改源数据。

## 重要边界

- 缓冲、简化和最小面积过滤会改变形态结果，必须在论文中报告；
- 源文件如果已经包含 `+2 km 缓冲` 等说明，重新计算的面积不能当成未缓冲的真实湖面；
- 复杂湖泊的重心可能落在湖外，地图标注应使用代表点或另行确定；
- FD 是近似形态指标，不能替代更高分辨率的盒计数等分形分析。
