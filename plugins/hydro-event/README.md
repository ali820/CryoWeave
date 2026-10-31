# Hydro Event and Water Balance

用于 DEM 库容曲线、储量插值、水量闭合和溃决/堰流情景计算。

## `hydro_event`

### `hypsometry`

从 DEM 计算高程—面积—库容关系：

```json
{
  "action": "hypsometry",
  "projectRoot": "<project-root>",
  "source": "dem.tif",
  "mask": "lake_boundary.geojson",
  "workingCrs": "EPSG:32645",
  "elevationStepM": 1
}
```

输出 `hypsometry.csv` 和 manifest，字段：

- `elev_m`
- `area_below_km2`
- `volume_km3`
- `bin_area_km2`

`volume_km3` 由面积—高程梯形积分得到。掩膜、DEM 分辨率、垂直基准和水下地形不可见性必须单独审查。

### `storage`

使用已有库容曲线插值某个水位的面积与储量：

```json
{
  "action": "storage",
  "projectRoot": "<project-root>",
  "hypsometry": "04_水量平衡/output/hypsometry_卓乃湖_AW3D30.csv",
  "levelM": 4760
}
```

超出曲线范围时会标记 `clamped: true`，不会隐式假装曲线外推可靠。

### `water_balance`

对分项账进行闭合核算：

```json
{
  "action": "water_balance",
  "projectRoot": "<project-root>",
  "components": "04_水量平衡/output/closure_terms.csv",
  "outcomeKm3": 3.073,
  "outcomeTerm": "卓乃湖释放",
  "nonSourceTerms": ["本地径流", "湖面直接降水"]
}
```

参数语义：

- `outcomeTerm` 是结果项，不能再次计入输入项；
- `nonSourceTerms` 表示非本湖来源进项，闭合时从其他蓄水与损失中扣除；
- `outcomeKm3` 是观测或目标变化，用于计算闭合率和残差。

默认不指定这些参数时，只返回各分项的代数和，不假设某个项目的闭合公式。

### `breach`

根据库容曲线计算释放量和平均出流：

```json
{
  "action": "breach",
  "projectRoot": "<project-root>",
  "hypsometry": "04_水量平衡/output/hypsometry_卓乃湖_AW3D30.csv",
  "preLevelM": 4760,
  "postLevelM": 4744.6,
  "durationDays": 22
}
```

可选堰流情景：

```json
{
  "widthM": 40,
  "dischargeCoefficient": 1.0,
  "headM": 15.4
}
```

堰流公式：

```text
Q = C_d × width × head^1.5
```

所有结果均带 `scenarioOnly: true`。平均出流和堰流峰值不是预报，也不是二维洪水演进结果。

## 重要限制

- 库容曲线依赖 DEM 和湖盆掩膜，水下地形不可直接由光学 DEM 观测；
- 水位、面积、流量和蒸发的单位与基准必须一致；
- 闭合率依赖分项定义、时间窗口和符号约定，不能跨项目直接比较；
- 参数化情景不能替代实测流量、现场水动力验证或风险评估。
