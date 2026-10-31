# GEE Analysis 1.1 验证记录

验证日期：2026-10-30。

## 兼容性与离线检查

- 保留 `gee_status`、`gee_analyze` 工具名及原有五类操作；
- 新增 `gee_corridor_analyze`，三个工具均通过 Host 注册和 Schema 检查；
- worker 自测返回 `{"ok":true,"selfTest":"passed","checks":11}`，新增共享状态路径和路径逃逸检查；
- 迁移版显式使用 UTF-8 stdin/stdout，中文项目路径与返回字段可正确处理；
- 原独立版本已验证 Landsat 清单、通用区域统计/时间序列和 Sentinel-2 NDVI 统计/时间序列，详见原 README 的验证说明。

## 真实走廊 NDVI 联合分析

使用 `corridor_analysis` 已生成的 `units.geojson`，直接复用其 `state://` 路径，不复制输入文件。

参数：

- 单元：`seg_0001`、`seg_0002`、`seg_0003`；
- 环：`ring_01`；
- 时间：2024-06-01 至 2024-09-01；
- 影像上限：10（仅用于功能验证）；
- 尺度：30 m；
- 统计：区域均值。

结果批次：`20261030T154140Z-d0c69c06`，返回 3 行：

| segment | ring | NDVI | 有效像元数 |
| --- | --- | ---: | ---: |
| seg_0001 | ring_01 | 0.1874206982 | 82,263 |
| seg_0002 | ring_01 | 0.2088483748 | 82,047 |
| seg_0003 | ring_01 | 0.4053411769 | 58,674 |

输出 `ndvi_zonal.csv` 和 manifest，均位于共享 corridor 状态目录。

## 跨插件复用验证

1. `corridor_analysis summarize` 直接读取该 NDVI CSV，按 segment 返回 3 组及像元汇总；
2. `research_run` 创建 `corridor-ndvi-zonal` 运行，登记 `state://corridor/20261030T154140Z-d0c69c06/ndvi_zonal.csv`；
3. `research_run verify` 返回 `current`，确认产物可回查和复用。

这验证了完整链路：

```text
路线资产
→ corridor_analysis 生成研究单元
→ gee_corridor_analyze 计算单元 NDVI
→ corridor_analysis 汇总结果
→ research_run 登记并验证产物
```

## 当前边界

- 分区 NDVI 使用中位数合成，`imageLimit` 仅用于限量测试，不应直接作为正式论文结果；
- `reduceRegions` 与 `reduceRegion` 参数不同，本工具不暴露不支持的 `maxPixels` / `bestEffort`；
- 安装激活前必须移除或停用旧包名，避免与旧 `gee_status` / `gee_analyze` 重名冲突；
- 旧源码目录保留作为回退，不随仓库迁移删除。
