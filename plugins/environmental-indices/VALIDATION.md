# Environmental Indices 验证记录

验证日期：2026-10-31。

## 离线自测

`python plugins/environmental-indices/indices_worker.py --self-test` 返回：

```json
{"ok": true, "selfTest": "passed", "checks": 6}
```

覆盖：六类指标计算、TVDI 干湿边拟合、输出波段与描述、栅格检查、路径逃逸和输入网格一致性。

## 真实 NDVI/LST 样例

输入：

- `NDVI/Q1/copyNDVI_2000_Q1.tif`
- `LST/Q1/copyLST_2000_Q1_Celsius.tif`

两份栅格大小均为 261×306，但原点与行方向不同。插件以 NDVI 栅格为目标网格，对 LST 进行双线性重投影，未修改源文件。

计算指标：`fvc`、`lst`、`tvdi`。

结果批次：`20261031T020633Z-a76ada71`。

- 有效像元：**12,141**；
- 有效比例：约 **15.20%**；
- TVDI 特征空间拟合点：**8,051**；
- 干边：slope **-51.941738**，intercept **24.068817**；
- 湿边：slope **34.624685**，intercept **-7.212753**；
- 平均 TVDI：约 **0.361912**；
- 平均 LST：约 **3.211 °C**。

输出包括 `indices.tif`、`stats.csv`、`tvdi_fit.json` 和 `manifest.json`。输出栅格含 3 个 Float32 波段，描述依次为 `fvc / lst / tvdi`，CRS 为 EPSG:4326。

不同调用中个别 NDVI 分箱边界点可能因浮点处理略有变化，验证关注有效像元与整体拟合一致性，而不是把单个边界点误判为错误。

## 当前边界

- 本次结果用于验证算法与网格配准，不应直接作为论文最终结论；
- 有效像元比例较低时，必须同时审查覆盖和空间分布；
- 干湿边拟合具有研究区和时间依赖性，不能未经验证跨期套用；
- 安装与 Host 激活是不同状态，需按 DSH 提示重启或启用。
