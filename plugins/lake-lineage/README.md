# Lake Lineage and Exposure

按年度湖泊几何追踪对象，输出谱系、事件台账、稳定端点队列和交通共位。插件不依赖固定湖泊 ID；没有 ID 时按双向重叠关系判断。

## `lake_lineage`

### `inspect`

检查每个年度文件的要素数、CRS 和可用多边形数量。

### `build`

输入 `yearFiles`：

```json
[
  {"year": 2020, "path": "data/raw/lake_tp/2020/Lake_TP_2020.shp"},
  {"year": 2021, "path": "data/raw/lake_tp/2021/Lake_TP_2021.shp"},
  {"year": 2022, "path": "data/raw/lake_tp/2022/Lake_TP_2022.shp"},
  {"year": 2023, "path": "data/raw/lake_tp/2023/Lake_TP_2023.shp"}
]
```

主要参数：

- `dominantOverlap`：双向最小重叠比例，默认 0.95；
- `minorOverlap`：小事端记录阈值，默认 0.001；
- `workingCrs`：重叠与面积的米制 CRS；
- `maxPolygonsPerYear`：每年最大多边形数；
- `startYear` / `endYear`：可选队列端点过滤；
- `requireComplete`：是否要求范围内每年均有观测；
- `simplifyMeters`：可选几何简化容差。

输出：

- `lineage.csv`：起止年、观测完整性、起止面积、变化和队列资格；
- `events.csv`：`new`、`disappear`、`merge`、`split`、`ambiguous`、`minor_exchange`；
- `cohort.geojson`：符合条件的端点几何；
- `manifest.json`：年度输入、阈值、数量和输出哈希。

多候选重叠时，如果最佳连接没有明显优势，会记录 `ambiguous`，不会强行续接。合并/分裂、消失和小事端分开记录。

### `colocate`

计算端点湖泊与现状交通网络的最近距离：

```json
{
  "action": "colocate",
  "projectRoot": "<project-root>",
  "cohort": "state://<batchId>/cohort.geojson",
  "transport": "data/processed/transport_qxp.gpkg",
  "workingCrs": "ESRI:102025",
  "distanceThresholdsKm": [1, 3, 5, 10],
  "limit": 100
}
```

输出 `colocation.csv` 和 manifest，包含最近距离、最近交通要素属性及 1/3/5/10 km 阈值判断。

## 科学边界

- 重叠关系不是湖泊身份或水力联系的证明；
- 队列资格取决于阈值、观测覆盖和几何质量，不能自动外推为全高原湖泊总量；
- 合并/分裂事件必须结合原始影像与对象定义复核；
- 交通共位只表示现状平面邻近，不表示历史暴露、淹没、损伤、因果关系或未来风险；
- 输出中的 `eligible` 仅是当前参数下的队列标记，不是科学结论许可。
