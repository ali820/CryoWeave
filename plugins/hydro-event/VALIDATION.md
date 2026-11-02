# Hydro Event 验证记录

验证日期：2026-11-02。

## 离线自测

使用插件独立环境运行：

```powershell
plugins/hydro-event/.venv/Scripts/python.exe plugins/hydro-event/hydro_worker.py --self-test
```

返回：

```json
{"ok": true, "selfTest": "passed", "checks": 11}
```

覆盖：DEM 库容曲线、最低高程基线、首层体积、储量插值、水量闭合、溃决平均出流、堰流情景、不同 CRS 下的掩膜网格与面积、VRT 根外/远程来源拒绝和路径逃逸。

独立参考值而非仅守恒检查：

- 100 个 `100 m × 100 m` 像元的总面积为 **1 km²**；0 m 基线有 1 个像元，10 m 有 11 个像元，因此首层梯形体积为 `(0.01 + 0.11) / 2 × 10 / 1000 = 0.0006 km³`；
- EPSG:4326 DEM 重投影到 EPSG:32645 后，掩膜与目标网格的 4 个内部像元精确对齐；期望面积为 `4 × |target_transform.a × target_transform.e| / 1e6`，并同时核对 `validPixels = 4`；
- 临时 VRT 分别引用 `projectRoot` 外的 GeoTIFF 与 `/vsicurl/https://example.invalid/dem.tif`，两者必须在打开栅格前拒绝，自测不访问网络。

## 真实库容与测高验证

使用 `hypsometry_卓乃湖_AW3D30.csv` 插值：

- 4760 m 对应储量 **6.5685575 km³**；
- 4744.6 m 对应储量 **3.4290355 km³**；
- 释放量 **3.1395220 km³**；
- 22 天平均出流 **1651.684554 m³/s**。

另行运行显式堰流情景：`widthM=40`、`dischargeCoefficient=1.0`、`headM=15.4`，峰值情景为 **2417.358558 m³/s**。该峰值是公式计算结果，不是实测流量或二维演进预测。

## 真实 closure_terms 闭合验证

参数：

- `outcomeTerm = 卓乃湖释放`，排除结果项；
- `nonSourceTerms = [本地径流, 湖面直接降水`，按非卓乃来源进项扣除；
- `outcomeKm3 = 3.073`。

结果：

- 净闭合项：**2.679 km³**；
- 闭合率：**87.178653%**；
- 残差：**0.394 km³**。

这与现有 `closure_summary_v3.txt` 的中心口径一致，避免了把结果项重复计入输入，以及把非来源降水/径流符号算反。

## 当前边界

- 库容曲线依赖 DEM、掩膜和垂直基准，水下地形不可见；
- 堰流和平均出流均为显式情景，不是实时预报；
- 闭合率依赖时间窗口、分项定义和符号约定；
- 安装与 Host 激活是不同状态，需按 DSH 提示重启或启用。
