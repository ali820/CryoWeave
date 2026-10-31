# InSAR Operations and QA

用于 Sentinel-1 档案、干涉对规划、速率/相干性产品异常簇分析和参考版本核验。插件不替代 ISCE、MintPy 或 SBAS 反演。

## `insar_ops`

### `inspect`

盘点档案文件、轨道、日期、重复文件名和总大小：

```json
{
  "action": "inspect",
  "projectRoot": "<project-root>",
  "archiveRoot": "slc",
  "pattern": "*.zip"
}
```

### `plan`

按时间顺序生成干涉对：

```json
{
  "action": "plan",
  "projectRoot": "<project-root>",
  "archiveRoot": "slc",
  "maxTemporalBaselineDays": 48,
  "maxConnections": 4,
  "maxPairs": 5000,
  "sameTrack": true
}
```

输出 `interferogram_plan.csv` 和 manifest。该计划只基于文件名日期与轨道，**不表示已经计算垂直基线或完成干涉处理**。

### `analyze`

使用已有速率和相干性 GeoTIFF 生成异常簇：

```json
{
  "action": "analyze",
  "projectRoot": "E:/文章同步/26-镇原/镇原县_InSAR成果包_轻量版_20260818",
  "velocity": "镇原县_LOS速率_冬春季_202301-202608.tif",
  "coherence": "镇原县_时间相干性.tif",
  "velocityThreshold": 10,
  "coherenceThreshold": 0.7,
  "minAreaPixels": 20
}
```

默认保留：

- 绝对速率 ≥ 10 mm/yr；
- 时间相干性 ≥ 0.7；
- 至少 20 个相连像元。

输出：

- `clusters.csv`：像元数、面积、均值/中位数/极值/标准差、相干性、WGS84 质心；
- `summary.json`：有效像元、异常簇数量、异常面积和整体统计；
- `manifest.json`：输入、阈值和输出哈希。

异常簇只是 QA 和现场复核候选，不能直接称为滑坡、沉降病害或工程损伤。

### `verify`

核验参考清单：

```json
{
  "reference_version": "ref_fix_v1",
  "products": [
    {"path": "velocity_RefNew_full_T143A.h5", "sha256": "<sha256>"}
  ]
}
```

可附 `expectedReferenceVersion`，用于识别旧参考版本。文件缺失和 SHA-256 不符会分别报告。

## 科学边界

- 形变速率依赖参考点、轨道、解缠、季节和大气校正；
- 异常阈值只是筛选规则，不是普适病害判据；
- 垂直/东西向分解与 LOS 解释需要结合观测几何；
- 旧参考版本必须显式标记，不能与新参考产品混用；
- 计划生成不等于已完成 SBAS 反演或现场验证。
