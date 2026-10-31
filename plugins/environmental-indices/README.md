# Environmental Indices and TVDI

从本地栅格计算 NDVI、FVC、LST、NDMI、NDSI 和 TVDI，输出有效像元统计、TVDI 干湿边拟合和可复现清单。

## `environmental_indices`

### `inspect`

检查输入波段、宽高、数据类型、CRS、仿射、范围和 NoData，便于确定各角色文件。

### `calculate`

`inputs` 使用角色映射，可指向单波段文件或多波段文件中的某个波段：

```json
{
  "ndvi": "NDVI/Q1/example.tif",
  "lst": "LST/Q1/example_Celsius.tif",
  "red": {"path": "stack.tif", "band": 3},
  "nir": {"path": "stack.tif", "band": 4},
  "green": {"path": "stack.tif", "band": 2},
  "swir1": {"path": "stack.tif", "band": 5},
  "qa": {"path": "qa.tif", "band": 1}
}
```

支持的指标及输入要求：

| 指标 | 输入 |
| --- | --- |
| `ndvi` | 已有 `ndvi`，或 `red + nir` |
| `fvc` | NDVI；可配置 `fvcNdviMin/Max` |
| `lst` | `lst`，支持摄氏/Kelvin 和缩放偏移 |
| `ndmi` | `nir + swir1` |
| `ndsi` | `green + swir1` |
| `tvdi` | NDVI + `lst`，另有分箱和干湿边参数 |

未指定 `indices` 时默认计算全部指标，因此也需要全部相应输入。只计算 TVDI/FVC/LST 时可这样调用：

```json
{
  "action": "calculate",
  "projectRoot": "E:/文章同步/16-dk2/1-data/dk_process",
  "inputs": {
    "ndvi": "NDVI/Q1/copyNDVI_2000_Q1.tif",
    "lst": "LST/Q1/copyLST_2000_Q1_Celsius.tif"
  },
  "indices": ["fvc", "lst", "tvdi"],
  "ndviBins": 10,
  "minBinPoints": 50
}
```

## 输出

每个批次生成：

- `indices.tif`：按请求顺序排列的 Float32 指标波段；
- `stats.csv`：各指标有效像元数、均值、标准差和范围；
- `tvdi_fit.json`：TVDI 分箱点数、干/湿边斜率和截距；
- `manifest.json`：输入输出哈希、参数、有效像元比例和拟合信息。

## 网格与 QA

- 以第一份输入栅格为目标网格；不同原点、行列方向或 CRS 的输入会重投影配准；
- 连续栅格使用双线性，QA 使用最近邻；
- 支持反射率/NDVI/LST 范围过滤、显式 NoData 和 Landsat QA_PIXEL；
- `keepSnow: true` 可在 QA 模式下保留雪像元，适合 NDSI 等专项分析，但不应默认用于普通水体或植被结论；
- 所有新文件只写入独立 `stateRoot`，不修改源栅格。

## 科学边界

TVDI 拟合依赖研究区、季节和有效样本。分箱不足、有效像元过少或干湿边不稳定时，不应直接跨期套用同一组边界。输出中的拟合参数与覆盖信息必须与结果一起审查。
