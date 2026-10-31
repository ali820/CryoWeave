# Lake Lineage 验证记录

验证日期：2026-10-31。

## 离线自测

`python plugins/lake-lineage/lineage_worker.py --self-test` 返回：

```json
{"ok": true, "selfTest": "passed", "checks": 4}
```

覆盖：跨年续接、分裂、稳定队列、交通共位和路径逃逸。额外通过真实数据检查了几何 CRS、无效几何、非有限数值和 GeoJSON 输出兼容性。

## 真实 Lake_TP 2020–2023 全流程

输入四年 Shapefile，采用 `ESRI:102025` 米制工作 CRS，主导重叠阈值 0.95，小事端阈值 0.001。

通过实际 `lake_lineage` Host 工具完成：

- **21,823 条谱系**；
- **34,916 条事件记录**：
  - `minor_exchange` 13,994；
  - `disappear` 2,083；
  - `split` 307；
  - `new` 18,532。

验证批次：`20261031T122146Z-0a89f359`。

输出 `lineage.csv`、`events.csv`、`cohort.geojson` 和 `manifest.json`，均可继续回查。

## 交通共位验证

使用同一批次的端点队列和 `data/processed/transport_qxp.gpkg`：

- 返回 10 条最近距离记录；
- 包含 OSM ID、highway/railway 属性及 1/3/5/10 km 阈值；
- 无 `Infinity`、无 `NaN`；
- 示例中既有相交/近距离对象，也有 195.59 km 的远距对象，说明距离和阈值确实在变化。

交通共位只表示现状平面邻近，不能据此推断淹没、损伤或风险。

## 当前边界

- 队列资格是参数化筛选结果，不等于全高原湖泊总量或独立真值；
- 多候选重叠需要结合原始影像和湖泊定义复核；
- `completeSeries` 只表示对应起止区间均有观测，不证明观测质量；
- 安装与 Host 激活是不同状态，需按 DSH 提示重启或启用。
