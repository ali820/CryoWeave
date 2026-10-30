# Corridor Analysis 验证记录

验证日期：2026-10-30。

## 离线自测

`python plugins/corridor-analysis/corridor_worker.py --self-test` 返回：

```json
{"ok": true, "selfTest": "passed", "checks": 6}
```

覆盖：矢量检查、里程分段、独立缓冲环、单元面积、分组统计、加权/像元汇总和相对路径逃逸。

## 真实 GEE 路线验证

通过实际 `corridor_analysis` Host 工具读取：

`projects/ee-my-road/assets/G217duku_all_line`

路线检查结果：

- 525 个 LineString 要素；
- 无空几何、无无效几何；
- 源 CRS 为 EPSG:4326，工作 CRS 为 EPSG:32645；
- 合并长度为 **525.730278 km**。

随后生成独库北段研究单元：

- 方向：north_to_south；
- 范围：0–120 km；
- 分段：10 km；
- 独立环：2/4/6/8/10 km；
- 结果：**12 个段 × 5 环 = 60 个单元**。

验证批次：`20261030T152218Z-fb82dc74`。

输出文件：

- `units.geojson`
- `units.csv`
- `segments.geojson`
- `manifest.json`

全部位于独立 `.cryo-state/corridor` 状态目录，未修改原研究项目。

## 分环统计验证

对真实 `units.csv` 的 `ring_area_km2` 按 `ring_id` 汇总：

- 输入 60 行；
- 返回 5 个环组，每组 12 个单元；
- `ring_01` 面积合计为 **540.332024 km²**；
- 各组缺失值均为 0。

Host 返回同时包含绝对路径、`state://` 路径和 manifest，可继续用于 GEE/本地统计与 research-run 登记。

## 当前边界

- 不直接执行遥感区域统计；
- 不自动把不连通线路当成一条路线；
- 输出几何为 WGS84，长度和缓冲在明确的米制工作 CRS 中计算；
- 安装与 Host 激活是不同状态，需按 DSH 提示重启或启用。
